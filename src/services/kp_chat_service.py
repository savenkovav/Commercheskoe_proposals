from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from src.config import OUTPUT_DIR, SEARCH_KIT_COMPONENT_LINKS
from src.services.assistant_intent import detect_assistant_intent
from src.services.kp_preferences import KpPreferences, filter_comparison_quotes
from src.services.kp_session import ChatTurn, KpSession, get_kp_session_store
from src.services.markup_settings import get_markup_percent, set_markup_percent
from src.services.models import MatchResult, MatchSource, ProposalSummary
from src.services.product_lookup import (
    ProductLookupService,
    kp_lookup_reply,
    resolve_freeform_product_lookup,
)
from src.services.proposal_processor import ProposalProcessor
from src.services.tz_rag_service import RagIndex, TZRagService

logger = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "Я КП-Ассистент. Напишите название товара — найду в каталоге, прайсах и реестре.\n\n"
    "Для расчёта КП по ТЗ загрузите файл .docx/.pdf/.xlsx или опишите несколько позиций.\n"
    "Выберите задачу:\n"
    "• Задача 1 — поиск и себестоимость\n"
    "• Задача 1+2 — плюс конкуренты и рекомендация цены"
)

PARSED_MESSAGE = (
    "ТЗ разобрано: {count} позиций.\n"
    "Поиск в каталогах и прайсах выполнен. Для позиций без совпадения "
    "подобраны цены из интернета (−5% от найденной стоимости)."
)


