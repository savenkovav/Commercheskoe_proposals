from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from src.config import (
    CATALOG_PATH,
    OUTPUT_DIR,
    REGISTRY_PATH,
)
from src.services.markup_settings import get_markup_percent
from src.services.ai_agent import AIAgent
from src.services.data_loader import (
    load_catalog,
    load_registry,
)
from src.services.tz_parser import parse_tz
from src.services.excel_generator import ExcelGenerator
from src.services.matcher import ItemMatcher
from src.services.models import MatchResult, MatchSource, MatchStatus, ProposalSummary
from src.services.price_list_manager import PriceListManager, get_price_list_manager

logger = logging.getLogger(__name__)


class ProposalProcessor:
    def __init__(self, price_manager: PriceListManager | None = None) -> None:
        self.price_manager = price_manager or get_price_list_manager()
        self.catalog = load_catalog(CATALOG_PATH)
        self.registry = load_registry(REGISTRY_PATH)
        self.price_lists: list = []
        self.ai = AIAgent()
        self.excel = ExcelGenerator()
        self.matcher = ItemMatcher(self.catalog, self.registry, [])

        total_price_items = self.reload_price_lists()

        logger.info(
            "Loaded data: catalog=%s, registry=%s, price_files=%s, price_items=%s",
            len(self.catalog),
            len(self.registry),
            len(self.price_manager.list_entries()),
            total_price_items,
        )

    def reload_price_lists(self) -> int:
        self.price_lists = self.price_manager.load_all_items()
        self.matcher = ItemMatcher(self.catalog, self.registry, self.price_lists)
        return len(self.price_lists)

    def reload_catalog(self) -> int:
        self.catalog = load_catalog(CATALOG_PATH)
        self.matcher = ItemMatcher(self.catalog, self.registry, self.price_lists)
        return len(self.catalog)

    def reload_registry(self) -> int:
        self.registry = load_registry(REGISTRY_PATH)
        self.matcher = ItemMatcher(self.catalog, self.registry, self.price_lists)
        return len(self.registry)

    def process_tz_file(
        self,
        tz_path: Path,
        output_dir: Path | None = None,
        use_ai: bool = True,
        request_number: str = "б/н",
    ) -> tuple[Path, ProposalSummary, list[MatchResult]]:
        start = time.perf_counter()
        tz_items = parse_tz(tz_path)
        if not tz_items:
            raise ValueError("Не удалось извлечь позиции из ТЗ")

        results: list[MatchResult] = []
        for tz_item in tz_items:
            result = self._process_item(tz_item, use_ai=use_ai)
            self._apply_pricing(result)
            results.append(result)

        summary = self._build_summary(results, time.perf_counter() - start)

        out_dir = output_dir or OUTPUT_DIR
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = out_dir / f"KP_{timestamp}.xlsx"

        self.excel.generate(results, summary, output_path, request_number)
        return output_path, summary, results

    def _process_item(self, tz_item, use_ai: bool = True) -> MatchResult:
        local = self.matcher.match_local(tz_item)

        if local and local.status == MatchStatus.EXACT and (
            local.unit_base_price is not None or local.unit_cost is not None
        ):
            return local

        if local and local.match_score >= 85 and (
            local.unit_base_price is not None or local.unit_cost is not None
        ):
            return local

        if not use_ai:
            if local:
                return local
            return MatchResult(
                tz_item=tz_item,
                status=MatchStatus.NOT_FOUND,
                source=MatchSource.NONE,
                notes="Позиция не найдена в каталогах и прайсах",
            )

        candidates = self.matcher.candidates_for_ai(tz_item)
        ai_result = self.ai.match_item(
            tz_item,
            candidates["catalog"],
            candidates["price"],
            candidates["registry"],
        )

        status = AIAgent.parse_status(ai_result.get("status", "not_found"))
        source = AIAgent.parse_source(ai_result.get("source", "none"))
        unit_cost = ai_result.get("unit_cost")
        unit_base_price = ai_result.get("unit_price") or ai_result.get("unit_cost")
        matched_name = ai_result.get("matched_name", "")
        match_score = float(ai_result.get("match_score", 0) or 0)
        notes = ai_result.get("notes", "")
        alternatives = ai_result.get("alternatives") or []

        if status == MatchStatus.NOT_FOUND and source == MatchSource.NONE:
            web_result = self.ai.estimate_web_price(tz_item)
            status = AIAgent.parse_status(web_result.get("status", "not_found"))
            source = AIAgent.parse_source(web_result.get("source", "none"))
            unit_cost = web_result.get("unit_cost")
            unit_base_price = web_result.get("unit_cost")
            matched_name = web_result.get("matched_name", "")
            match_score = float(web_result.get("match_score", 0) or 0)
            notes = web_result.get("notes", notes)
            alternatives = web_result.get("alternatives") or alternatives

        if local and local.match_score > match_score and (
            local.unit_base_price is not None or local.unit_cost is not None
        ):
            return local

        if local:
            if unit_base_price is None:
                unit_base_price = local.unit_base_price
            if unit_cost is None:
                unit_cost = local.unit_cost

        source_detail = self._resolve_source_detail(source, matched_name)

        return MatchResult(
            tz_item=tz_item,
            status=status,
            source=source,
            matched_name=matched_name or (local.matched_name if local else ""),
            match_score=max(match_score, local.match_score if local else 0),
            unit_cost=unit_cost,
            unit_base_price=unit_base_price,
            notes=notes or (local.notes if local else "Не найдено"),
            source_detail=source_detail or (local.source_detail if local else ""),
            alternatives=alternatives or (local.alternatives if local else []),
        )

    @staticmethod
    def _resolve_source_detail(source: MatchSource, matched_name: str) -> str:
        if source == MatchSource.WEB:
            return "Оценка по открытым источникам (AI)"
        if source == MatchSource.CATALOG:
            return f"Каталог: {matched_name}"
        if source == MatchSource.PRICE_LIST:
            return f"Прайс: {matched_name}"
        if source == MatchSource.REGISTRY:
            return f"Реестр: {matched_name}"
        return ""

    @staticmethod
    def _apply_pricing(result: MatchResult) -> None:
        qty = result.tz_item.quantity

        if result.unit_cost is not None:
            result.total_cost = round(result.unit_cost * qty, 2)
        else:
            result.total_cost = None

        if result.unit_base_price is None:
            result.unit_price = None
            result.total_price = None
            return

        multiplier = 1 + get_markup_percent() / 100
        result.unit_price = round(result.unit_base_price * multiplier, 2)
        result.total_price = round(result.unit_price * qty, 2)

    @staticmethod
    def _build_summary(results: list[MatchResult], elapsed: float) -> ProposalSummary:
        exact = sum(1 for r in results if r.status == MatchStatus.EXACT)
        similar = sum(1 for r in results if r.status == MatchStatus.SIMILAR)
        not_found = sum(1 for r in results if r.status == MatchStatus.NOT_FOUND)

        total_cost = sum(r.total_cost or 0 for r in results)
        total_base_price = sum(
            round(r.unit_base_price * r.tz_item.quantity, 2)
            for r in results
            if r.unit_base_price is not None
        )
        total_price = sum(r.total_price or 0 for r in results)

        return ProposalSummary(
            total_items=len(results),
            exact_count=exact,
            similar_count=similar,
            not_found_count=not_found,
            total_cost=total_cost,
            total_base_price=total_base_price,
            total_price=total_price,
            processing_seconds=elapsed,
        )

    @staticmethod
    def format_summary_text(summary: ProposalSummary) -> str:
        return (
            f"📊 *Результат обработки ТЗ*\n\n"
            f"Всего позиций: *{summary.total_items}*\n"
            f"✅ Точных совпадений: *{summary.exact_count}*\n"
            f"⚠️ Похожих (проверить): *{summary.similar_count}*\n"
            f"❌ Не найдено: *{summary.not_found_count}*\n\n"
            f"💰 Себестоимость: *{summary.total_cost:,.2f}* ₽\n"
            f"🏷 Цена без наценки: *{summary.total_base_price:,.2f}* ₽\n"
            f"📈 Цена КП (+{get_markup_percent()}%): *{summary.total_price:,.2f}* ₽\n"
            f"⏱ Время: *{summary.processing_seconds:.1f}* сек"
        ).replace(",", " ")
