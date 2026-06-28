from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src.config import KP_SESSIONS_PATH
from src.services.kp_preferences import KpPreferences
from src.services.models import (
    KitComponentLine,
    MatchResult,
    MatchSource,
    MatchStatus,
    PriceQuote,
    ProposalSummary,
    TZItem,
)

logger = logging.getLogger(__name__)


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
    pdf_path: Path | None = None
    preferences: KpPreferences = field(default_factory=KpPreferences)
    chat_history: list[ChatTurn] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    task_mode: str = "task1"
    stage: str = "intake"
    search_completed: bool = False
    tz_filename: str = ""
    rag_chunks: list[dict[str, str | int | float]] = field(default_factory=list)
    rag_vectors: list[list[float]] = field(default_factory=list)


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {key: _to_jsonable(value) for key, value in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, list):
        return [_to_jsonable(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _to_jsonable(value) for key, value in obj.items()}
    return obj


def _decode_tz_item(data: dict[str, Any]) -> TZItem:
    return TZItem(
        number=int(data["number"]),
        name=str(data["name"]),
        unit=str(data.get("unit") or "шт"),
        quantity=float(data.get("quantity") or 0),
        specifications=str(data.get("specifications") or ""),
        country_of_origin=str(data.get("country_of_origin") or ""),
        target_sale_price=data.get("target_sale_price"),
    )


def _decode_price_quote(data: dict[str, Any]) -> PriceQuote:
    return PriceQuote(
        source=str(data.get("source") or ""),
        label=str(data.get("label") or ""),
        matched_name=str(data.get("matched_name") or ""),
        price=data.get("price"),
        cost=data.get("cost"),
        price_label=data.get("price_label"),
        wholesale_price=data.get("wholesale_price"),
        articul=data.get("articul"),
        supplier=data.get("supplier"),
        purchase_date=data.get("purchase_date"),
        match_score=float(data.get("match_score") or 0),
        url=data.get("url"),
        notes=str(data.get("notes") or ""),
        image_url=data.get("image_url"),
    )


def _decode_kit_component(data: dict[str, Any]) -> KitComponentLine:
    return KitComponentLine(
        name=str(data.get("name") or ""),
        unit_cost=data.get("unit_cost"),
        unit_price=data.get("unit_price"),
        quantity=float(data.get("quantity") or 1),
        supplier=data.get("supplier"),
        price_list_price=data.get("price_list_price"),
        purchase_date=data.get("purchase_date"),
        competitor_url=data.get("competitor_url"),
        competitor_platform=data.get("competitor_platform"),
        found_in_catalog=bool(data.get("found_in_catalog")),
        catalog_matched_name=data.get("catalog_matched_name"),
    )


def _decode_match_result(data: dict[str, Any]) -> MatchResult:
    price_list_check = data.get("price_list_check")
    return MatchResult(
        tz_item=_decode_tz_item(data["tz_item"]),
        status=MatchStatus(str(data["status"])),
        source=MatchSource(str(data["source"])),
        matched_name=str(data.get("matched_name") or ""),
        match_score=float(data.get("match_score") or 0),
        unit_cost=data.get("unit_cost"),
        unit_base_price=data.get("unit_base_price"),
        unit_price=data.get("unit_price"),
        total_cost=data.get("total_cost"),
        total_price=data.get("total_price"),
        notes=str(data.get("notes") or ""),
        source_detail=str(data.get("source_detail") or ""),
        alternatives=[str(item) for item in data.get("alternatives") or []],
        supplier=data.get("supplier"),
        purchase_date=data.get("purchase_date"),
        comparison=[_decode_price_quote(item) for item in data.get("comparison") or []],
        competitors=[_decode_price_quote(item) for item in data.get("competitors") or []],
        kit_components=[_decode_kit_component(item) for item in data.get("kit_components") or []],
        price_list_check=_decode_price_quote(price_list_check) if price_list_check else None,
        is_kit=bool(data.get("is_kit")),
        internet_priced=bool(data.get("internet_priced")),
        applied_markup_pct=data.get("applied_markup_pct"),
    )


def _decode_summary(data: dict[str, Any]) -> ProposalSummary:
    return ProposalSummary(
        total_items=int(data.get("total_items") or 0),
        exact_count=int(data.get("exact_count") or 0),
        similar_count=int(data.get("similar_count") or 0),
        not_found_count=int(data.get("not_found_count") or 0),
        total_cost=float(data.get("total_cost") or 0),
        total_base_price=float(data.get("total_base_price") or 0),
        total_price=float(data.get("total_price") or 0),
        processing_seconds=float(data.get("processing_seconds") or 0),
    )


def _decode_preferences(data: dict[str, Any] | None) -> KpPreferences:
    payload = data or {}
    return KpPreferences(
        excluded_platforms=[str(item) for item in payload.get("excluded_platforms") or []],
        disabled_sources=[str(item) for item in payload.get("disabled_sources") or []],
        search_kit_component_links=bool(payload.get("search_kit_component_links")),
        force_kit_component_pricing=bool(payload.get("force_kit_component_pricing")),
        rules=[str(item) for item in payload.get("rules") or []],
    )


def _decode_chat_turn(data: dict[str, Any]) -> ChatTurn:
    return ChatTurn(
        role=str(data.get("role") or ""),
        text=str(data.get("text") or ""),
        ts=float(data.get("ts") or time.time()),
    )


def _decode_session(data: dict[str, Any]) -> KpSession | None:
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        return None
    try:
        return KpSession(
            session_id=session_id,
            tz_items=[_decode_tz_item(item) for item in data.get("tz_items") or []],
            results=[_decode_match_result(item) for item in data.get("results") or []],
            summary=_decode_summary(data.get("summary") or {}),
            output_path=Path(str(data.get("output_path") or "output/pending.xlsx")),
            pdf_path=Path(str(data["pdf_path"])) if data.get("pdf_path") else None,
            use_ai=bool(data.get("use_ai", True)),
            preferences=_decode_preferences(data.get("preferences")),
            chat_history=[_decode_chat_turn(item) for item in data.get("chat_history") or []],
            created_at=float(data.get("created_at") or time.time()),
            task_mode=str(data.get("task_mode") or "task1"),
            stage=str(data.get("stage") or "intake"),
            search_completed=bool(data.get("search_completed")),
            tz_filename=str(data.get("tz_filename") or ""),
            rag_chunks=list(data.get("rag_chunks") or []),
            rag_vectors=list(data.get("rag_vectors") or []),
        )
    except Exception:
        logger.exception("Failed to decode KP session %s", session_id)
        return None


class KpSessionStore:
    _MAX_SESSIONS = 30
    _TTL_SECONDS = 3600 * 8

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or KP_SESSIONS_PATH
        self._sessions: dict[str, KpSession] = {}
        self._loaded = False

    def create(self, session: KpSession) -> str:
        self.ensure_loaded()
        self._purge_stale()
        if len(self._sessions) >= self._MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda item: item.created_at)
            self._sessions.pop(oldest.session_id, None)
        self._sessions[session.session_id] = session
        self.save()
        return session.session_id

    def get(self, session_id: str) -> KpSession | None:
        self.ensure_loaded()
        self._purge_stale()
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return session

    def save(self) -> None:
        self.ensure_loaded()
        self._purge_stale()
        payload = {
            "updated_at": time.time(),
            "sessions": [_to_jsonable(session) for session in self._sessions.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = payload.get("sessions", [])
            if not isinstance(rows, list):
                return
            for row in rows:
                if not isinstance(row, dict):
                    continue
                session = _decode_session(row)
                if session:
                    self._sessions[session.session_id] = session
        except Exception:
            logger.exception("Failed to load KP sessions from %s", self.path)

    def _purge_stale(self) -> None:
        cutoff = time.time() - self._TTL_SECONDS
        stale = [sid for sid, session in self._sessions.items() if session.created_at < cutoff]
        if not stale:
            return
        for sid in stale:
            self._sessions.pop(sid, None)
        self.save()


_store = KpSessionStore()


def get_kp_session_store() -> KpSessionStore:
    return _store