class KpChatService:
    def __init__(self, processor: ProposalProcessor) -> None:
        self.processor = processor
        self.store = get_kp_session_store()
        self.lookup = ProductLookupService(
            processor.matcher,
            processor.ai,
            processor.tz_matcher.web_search,
        )
        self.rag = TZRagService(processor.ai)

    def create_session(
        self,
        tz_items: list,
        results: list[MatchResult],
        summary: ProposalSummary,
        output_path: Path | None,
        use_ai: bool,
        *,
        tz_filename: str = "",
        parsed_only: bool = False,
        auto_searched: bool = False,
        rag_index: RagIndex | None = None,
    ) -> str:
        from src.services.kp_session import new_session_id

        preferences = KpPreferences(
            search_kit_component_links=SEARCH_KIT_COMPONENT_LINKS,
        )
        session = KpSession(
            session_id="",
            tz_items=tz_items,
            results=results,
            summary=summary,
            output_path=output_path or (OUTPUT_DIR / "pending.xlsx"),
            use_ai=use_ai,
            preferences=preferences,
            task_mode="task1",
            stage="parsed" if parsed_only else "searched",
            search_completed=not parsed_only,
            tz_filename=tz_filename,
            rag_chunks=(rag_index.chunks if rag_index else []),
            rag_vectors=(rag_index.vectors if rag_index else []),
        )
        session.session_id = new_session_id()
        if auto_searched or not parsed_only:
            greeting = PARSED_MESSAGE.format(count=len(tz_items))
        elif parsed_only:
            greeting = (
                "ТЗ разобрано: {count} позиций.\n"
                "Поиск ещё не запускался. Нажмите «Только поиск» "
                "или напишите «найди в каталогах»."
            ).format(count=len(tz_items))
        else:
            greeting = WELCOME_MESSAGE
        session.chat_history.append(ChatTurn(role="assistant", text=greeting))
        return self.store.create(session)

    def create_free_session(self, *, use_ai: bool = True) -> str:
        from src.services.kp_session import new_session_id

        session = KpSession(
            session_id="",
            tz_items=[],
            results=[],
            summary=self.processor.empty_summary(0),
            output_path=OUTPUT_DIR / "pending.xlsx",
            use_ai=use_ai,
            preferences=KpPreferences(
                search_kit_component_links=SEARCH_KIT_COMPONENT_LINKS,
            ),
            task_mode="task1",
            stage="intake",
            search_completed=False,
        )
        session.session_id = new_session_id()
        session.chat_history.append(ChatTurn(role="assistant", text=WELCOME_MESSAGE))
        session_id = self.store.create(session)
        logger.info("KP free session created: %s", session_id)
        return session_id

    def chat(self, session_id: str, message: str) -> dict:
        session = self.store.get(session_id)
        if not session:
            raise ValueError("Сессия не найдена. Загрузите ТЗ заново.")

        text = message.strip()
        if not text:
            raise ValueError("Введите сообщение")

        logger.info(
            "KP chat session=%s tz_items=%s message=%r",
            session_id,
            len(session.tz_items),
            text[:120],
        )
        started = time.perf_counter()

        session.chat_history.append(ChatTurn(role="user", text=text))

        lookup_query = resolve_freeform_product_lookup(text, session.tz_items)
        lookup_result = None
        if lookup_query:
            logger.info(
                "KP chat lookup product=%r fields=%s",
                lookup_query.product_name,
                [field.value for field in lookup_query.requested_fields],
            )
            lookup_result = self.lookup.lookup(
                lookup_query.product_name,
                lookup_query.requested_fields,
            )

        items_summary = self._items_summary(session.results, session.tz_items)
        if self.processor.ai.enabled:
            rag_context = self.rag.retrieve_context(
                text,
                RagIndex(
                    chunks=session.rag_chunks,
                    vectors=session.rag_vectors,
                ),
            )
            patch = self.processor.ai.interpret_assistant_message(
                user_message=text,
                items_summary=items_summary,
                preferences=session.preferences.to_dict(),
                chat_history=[
                    {"role": turn.role, "text": turn.text}
                    for turn in session.chat_history[-12:]
                ],
                markup_percent=get_markup_percent(),
                task_mode=session.task_mode,
                stage=session.stage,
                search_completed=session.search_completed,
                rag_context=rag_context,
            )
        else:
            patch = detect_assistant_intent(
                text,
                has_items=bool(session.tz_items),
                search_completed=session.search_completed,
            )

        reply = str(patch.get("reply") or "Понял.")
        if lookup_result is not None:
            reply = kp_lookup_reply(lookup_result)
            patch["run_local_search"] = False
            patch["run_web_search"] = False
            patch["generate_excel"] = False
            patch["reprocess_items"] = []
            patch["reprocess_all"] = False

        if patch.get("task_mode") in ("task1", "task1_task2"):
            session.task_mode = patch["task_mode"]

        if patch.get("save_rule"):
            session.preferences.add_rule(str(patch["save_rule"]))
            reply = f"Правило сохранено: {patch['save_rule']}"

        prefs_before = session.preferences.to_dict()
        session.preferences.merge_ai_patch(patch)
        preferences_changed = session.preferences.to_dict() != prefs_before

        markup_value = patch.get("markup_percent")
        markup_changed = False
        if markup_value is not None:
            try:
                set_markup_percent(float(markup_value))
                markup_changed = True
            except ValueError:
                pass

        numbers_to_reprocess: set[int] = set()
        for raw in patch.get("reprocess_items") or []:
            try:
                numbers_to_reprocess.add(int(raw))
            except (TypeError, ValueError):
                continue
        if patch.get("reprocess_all"):
            numbers_to_reprocess = {item.number for item in session.tz_items}

        run_local = bool(patch.get("run_local_search"))
        run_web = bool(patch.get("run_web_search"))
        generate_excel = bool(patch.get("generate_excel"))

        if run_local or numbers_to_reprocess or (preferences_changed and session.search_completed):
            include_web = True
            if not numbers_to_reprocess and session.tz_items:
                numbers_to_reprocess = {item.number for item in session.tz_items}
            self._run_search(
                session,
                numbers_to_reprocess,
                include_web=include_web,
            )

        if session.preferences.force_kit_component_pricing:
            self._apply_kit_aggregation(session.results)

        self._apply_preferences_to_results(session.results, session.preferences)

        excel_generated = False
        if generate_excel and session.search_completed:
            excel_generated = True
            self._generate_excel(session)
        elif session.search_completed and (run_local or numbers_to_reprocess):
            excel_generated = True
            self._generate_excel(session)
        elif markup_changed and session.search_completed:
            for result in session.results:
                self.processor._apply_pricing(result)
            session.summary = self.processor._build_summary(session.results, 0.0)
            excel_generated = True
            self._generate_excel(session)

        session.chat_history.append(ChatTurn(role="assistant", text=reply))

        response: dict = {
            "reply": reply,
            "preferences": session.preferences.to_dict(),
            "markup_percent": get_markup_percent(),
            "summary": session.summary,
            "results": session.results,
            "output_path": session.output_path,
            "task_mode": session.task_mode,
            "stage": session.stage,
            "search_completed": session.search_completed,
            "actions": {
                "reprocessed_items": sorted(numbers_to_reprocess),
                "reprocess_all": bool(patch.get("reprocess_all")),
                "run_local_search": run_local,
                "run_web_search": run_web,
                "generate_excel": excel_generated,
            },
        }
        if lookup_result is not None:
            response["lookup"] = lookup_result
        logger.info(
            "KP chat done session=%s lookup=%s local=%s web=%s excel=%s %.0fms",
            session_id,
            lookup_result is not None,
            run_local,
            run_web,
            excel_generated,
            (time.perf_counter() - started) * 1000,
        )
        return response

    def _run_search(
        self,
        session: KpSession,
        numbers: set[int],
        *,
        include_web: bool,
    ) -> None:
        start = time.perf_counter()
        searched = self.processor.search_tz_items(
            session.tz_items,
            use_ai=session.use_ai,
            preferences=session.preferences,
            include_web=include_web,
            numbers=numbers,
        )
        by_number = {item.number: item for item in session.tz_items}
        result_by_number = {r.tz_item.number: r for r in session.results}

        for result in searched:
            result_by_number[result.tz_item.number] = result

        session.results = [
            result_by_number.get(
                num,
                self.processor.stub_results([by_number[num]])[0],
            )
            for num in sorted(by_number)
        ]
        session.search_completed = True
        session.stage = "searched"
        session.summary = self.processor._build_summary(
            session.results,
            time.perf_counter() - start,
        )

    def _generate_excel(self, session: KpSession) -> None:
        for result in session.results:
            self.processor._apply_pricing(result)
        session.output_path = self._next_output_path(session.output_path)
        self.processor.excel.generate(
            session.results,
            session.summary,
            session.output_path,
            preferences=session.preferences,
        )
        session.stage = "exported"

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
                result.internet_priced = quote_source == "web"
                result.matched_name = quote.matched_name or result.matched_name
                result.unit_cost = quote.cost if quote.cost is not None else base_price
                result.unit_base_price = base_price
                result.match_score = max(result.match_score, quote.match_score or 0)
                result.notes = (
                    f"{result.notes} | Пересчёт без {source_key}".strip(" |")
                )
                return

    @staticmethod
    def _items_summary(
        results: list[MatchResult],
        tz_items: list,
    ) -> list[dict]:
        if not tz_items:
            return []

        result_by_number = {r.tz_item.number: r for r in results}
        rows: list[dict] = []
        for item in tz_items:
            result = result_by_number.get(item.number)
            rows.append(
                {
                    "number": item.number,
                    "name": item.name,
                    "quantity": item.quantity,
                    "unit": item.unit,
                    "specifications": (item.specifications or "")[:200],
                    "status": result.status.value if result else "pending",
                    "matched_name": result.matched_name if result else "",
                    "unit_price_kp": result.unit_price if result else None,
                    "search_pending": (
                        result.notes == "Ожидает поиска по команде пользователя"
                        if result
                        else True
                    ),
                }
            )
        return rows
