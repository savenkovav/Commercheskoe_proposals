from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from src.config import SEARCH_KIT_COMPONENT_LINKS
from src.services.kp_preferences import (
    KpPreferences,
    filter_comparison_quotes,
)
from src.services.kp_session import ChatTurn, KpSession, get_kp_session_store
from src.services.markup_settings import get_markup_percent, set_markup_percent
from src.services.models import MatchResult, MatchSource, ProposalSummary, TZItem
from src.services.proposal_processor import ProposalProcessor

logger = logging.getLogger(__name__)


class KpChatService:
    def __init__(self, processor: ProposalProcessor) -> None:
        self.processor = processor
        self.store = get_kp_session_store()

    def create_session(
        self,
        tz_items: list[TZItem],
        results: list[MatchResult],
        summary: ProposalSummary,
        output_path: Path,
        use_ai: bool,
    ) -> str:
        preferences = KpPreferences(
            search_kit_component_links=SEARCH_KIT_COMPONENT_LINKS,
        )
        session = KpSession(
            session_id="",
            tz_items=tz_items,
            results=results,
            summary=summary,
            output_path=output_path,
            use_ai=use_ai,
            preferences=preferences,
        )
        from src.services.kp_session import new_session_id

        session.session_id = new_session_id()
        self._apply_preferences_to_results(session.results, session.preferences)
        return self.store.create(session)

    def chat(self, session_id: str, message: str) -> dict:
        session = self.store.get(session_id)
        if not session:
            raise ValueError("Сессия не найдена. Сформируйте КП заново.")

        text = message.strip()
        if not text:
            raise ValueError("Введите сообщение")

        session.chat_history.append(ChatTurn(role="user", text=text))

        items_summary = self._items_summary(session.results)
        patch = self.processor.ai.interpret_kp_refinement(
            user_message=text,
            items_summary=items_summary,
            preferences=session.preferences.to_dict(),
            chat_history=[
                {"role": turn.role, "text": turn.text}
                for turn in session.chat_history[-10:]
            ],
            markup_percent=get_markup_percent(),
        )

        reply = str(patch.get("reply") or "Готово.")
        prefs_before = session.preferences.to_dict()
        session.preferences.merge_ai_patch(patch)
        preferences_changed = session.preferences.to_dict() != prefs_before

        markup_value = patch.get("markup_percent")
        if markup_value is not None:
            try:
                set_markup_percent(float(markup_value))
            except ValueError:
                pass

        reprocess_all = bool(patch.get("reprocess_all"))
        reprocess_items = patch.get("reprocess_items") or []
        numbers_to_reprocess: set[int] = set()
        if reprocess_all or preferences_changed:
            numbers_to_reprocess = {item.number for item in session.tz_items}
        else:
            for raw in reprocess_items:
                try:
                    numbers_to_reprocess.add(int(raw))
                except (TypeError, ValueError):
                    continue

        if numbers_to_reprocess:
            self._reprocess_items(session, numbers_to_reprocess)

        if session.preferences.force_kit_component_pricing:
            self._apply_kit_aggregation(session.results)

        self._apply_preferences_to_results(session.results, session.preferences)

        for result in session.results:
            self.processor._apply_pricing(result)

        start = time.perf_counter()
        session.summary = self.processor._build_summary(
            session.results,
            time.perf_counter() - start,
        )
        session.output_path = self._next_output_path(session.output_path)
        self.processor.excel.generate(
            session.results,
            session.summary,
            session.output_path,
            preferences=session.preferences,
        )

        session.chat_history.append(ChatTurn(role="assistant", text=reply))

        return {
            "reply": reply,
            "preferences": session.preferences.to_dict(),
            "markup_percent": get_markup_percent(),
            "summary": session.summary,
            "results": session.results,
            "output_path": session.output_path,
            "actions": {
                "reprocessed_items": sorted(numbers_to_reprocess),
                "reprocess_all": reprocess_all,
            },
        }

    @staticmethod
    def _apply_kit_aggregation(results: list[MatchResult]) -> None:
        from src.services.tz_match_service import TZMatchService

        for result in results:
            if not result.is_kit or not result.kit_components:
                continue
            agg_cost, agg_price = TZMatchService._aggregate_kit_components(
                result.kit_components
            )
            if agg_cost is not None:
                result.unit_cost = agg_cost
            if agg_price is not None:
                result.unit_base_price = agg_price

    def _reprocess_items(self, session: KpSession, numbers: set[int]) -> None:
        tz_by_number = {item.number: item for item in session.tz_items}
        for index, result in enumerate(session.results):
            number = result.tz_item.number
            if number not in numbers:
                continue
            tz_item = tz_by_number.get(number)
            if not tz_item:
                continue
            session.results[index] = self.processor.tz_matcher.match_item(
                tz_item,
                use_ai=session.use_ai,
                preferences=session.preferences,
            )

    @staticmethod
    def _next_output_path(current_path: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return current_path.parent / f"KP_{stamp}.xlsx"

    @staticmethod
    def _apply_preferences_to_results(
        results: list[MatchResult],
        preferences: KpPreferences,
    ) -> None:
        for result in results:
            result.comparison = filter_comparison_quotes(
                result.comparison, preferences
            )
            result.competitors = [
                q for q in result.comparison if q.source == "web"
            ]
            KpChatService._reapply_primary_from_comparison(result, preferences)

    @staticmethod
    def _reapply_primary_from_comparison(
        result: MatchResult,
        preferences: KpPreferences,
    ) -> None:
        source_key = result.source.value if result.source else ""
        web_disabled = "web" in preferences.disabled_sources
        needs_reselect = source_key in preferences.disabled_sources or (
            result.source == MatchSource.WEB and web_disabled
        )
        if not needs_reselect:
            return

        source_map = {
            "catalog": MatchSource.CATALOG,
            "price_list": MatchSource.PRICE_LIST,
            "registry": MatchSource.REGISTRY,
            "web": MatchSource.WEB,
        }
        for quote_source in ("catalog", "price_list", "registry", "web"):
            if quote_source in preferences.disabled_sources:
                continue
            if quote_source == "web" and web_disabled:
                continue
            for quote in result.comparison:
                if quote.source != quote_source:
                    continue
                base_price = quote.cost if quote.cost is not None else quote.price
                if base_price is None:
                    continue
                result.source = source_map[quote_source]
                result.matched_name = quote.matched_name or result.matched_name
                result.unit_cost = quote.cost if quote.cost is not None else base_price
                result.unit_base_price = base_price
                result.match_score = max(result.match_score, quote.match_score or 0)
                result.notes = (
                    f"{result.notes} | Пересчёт без {source_key}".strip(" |")
                )
                return

    @staticmethod
    def _items_summary(results: list[MatchResult]) -> list[dict]:
        rows: list[dict] = []
        for result in results:
            rows.append(
                {
                    "number": result.tz_item.number,
                    "name": result.tz_item.name,
                    "quantity": result.tz_item.quantity,
                    "unit": result.tz_item.unit,
                    "status": result.status.value,
                    "matched_name": result.matched_name,
                    "unit_price_kp": result.unit_price,
                    "total_price": result.total_price,
                    "is_kit": result.is_kit,
                    "kit_components_count": len(result.kit_components),
                    "competitor_labels": [
                        q.label for q in result.comparison if q.source == "web"
                    ],
                }
            )
        return rows
