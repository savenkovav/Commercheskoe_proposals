from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.services.kp_preferences import KpPreferences
from src.services.models import MatchResult, ProposalSummary, TZItem


@dataclass
class ChatTurn:
    role: str
    text: str
    ts: float = field(default_factory=time.time)


@dataclass
class KpSession:
    session_id: str
    tz_items: list[TZItem]
    results: list[MatchResult]
    summary: ProposalSummary
    output_path: Path
    use_ai: bool
    preferences: KpPreferences = field(default_factory=KpPreferences)
    chat_history: list[ChatTurn] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class KpSessionStore:
    _MAX_SESSIONS = 30
    _TTL_SECONDS = 3600 * 8

    def __init__(self) -> None:
        self._sessions: dict[str, KpSession] = {}

    def create(self, session: KpSession) -> str:
        self._purge_stale()
        if len(self._sessions) >= self._MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.created_at)
            self._sessions.pop(oldest.session_id, None)
        self._sessions[session.session_id] = session
        return session.session_id

    def get(self, session_id: str) -> KpSession | None:
        self._purge_stale()
        return self._sessions.get(session_id)

    def _purge_stale(self) -> None:
        cutoff = time.time() - self._TTL_SECONDS
        stale = [sid for sid, s in self._sessions.items() if s.created_at < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)


_store = KpSessionStore()


def get_kp_session_store() -> KpSessionStore:
    return _store


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]
