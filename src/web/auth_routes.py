from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from src.config import AUTH_ENABLED, OUTPUT_DIR
from src.services.auth_service import (
    AUTH_COOKIE_NAME,
    AUTH_TOKEN_TTL_DAYS,
    authenticate,
    create_manager,
    delete_manager,
    logout,
    promote_to_admin,
    resolve_user_by_token,
    update_user_login,
    update_user_password,
    validate_credential,
)
from src.services.user_db import ROLE_ADMIN, ROLE_MANAGER, UserRecord, get_user_database

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])
history_router = APIRouter(prefix="/api/history", tags=["history"])


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UpdateCredentialsRequest(BaseModel):
    login: str | None = Field(default=None, max_length=64)
    password: str | None = Field(default=None, max_length=128)


class CreateManagerRequest(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


def _user_payload(user: UserRecord) -> dict:
    return {
        "id": user.id,
        "login": user.login,
        "role": user.role,
        "is_admin": user.role == ROLE_ADMIN,
    }


def get_current_user(
    request: Request,
    kp_auth: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
) -> UserRecord:
    if not AUTH_ENABLED:
        return UserRecord(
            id=0,
            login="local",
            role=ROLE_ADMIN,
            created_at="",
            updated_at="",
        )
    token = kp_auth or request.headers.get("X-Auth-Token")
    user = resolve_user_by_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    request.state.user = user
    request.state.auth_token = token
    return user


def get_admin_user(user: UserRecord = Depends(get_current_user)) -> UserRecord:
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Доступ только для администратора")
    return user


@auth_router.post("/login")
def api_login(body: LoginRequest, response: Response) -> dict:
    result = authenticate(body.login.strip(), body.password)
    if result is None:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=result["token"],
        httponly=True,
        samesite="lax",
        max_age=AUTH_TOKEN_TTL_DAYS * 24 * 3600,
        path="/",
    )
    return {"user": result["user"]}


@auth_router.post("/logout")
def api_logout(
    response: Response,
    user: UserRecord = Depends(get_current_user),
    kp_auth: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
) -> dict:
    del user
    logout(kp_auth)
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return {"ok": True}


@auth_router.get("/me")
def api_me(user: UserRecord = Depends(get_current_user)) -> dict:
    return _user_payload(user)


@admin_router.get("/system-status")
def api_admin_system_status(_: UserRecord = Depends(get_admin_user)) -> dict:
    from src.services.system_status import get_system_status

    return get_system_status()


@admin_router.get("/users")
def api_list_users(_: UserRecord = Depends(get_admin_user)) -> dict:
    users = get_user_database().list_users()
    return {
        "items": [
            {
                "id": user.id,
                "login": user.login,
                "role": user.role,
                "created_at": user.created_at,
            }
            for user in users
        ]
    }


@admin_router.post("/users/managers")
def api_create_manager(
    body: CreateManagerRequest,
    _: UserRecord = Depends(get_admin_user),
) -> dict:
    try:
        created = create_manager(body.login, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = get_user_database().get_user_by_id(int(created["id"]))
    if user is None:
        raise HTTPException(status_code=500, detail="Не удалось создать пользователя")
    return {"user": _user_payload(user)}


@admin_router.post("/users/{user_id}/promote")
def api_promote_user(user_id: int, _: UserRecord = Depends(get_admin_user)) -> dict:
    try:
        promote_to_admin(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = get_user_database().get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"user": _user_payload(user)}


@admin_router.patch("/users/{user_id}")
def api_update_user(
    user_id: int,
    body: UpdateCredentialsRequest,
    _: UserRecord = Depends(get_admin_user),
) -> dict:
    if not body.login and not body.password:
        raise HTTPException(status_code=400, detail="Укажите новый логин или пароль")
    try:
        if body.login:
            if not validate_credential(body.login):
                raise ValueError(
                    "Логин может содержать только латинские буквы и символы _ . @ % ! /"
                )
            update_user_login(user_id, body.login.strip())
        if body.password:
            if not validate_credential(body.password):
                raise ValueError(
                    "Пароль может содержать только латинские буквы и символы _ . @ % ! /"
                )
            update_user_password(user_id, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = get_user_database().get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"user": _user_payload(user)}


@admin_router.delete("/users/{user_id}")
def api_delete_user(
    user_id: int,
    current: UserRecord = Depends(get_admin_user),
) -> dict:
    try:
        delete_manager(user_id, current_user_id=current.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@history_router.get("")
def api_history(user: UserRecord = Depends(get_current_user)) -> dict:
    db = get_user_database()
    if user.role == ROLE_ADMIN:
        downloads = db.list_download_history(limit=300)
        uploads = db.list_upload_history(limit=300)
    else:
        downloads = db.list_download_history(user_id=user.id, limit=300)
        uploads = db.list_upload_history(user_id=user.id, limit=300)

    return {
        "role": user.role,
        "downloads": [
            {
                "id": row.id,
                "filename": row.filename,
                "file_type": row.file_type,
                "downloaded_at": row.downloaded_at,
                "user_login": row.user_login,
                "tz_filename": row.tz_filename,
                "download_url": f"/api/files/{row.filename}",
            }
            for row in downloads
        ],
        "uploads": [
            {
                "id": row.id,
                "original_filename": row.original_filename,
                "items_count": row.items_count,
                "task_mode": row.task_mode,
                "created_at": row.created_at,
                "user_login": row.user_login,
            }
            for row in uploads
        ],
    }


def _safe_history_file_path(filename: str) -> Path | None:
    safe_name = Path(filename).name
    if not safe_name.startswith("KP_"):
        return None
    if not (safe_name.endswith(".xlsx") or safe_name.endswith(".pdf")):
        return None
    path = (OUTPUT_DIR / safe_name).resolve()
    output_root = OUTPUT_DIR.resolve()
    if output_root not in path.parents:
        return None
    return path


@history_router.delete("/downloads/{event_id}")
def api_delete_download_history(
    event_id: int,
    user: UserRecord = Depends(get_current_user),
) -> dict:
    db = get_user_database()
    try:
        result = db.delete_download_event(
            event_id,
            acting_user_id=user.id,
            is_admin=user.role == ROLE_ADMIN,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    for filename in result.get("files_to_remove") or []:
        path = _safe_history_file_path(filename)
        if path and path.exists() and path.is_file():
            try:
                path.unlink()
            except OSError:
                logger.warning("Failed to delete history file %s", path, exc_info=True)

    return {"ok": True, "id": result["id"]}
