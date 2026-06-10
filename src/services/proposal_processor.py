from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from src.config import (
    CATALOG_PATH,
    GOODS_REPORT_PATH,
    KP_PARALLEL_WORKERS,
    OUTPUT_DIR,
    PROCUREMENT_REPORT_PATH,
    REGISTRY_PATH,
    USE_GOODS_REPORT,
)
from src.services.markup_settings import get_markup_percent
from src.services.ai_agent import AIAgent
from src.services.catalog_structure import CatalogStructure
from src.services.data_loader import (
    load_catalog,
    load_goods_report,
    load_registry,
    merge_goods_reports,
)
from src.services.tz_parser import parse_tz
from src.services.excel_generator import ExcelGenerator
from src.services.matcher import ItemMatcher
from src.services.models import MatchResult, MatchStatus, ProposalSummary
from src.services.price_list_manager import PriceListManager, get_price_list_manager
from src.services.tz_match_service import TZMatchService

logger = logging.getLogger(__name__)


class ProposalProcessor:
    def __init__(self, price_manager: PriceListManager | None = None) -> None:
        self.price_manager = price_manager or get_price_list_manager()
        self.catalog = load_catalog(CATALOG_PATH)
        self.registry = load_registry(REGISTRY_PATH)
        self.goods_report = self._load_all_goods_reports()
        self.price_lists: list = []
        self.ai = AIAgent()
        self.excel = ExcelGenerator()
        self.matcher = ItemMatcher(self.catalog, self.registry, [])
        self.catalog_structure = CatalogStructure(self.catalog)
        self.tz_matcher = TZMatchService(
            self.matcher,
            self.ai,
            self.catalog,
            self.goods_report,
            self.catalog_structure,
        )

        total_price_items = self.reload_price_lists()

        logger.info(
            "Loaded data: catalog=%s, goods_report=%s, registry=%s, price_files=%s, price_items=%s",
            len(self.catalog),
            len(self.goods_report),
            len(self.registry),
            len(self.price_manager.list_entries()),
            total_price_items,
        )

    def reload_price_lists(self) -> int:
        self.price_lists = self.price_manager.load_all_items()
        self._rebuild_matcher()
        return len(self.price_lists)

    def reload_catalog(self) -> int:
        self.catalog = load_catalog(CATALOG_PATH)
        self.catalog_structure = CatalogStructure(self.catalog)
        self._rebuild_matcher()
        return len(self.catalog)

    def reload_registry(self) -> int:
        self.registry = load_registry(REGISTRY_PATH)
        self._rebuild_matcher()
        return len(self.registry)

    def reload_goods_report(self) -> int:
        self.goods_report = self._load_all_goods_reports()
        self._rebuild_matcher()
        return len(self.goods_report)

    @staticmethod
    def _load_all_goods_reports() -> list:
        if not USE_GOODS_REPORT:
            return []
        sources = [load_goods_report(GOODS_REPORT_PATH)]
        if PROCUREMENT_REPORT_PATH and PROCUREMENT_REPORT_PATH.exists():
            sources.append(load_goods_report(PROCUREMENT_REPORT_PATH))
        return merge_goods_reports(*sources)

    def _rebuild_matcher(self) -> None:
        self.matcher = ItemMatcher(self.catalog, self.registry, self.price_lists)
        self.tz_matcher = TZMatchService(
            self.matcher,
            self.ai,
            self.catalog,
            self.goods_report,
            self.catalog_structure,
        )

    def process_tz_file(
        self,
        tz_path: Path,
        output_dir: Path | None = None,
        use_ai: bool = True,
        request_number: str = "б/н",
    ) -> tuple[Path, ProposalSummary, list[MatchResult], list]:
        start = time.perf_counter()
        tz_items = parse_tz(tz_path)
        if not tz_items:
            raise ValueError("Не удалось извлечь позиции из ТЗ")

        results = self._process_all_items(tz_items, use_ai=use_ai)

        summary = self._build_summary(results, time.perf_counter() - start)

        out_dir = output_dir or OUTPUT_DIR
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = out_dir / f"KP_{timestamp}.xlsx"

        self.excel.generate(results, summary, output_path, request_number)
        return output_path, summary, results, tz_items

    def _process_item(self, tz_item, use_ai: bool = True) -> MatchResult:
        return self.tz_matcher.match_item(tz_item, use_ai=use_ai)

    def _process_all_items(
        self,
        tz_items: list,
        use_ai: bool = True,
    ) -> list[MatchResult]:
        workers = KP_PARALLEL_WORKERS if len(tz_items) > 1 else 1
        if workers <= 1:
            results: list[MatchResult] = []
            for tz_item in tz_items:
                result = self._process_item(tz_item, use_ai=use_ai)
                self._apply_pricing(result)
                results.append(result)
            return results

        indexed_results: list[MatchResult | None] = [None] * len(tz_items)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._process_item, tz_item, use_ai): index
                for index, tz_item in enumerate(tz_items)
            }
            for future in futures:
                index = futures[future]
                result = future.result()
                self._apply_pricing(result)
                indexed_results[index] = result

        return [result for result in indexed_results if result is not None]

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
