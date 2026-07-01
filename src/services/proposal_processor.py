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
    STOCK_BALANCE_PATH,
    USE_GOODS_REPORT,
)
from src.services.pricing_rules import apply_kp_pricing
from src.services.ai_agent import AIAgent
from src.services.catalog_structure import CatalogStructure
from src.services.data_loader import (
    load_catalog,
    load_goods_report,
    load_registry,
    merge_goods_reports,
    merge_registry,
)
from src.services.tz_parser import parse_tz
from src.services.excel_generator import ExcelGenerator
from src.services.matcher import ItemMatcher
from src.services.meilisearch_service import sync_meilisearch_index
from src.services.models import MatchResult, MatchSource, MatchStatus, ProposalSummary
from src.services.price_list_manager import PriceListManager, get_price_list_manager
from src.services.tz_match_service import TZMatchService

logger = logging.getLogger(__name__)


class ProposalProcessor:
    def __init__(self, price_manager: PriceListManager | None = None) -> None:
        self.price_manager = price_manager or get_price_list_manager()
        self.catalog = load_catalog(CATALOG_PATH)
        self.registry = self._load_all_registry_items()
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
        self.registry = self._load_all_registry_items()
        self._rebuild_matcher()
        return len(self.registry)

    def reload_goods_report(self) -> int:
        self.goods_report = self._load_all_goods_reports()
        self._rebuild_matcher()
        return len(self.goods_report)

    @staticmethod
    def _load_all_registry_items() -> list:
        sources = [load_registry(REGISTRY_PATH)]
        if (
            STOCK_BALANCE_PATH
            and STOCK_BALANCE_PATH.exists()
            and STOCK_BALANCE_PATH.resolve() != REGISTRY_PATH.resolve()
        ):
            sources.append(load_registry(STOCK_BALANCE_PATH))
        return merge_registry(*sources)

    @staticmethod
    def _load_all_goods_reports() -> list:
        if not USE_GOODS_REPORT:
            return []
        sources: list = []
        if STOCK_BALANCE_PATH and STOCK_BALANCE_PATH.exists():
            sources.append(load_goods_report(STOCK_BALANCE_PATH))
        if GOODS_REPORT_PATH.exists():
            sources.append(load_goods_report(GOODS_REPORT_PATH))
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
        self._sync_meilisearch()

    def _sync_meilisearch(self) -> None:
        result = sync_meilisearch_index(self.catalog, self.registry, self.price_lists)
        if result.get("documents"):
            logger.info("Meilisearch synced: %s documents", result["documents"])
        elif result.get("error"):
            logger.warning("Meilisearch sync error: %s", result["error"])

    @staticmethod
    def stub_results(tz_items: list) -> list[MatchResult]:
        from src.services.models import MatchResult, MatchSource, MatchStatus

        return [
            MatchResult(
                tz_item=item,
                status=MatchStatus.NOT_FOUND,
                source=MatchSource.NONE,
                notes="Ожидает поиска по команде пользователя",
            )
            for item in tz_items
        ]

    @staticmethod
    def empty_summary(item_count: int) -> ProposalSummary:
        return ProposalSummary(
            total_items=item_count,
            exact_count=0,
            similar_count=0,
            not_found_count=item_count,
            total_cost=0.0,
            total_base_price=0.0,
            total_price=0.0,
            processing_seconds=0.0,
        )

    def parse_tz_file(self, tz_path: Path) -> list:
        tz_items = parse_tz(tz_path)
        if not tz_items:
            raise ValueError("Не удалось извлечь позиции из ТЗ")
        return tz_items

    def search_tz_items(
        self,
        tz_items: list,
        use_ai: bool = True,
        preferences=None,
        *,
        include_web: bool = True,
        numbers: set[int] | None = None,
    ) -> list[MatchResult]:
        from src.services.kp_preferences import KpPreferences

        targets = tz_items
        if numbers is not None:
            targets = [item for item in tz_items if item.number in numbers]

        prefs = preferences or KpPreferences()
        if not include_web and "web" not in prefs.disabled_sources:
            prefs.disabled_sources = [*prefs.disabled_sources, "web"]

        if len(targets) <= 1:
            results: list[MatchResult] = []
            for tz_item in targets:
                result = self.tz_matcher.match_item(
                    tz_item,
                    use_ai=use_ai,
                    preferences=prefs,
                )
                self._apply_pricing(result)
                results.append(result)
            return results

        workers = min(KP_PARALLEL_WORKERS, len(targets))
        logger.info(
            "TZ search start: items=%s workers=%s include_web=%s",
            len(targets),
            workers,
            include_web,
        )
        started = time.perf_counter()
        indexed_results: list[MatchResult | None] = [None] * len(targets)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self.tz_matcher.match_item,
                    tz_item,
                    use_ai,
                    prefs,
                ): index
                for index, tz_item in enumerate(targets)
            }
            for future in futures:
                index = futures[future]
                result = future.result()
                self._apply_pricing(result)
                indexed_results[index] = result

        return [result for result in indexed_results if result is not None]

    def process_tz_file(
        self,
        tz_path: Path,
        output_dir: Path | None = None,
        use_ai: bool = True,
        request_number: str = "б/н",
        *,
        include_web: bool = True,
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
        apply_kp_pricing(result)

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

