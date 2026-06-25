from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from src.services.user_db import ROLE_ADMIN, ROLE_MANAGER, UserDatabase, get_user_database

logger = logging.getLogger(__name__)

AUTH_COOKIE_NAME = "kp_auth"
AUTH_TOKEN_TTL_DAYS = 7
CREDENTIAL_PATTERN = re.compile(r"^[a-zA-Z0-9_.@%!/]+$")
LOGIN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
PASSWORD_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@%!/"


def validate_credential(value: str) -> bool:
    text = (value or "").strip()
    return bool(text) and bool(CREDENTIAL_PATTERN.fullmatch(text))


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt_value = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_value.encode("utf-8"),
        120_000,
    )
    return digest.hex(), salt_value


def verify_password(password: str, password_hash: str, password_salt: str) -> bool:
    candidate, _ = hash_password(password, password_salt)
    return secrets.compare_digest(candidate, password_hash)


def generate_login(db: UserDatabase, *, length: int = 8) -> str:
    for _ in range(50):
        login = "".join(secrets.choice(LOGIN_ALPHABET) for _ in range(length))
        if not db.get_user_by_login(login):
            return login
    raise RuntimeError("Не удалось сгенерировать уникальный логин")


def generate_password(*, length: int = 12) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def ensure_default_admin(db: UserDatabase | None = None) -> None:
    database = db or get_user_database()
    if database.get_user_by_login("admin"):
        return
    password_hash, password_salt = hash_password("admin")
    database.create_user(
        "admin",
        password_hash,
        password_salt,
        role=ROLE_ADMIN,
    )
    logger.info("Default admin user created (login: admin)")


def authenticate(login: str, password: str) -> dict | None:
    db = get_user_database()
    row = db.get_user_by_login(login)
    if not row:
        return None
    if not verify_password(password, str(row["password_hash"]), str(row["password_salt"])):
        return None
    user = db.get_user_by_id(int(row["id"]))
    if user is None:
        return None
    token = secrets.token_urlsafe(32)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=AUTH_TOKEN_TTL_DAYS)
    ).isoformat()
    db.create_auth_token(user.id, token, expires_at)
    return {
        "token": token,
        "user": {
            "id": user.id,
            "login": user.login,
            "role": user.role,
        },
    }


def logout(token: str | None) -> None:
    if not token:
        return
    get_user_database().delete_auth_token(token)


def resolve_user_by_token(token: str | None):
    if not token:
        return None
    db = get_user_database()
    user_id = db.get_user_id_by_token(token)
    if user_id is None:
        return None
    return db.get_user_by_id(user_id)


def create_manager() -> dict[str, str]:
    db = get_user_database()
    login = generate_login(db)
    password = generate_password()
    password_hash, password_salt = hash_password(password)
    user = db.create_user(login, password_hash, password_salt, role=ROLE_MANAGER)
    return {
        "id": str(user.id),
        "login": user.login,
        "password": password,
        "role": user.role,
    }


def update_user_login(user_id: int, login: str) -> None:
    if not validate_credential(login):
        raise ValueError("Логин может содержать только латинские буквы и символы _ . @ % ! /")
    db = get_user_database()
    if db.get_user_by_login(login) and db.get_user_by_login(login)["id"] != user_id:
        raise ValueError("Пользователь с таким логином уже существует")
    updated = db.update_user_credentials(user_id, login=login.strip())
    if updated is None:
        raise ValueError("Пользователь не найден")


def update_user_password(user_id: int, password: str) -> None:
    if not validate_credential(password):
        raise ValueError("Пароль может содержать только латинские буквы и символы _ . @ % ! /")
    password_hash, password_salt = hash_password(password)
    updated = get_user_database().update_user_credentials(
        user_id,
        password_hash=password_hash,
        password_salt=password_salt,
    )
    if updated is None:
        raise ValueError("Пользователь не найден")


def promote_to_admin(user_id: int) -> None:
    updated = get_user_database().update_user_credentials(user_id, role=ROLE_ADMIN)
    if updated is None:
        raise ValueError("Пользователь не найден")


def delete_manager(user_id: int, *, current_user_id: int) -> None:
    db = get_user_database()
    user = db.get_user_by_id(user_id)
    if user is None:
        raise ValueError("Пользователь не найден")
    if user.id == current_user_id:
        raise ValueError("Нельзя удалить собственную учётную запись")
    if user.role == ROLE_ADMIN and db.count_admins() <= 1:
        raise ValueError("Нельзя удалить последнего администратора")
    if not db.delete_user(user_id):
        raise ValueError("Не удалось удалить пользователя")
