"""SQLite-хранилище сессий формирования КП."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from src.config import KP_MAX_SESSIONS, KP_SESSIONS_DB_PATH, KP_SESSIONS_PATH

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KpSessionDatabase:
    def __init__(self, db_path: Path = KP_SESSIONS_DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate_json_if_needed()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            with self._connection() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS kp_sessions (
                        session_id TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_kp_sessions_created
                        ON kp_sessions(created_at);
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
                    ("version", str(_SCHEMA_VERSION)),
                )

    def _migrate_json_if_needed(self) -> None:
        if not KP_SESSIONS_PATH.exists():
            return
        with self._lock:
            with self._connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM kp_sessions").fetchone()[0]
                if count > 0:
                    return
        try:
            payload = json.loads(KP_SESSIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read legacy kp_sessions.json")
            return
        rows = payload.get("sessions", [])
        if not isinstance(rows, list) or not rows:
            return
        imported = 0
        for row in rows:
            if not isinstance(row, dict) or not row.get("session_id"):
                continue
            self.save_payload(
                str(row["session_id"]),
                row,
                created_at=float(row.get("created_at") or 0) or None,
            )
            imported += 1
        logger.info(
            "Migrated %s KP sessions from %s into SQLite %s",
            imported,
            KP_SESSIONS_PATH,
            self.db_path,
        )

    def save_payload(
        self,
        session_id: str,
        payload: dict,
        *,
        created_at: float | None = None,
    ) -> None:
        now_ts = created_at if created_at is not None else float(payload.get("created_at") or 0)
        if not now_ts:
            import time

            now_ts = time.time()
        updated_at = _utc_now()
        with self._lock:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO kp_sessions(session_id, payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        json.dumps(payload, ensure_ascii=False),
                        now_ts,
                        updated_at,
                    ),
                )

    def get_payload(self, session_id: str) -> dict | None:
        with self._lock:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM kp_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except Exception:
            logger.exception("Failed to decode KP session payload %s", session_id)
            return None
        return payload if isinstance(payload, dict) else None

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            with self._connection() as conn:
                conn.execute("DELETE FROM kp_sessions WHERE session_id = ?", (session_id,))

    def count_sessions(self) -> int:
        with self._lock:
            with self._connection() as conn:
                row = conn.execute("SELECT COUNT(*) FROM kp_sessions").fetchone()
        return int(row[0]) if row else 0

    def purge_stale(self, ttl_seconds: float) -> int:
        import time

        cutoff = time.time() - ttl_seconds
        with self._lock:
            with self._connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM kp_sessions WHERE created_at < ?",
                    (cutoff,),
                )
                return int(cursor.rowcount or 0)

    def enforce_max_sessions(self, max_sessions: int) -> None:
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT session_id FROM kp_sessions
                    ORDER BY created_at ASC
                    """
                ).fetchall()
                overflow = len(rows) - max_sessions
                if overflow <= 0:
                    return
                for row in rows[:overflow]:
                    conn.execute(
                        "DELETE FROM kp_sessions WHERE session_id = ?",
                        (str(row["session_id"]),),
                    )

    def stats(self) -> dict[str, int]:
        return {
            "active_sessions": self.count_sessions(),
            "max_sessions": KP_MAX_SESSIONS,
        }


_db: KpSessionDatabase | None = None


def get_kp_session_database() -> KpSessionDatabase:
    global _db
    if _db is None:
        _db = KpSessionDatabase()
    return _db
