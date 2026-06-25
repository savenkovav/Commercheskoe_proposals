from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import USERS_DB_PATH

logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"


@dataclass
class UserRecord:
    id: int
    login: str
    role: str
    created_at: str
    updated_at: str


@dataclass
class DownloadHistoryRow:
    id: int
    filename: str
    file_type: str
    downloaded_at: str
    user_login: str
    tz_filename: str | None
    xlsx_filename: str | None
    pdf_filename: str | None


@dataclass
class UploadHistoryRow:
    id: int
    original_filename: str
    items_count: int
    task_mode: str | None
    created_at: str
    user_login: str


class UserDatabase:
    def __init__(self, db_path: Path = USERS_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    login TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'manager')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tz_uploads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    items_count INTEGER NOT NULL DEFAULT 0,
                    task_mode TEXT,
                    session_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS file_exports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    session_id TEXT,
                    tz_filename TEXT,
                    xlsx_filename TEXT,
                    pdf_filename TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS download_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    export_id INTEGER REFERENCES file_exports(id) ON DELETE SET NULL,
                    filename TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    downloaded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id);
                CREATE INDEX IF NOT EXISTS idx_tz_uploads_user ON tz_uploads(user_id);
                CREATE INDEX IF NOT EXISTS idx_file_exports_user ON file_exports(user_id);
                CREATE INDEX IF NOT EXISTS idx_download_events_user ON download_events(user_id);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_user_by_login(self, login: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE login = ? COLLATE NOCASE",
                (login.strip(),),
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        return UserRecord(
            id=int(row["id"]),
            login=str(row["login"]),
            role=str(row["role"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_users(self, *, role: str | None = None) -> list[UserRecord]:
        query = "SELECT * FROM users"
        params: tuple[Any, ...] = ()
        if role:
            query += " WHERE role = ?"
            params = (role,)
        query += " ORDER BY role DESC, login COLLATE NOCASE"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            UserRecord(
                id=int(row["id"]),
                login=str(row["login"]),
                role=str(row["role"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def create_user(
        self,
        login: str,
        password_hash: str,
        password_salt: str,
        *,
        role: str = ROLE_MANAGER,
    ) -> UserRecord:
        now = self._now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (login, password_hash, password_salt, role, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (login, password_hash, password_salt, role, now, now),
            )
            user_id = int(cursor.lastrowid)
        user = self.get_user_by_id(user_id)
        if user is None:
            raise RuntimeError("Failed to create user")
        return user

    def update_user_credentials(
        self,
        user_id: int,
        *,
        login: str | None = None,
        password_hash: str | None = None,
        password_salt: str | None = None,
        role: str | None = None,
    ) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                return None
            now = self._now()
            conn.execute(
                """
                UPDATE users
                SET login = ?, password_hash = ?, password_salt = ?, role = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    login or str(row["login"]),
                    password_hash if password_hash is not None else str(row["password_hash"]),
                    password_salt if password_salt is not None else str(row["password_salt"]),
                    role or str(row["role"]),
                    now,
                    user_id,
                ),
            )
        return self.get_user_by_id(user_id)

    def delete_user(self, user_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0

    def count_admins(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role = ?",
                (ROLE_ADMIN,),
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def create_auth_token(self, user_id: int, token: str, expires_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_tokens (token, user_id, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (token, user_id, expires_at, self._now()),
            )

    def delete_auth_token(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))

    def get_user_id_by_token(self, token: str) -> int | None:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id FROM auth_tokens
                WHERE token = ? AND expires_at > ?
                """,
                (token, now),
            ).fetchone()
        return int(row["user_id"]) if row else None

    def record_tz_upload(
        self,
        user_id: int,
        *,
        filename: str,
        original_filename: str,
        items_count: int,
        task_mode: str | None,
        session_id: str | None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tz_uploads
                (user_id, filename, original_filename, items_count, task_mode, session_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    filename,
                    original_filename,
                    items_count,
                    task_mode,
                    session_id,
                    self._now(),
                ),
            )
            return int(cursor.lastrowid)

    def record_file_export(
        self,
        user_id: int,
        *,
        session_id: str | None,
        tz_filename: str | None,
        xlsx_filename: str,
        pdf_filename: str,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO file_exports
                (user_id, session_id, tz_filename, xlsx_filename, pdf_filename, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_id,
                    tz_filename,
                    xlsx_filename,
                    pdf_filename,
                    self._now(),
                ),
            )
            return int(cursor.lastrowid)

    def find_export_by_filename(self, filename: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM file_exports
                WHERE xlsx_filename = ? OR pdf_filename = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (filename, filename),
            ).fetchone()
        return dict(row) if row else None

    def record_download(
        self,
        user_id: int,
        *,
        filename: str,
        file_type: str,
        export_id: int | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO download_events
                (user_id, export_id, filename, file_type, downloaded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, export_id, filename, file_type, self._now()),
            )
            return int(cursor.lastrowid)

    def list_download_history(
        self,
        *,
        user_id: int | None = None,
        limit: int = 200,
    ) -> list[DownloadHistoryRow]:
        query = """
            SELECT
                d.id,
                d.filename,
                d.file_type,
                d.downloaded_at,
                u.login AS user_login,
                e.tz_filename,
                e.xlsx_filename,
                e.pdf_filename
            FROM download_events d
            JOIN users u ON u.id = d.user_id
            LEFT JOIN file_exports e ON e.id = d.export_id
        """
        params: list[Any] = []
        if user_id is not None:
            query += " WHERE d.user_id = ?"
            params.append(user_id)
        query += " ORDER BY d.downloaded_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            DownloadHistoryRow(
                id=int(row["id"]),
                filename=str(row["filename"]),
                file_type=str(row["file_type"]),
                downloaded_at=str(row["downloaded_at"]),
                user_login=str(row["user_login"]),
                tz_filename=row["tz_filename"],
                xlsx_filename=row["xlsx_filename"],
                pdf_filename=row["pdf_filename"],
            )
            for row in rows
        ]

    def list_upload_history(
        self,
        *,
        user_id: int | None = None,
        limit: int = 200,
    ) -> list[UploadHistoryRow]:
        query = """
            SELECT t.id, t.original_filename, t.items_count, t.task_mode, t.created_at, u.login AS user_login
            FROM tz_uploads t
            JOIN users u ON u.id = t.user_id
        """
        params: list[Any] = []
        if user_id is not None:
            query += " WHERE t.user_id = ?"
            params.append(user_id)
        query += " ORDER BY t.created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            UploadHistoryRow(
                id=int(row["id"]),
                original_filename=str(row["original_filename"]),
                items_count=int(row["items_count"]),
                task_mode=row["task_mode"],
                created_at=str(row["created_at"]),
                user_login=str(row["user_login"]),
            )
            for row in rows
        ]


_db: UserDatabase | None = None


def get_user_database() -> UserDatabase:
    global _db
    if _db is None:
        _db = UserDatabase()
    return _db
