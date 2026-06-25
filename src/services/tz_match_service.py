from __future__ import annotations

import logging

from rapidfuzz import process

from src.config import (
    EXACT_MATCH_THRESHOLD,
    LOCAL_MATCH_THRESHOLD,
    SIMILAR_MATCH_THRESHOLD,
    USE_AI_INTERNET_SEARCH,
    WEB_PRICE_DISCOUNT_PERCENT,
    WEB_SEARCH_ENABLED,
    WEB_SEARCH_EXACT_THRESHOLD,
)
from src.services.ai_agent import AIAgent
from src.services.catalog_structure import CatalogStructure
from src.services.data_loader import format_catalog_supplier, normalize_name
from src.config import SEARCH_KIT_COMPONENT_LINKS
from src.services.kp_preferences import KpPreferences, filter_web_quotes
from src.services.competitor_urls import (
    build_marketplace_search_url,
    competitor_urls_for_item,
    resolve_competitor_url,
)
from src.services.fuzzy_scoring import name_match_score
from src.services.kit_spec_parser import parse_kit_components_from_specs
from src.services.local_price_match import has_local_catalog_or_price_list_price
from src.services.match_tier import effective_score, meets_floor, resolve_local_floor
from src.services.tz_search import (
    combined_match_score,
    is_relevant_match,
    primary_search_text,
    product_type_conflict,
    relevance_score,
    tz_match_query,
)
from src.services.web_quote_priority import (
    enrich_source_detail_with_price_url,
    has_acceptable_web_pricing_in_comparison,
    has_unpriced_competitor_display_quote,
    is_acceptable_web_pricing_quote,
    is_competitor_url,
    is_marketplace_url,
    is_product_page_url,
    is_search_listing_url,
    pick_best_web_priced_quote,
    pick_internet_url,
    pick_marketplace_priced_quote,
    resolve_price_source_url,
    sort_web_quotes,
    web_quote_rank_key,
)
from src.services.web_search_service import WebSearchService
from src.services.matcher import FuzzyHit, ItemMatcher
from src.services.models import (
    CatalogItem,
    GoodsReportItem,
    KitComponentLine,
    MatchResult,
    MatchSource,
    MatchStatus,
    PriceListItem,
    PriceQuote,
    RegistryItem,
    TZItem,
)

logger = logging.getLogger(__name__)


class TZMatchService:
    def __init__(
        self,
        matcher: ItemMatcher,
        ai: AIAgent,
        catalog: list[CatalogItem],
        goods_report: list[GoodsReportItem],
        catalog_structure: CatalogStructure,
    ) -> None:
        self.matcher = matcher
        self.ai = ai
        self.catalog = catalog
        self.goods_report = goods_report
        self.catalog_structure = catalog_structure
        self.web_search = WebSearchService()
        self._goods_names = [normalize_name(item.name) for item in goods_report]

    def match_item(
        self,
        tz_item: TZItem,
        use_ai: bool = True,
        preferences: KpPreferences | None = None,
    ) -> MatchResult:
        prefs = preferences or KpPreferences(
            search_kit_component_links=SEARCH_KIT_COMPONENT_LINKS,
        )
        candidates = self.matcher.find_candidates(tz_item)
        local_floor = self._compute_local_floor(tz_item, candidates)

        catalog_hit = self._pick_validated_hit(
            candidates["catalog"], tz_item, use_ai, min_score=local_floor
        )
        direct_catalog = self._find_direct_catalog_hit(
            tz_item, use_ai=use_ai, min_score=local_floor
        )
        if direct_catalog and (
            catalog_hit is None or direct_catalog.score > catalog_hit.score
        ):
            catalog_hit = direct_catalog
        if catalog_hit and self.matcher.is_distinctive_mismatch(
            tz_item.name, catalog_hit.name
        ):
            catalog_hit = None
        price_hit = self._pick_validated_hit(
            candidates["price"], tz_item, use_ai, min_score=local_floor
        )
        registry_hit = self._pick_validated_hit(
            candidates["registry"], tz_item, use_ai, min_score=local_floor
        )
        goods_hit = self._match_goods_report(tz_item, min_score=local_floor)

        comparison: list[PriceQuote] = []
        kit_components: list[KitComponentLine] = []
        supplier: str | None = None
        purchase_date: str | None = None
        is_kit = False
        tz_kit_names = parse_kit_components_from_specs(tz_item.specifications)
        if (
            catalog_hit
            and local_floor is not None
            and is_relevant_match(
                tz_item,
                catalog_hit.name,
                score=catalog_hit.score,
                min_score=local_floor,
            )
            and effective_score(tz_item, catalog_hit.name, catalog_hit.score) >= local_floor
        ):
            tz_kit_names = []

        if tz_kit_names:
            kit_components = self._build_kit_from_tz_components(tz_kit_names)
            is_kit = True
        if catalog_hit and not kit_components:
            catalog_item = catalog_hit.payload
            if isinstance(catalog_item, CatalogItem):
                kit_components = self.catalog_structure.kit_breakdown(catalog_item)
                is_kit = catalog_item.entry_type in {"kit_total", "sub_kit"} and bool(
                    kit_components
                )

        if catalog_hit:
            comparison.append(
                self._quote_from_catalog(catalog_hit, kit_components)
            )

        if goods_hit:
            supplier = goods_hit.supplier
            purchase_date = goods_hit.purchase_date
            comparison.append(self._quote_from_goods_report(goods_hit))

        if catalog_hit and isinstance(catalog_hit.payload, CatalogItem):
            catalog_supplier = format_catalog_supplier(catalog_hit.payload)
            if catalog_supplier:
                supplier = catalog_supplier

        if price_hit:
            comparison.append(self._quote_from_price(price_hit))

        if registry_hit:
            comparison.append(self._quote_from_registry(registry_hit))

        price_list_check = self._price_by_supplier(tz_item.name, supplier)
        if price_list_check:
            comparison.append(price_list_check)

        for extra_price in self._extra_price_quotes(
            candidates["price"],
            tz_item.name,
            min_score=local_floor,
        ):
            if not any(
                q.source == "price_list" and q.matched_name == extra_price.matched_name
                for q in comparison
            ):
                comparison.append(extra_price)

        local_miss = local_floor is None
        skip_competitors = local_floor is not None and has_local_catalog_or_price_list_price(
            tz_item,
            catalog_hit,
            price_hit,
            self.matcher,
            min_score=local_floor,
        )
        internet_searched = (
            local_miss and WEB_SEARCH_ENABLED and not skip_competitors
        )
        web_quote, competitors = self._fetch_internet_comparison(
            tz_item,
            prefs,
            use_ai=use_ai,
            local_miss=local_miss,
            skip_competitors=skip_competitors,
        )
        comparison.extend(competitors)
        comparison = self._filter_comparison_quotes(tz_item, comparison)

        primary = self._resolve_primary_match(
            tz_item,
            catalog_hit,
            price_hit,
            registry_hit,
            goods_hit,
            web_quote,
            kit_components,
            use_ai=use_ai,
            candidates=candidates,
            local_miss=local_miss,
            local_floor=local_floor,
        )

        if primary.supplier is None and supplier:
            primary.supplier = supplier
        if primary.purchase_date is None and purchase_date:
            primary.purchase_date = purchase_date
        primary.comparison = comparison
        primary.competitors = [q for q in comparison if q.source == "web"]
        primary.kit_components = self._enrich_kit_components(
            kit_components, supplier, prefs
        )
        primary.price_list_check = price_list_check
        primary.is_kit = is_kit

        if price_list_check and primary.supplier:
            primary.notes = (
                f"{primary.notes} | Прайс ({primary.supplier}): "
                f"{price_list_check.price or '—'} ₽"
            ).strip(" |")

        if primary.is_kit and primary.kit_components:
            if prefs.force_kit_component_pricing or tz_kit_names:
                agg_cost, agg_price = self._aggregate_kit_components(
                    primary.kit_components
                )
                if agg_cost is not None:
                    primary.unit_cost = agg_cost
                if agg_price is not None:
                    primary.unit_base_price = agg_price
                if tz_kit_names and (agg_cost is not None or agg_price is not None):
                    self._apply_kit_composition_metadata(primary)

        if tz_kit_names and primary.is_kit:
            priced = sum(
                1
                for line in primary.kit_components
                if line.unit_cost is not None or line.unit_price is not None
            )
            primary.notes = (
                f"{primary.notes} | Состав из ТЗ: {len(tz_kit_names)} поз., "
                f"с ценой: {priced}"
            ).strip(" |")

        primary.comparison = filter_web_quotes(primary.comparison, prefs)
        primary.competitors = [q for q in primary.comparison if q.source == "web"]
        self._finalize_result_pricing(
            primary,
            use_ai=use_ai,
            prefs=prefs,
            skip_internet_search=internet_searched,
        )
        self._ensure_internet_comparison(primary)
        self._normalize_result_metadata(primary)
        if local_miss and not skip_competitors:
            self._ensure_mandatory_internet_pricing(
                primary,
                prefs,
                use_ai=use_ai,
                skip_internet_search=internet_searched,
            )
        primary.comparison = filter_web_quotes(primary.comparison, prefs)
        primary.comparison = sort_web_quotes(primary.comparison)
        primary.competitors = [q for q in primary.comparison if q.source == "web"]
        self._ensure_internet_comparison(primary)
        self._promote_internet_status(primary)
        self._ensure_internet_source_detail(primary)
        primary.competitors = [q for q in primary.comparison if q.source == "web"]
        return primary

    def _apply_kit_composition_metadata(self, result: MatchResult) -> None:
        if not result.kit_components:
            return
        in_catalog = sum(1 for line in result.kit_components if line.found_in_catalog)
        total = len(result.kit_components)
        result.status = MatchStatus.SIMILAR
        result.source = MatchSource.CATALOG
        result.internet_priced = False
        if not result.matched_name:
            result.matched_name = result.tz_item.name
        result.match_score = max(
            result.match_score,
            round(in_catalog / total * 100, 1) if total else 0,
        )
        result.source_detail = (
            f"Комплект по составу ТЗ ({in_catalog}/{total} поз. в каталоге)"
        )
        note = f"Себестоимость по составу комплекта: {total} поз."
        result.notes = note if not result.notes else f"{result.notes} | {note}"

    def _normalize_result_metadata(self, result: MatchResult) -> None:
        has_price = result.unit_base_price is not None or result.unit_cost is not None
        if not has_price:
            return

        if result.status != MatchStatus.NOT_FOUND and result.source != MatchSource.NONE:
            if result.internet_priced and result.source != MatchSource.WEB:
                result.source = MatchSource.WEB
            if result.internet_priced or result.source == MatchSource.WEB:
                self._ensure_internet_source_detail(result)
            return

        if (
            result.is_kit
            and result.kit_components
            and any(
                line.unit_cost is not None or line.unit_price is not None
                for line in result.kit_components
            )
        ):
            self._apply_kit_composition_metadata(result)
            return

        best_local = self._best_local_priced_quote(result.comparison, result.tz_item)
        if best_local:
            self._apply_priced_quote(result, best_local)
            return

        best_web = self._best_web_priced_quote(result.comparison)
        if best_web:
            self._apply_priced_quote(result, best_web)
            return

        if result.internet_priced or any(
            q.source == "web" and (q.price or q.cost) for q in result.comparison
        ):
            result.source = MatchSource.WEB
            result.status = (
                MatchStatus.EXACT
                if result.match_score >= EXACT_MATCH_THRESHOLD
                else MatchStatus.SIMILAR
            )
            if not result.matched_name:
                result.matched_name = result.tz_item.name
            if not result.source_detail:
                result.source_detail = "Интернет"
            self._ensure_internet_source_detail(result)
            return

        result.status = MatchStatus.SIMILAR
        if result.source == MatchSource.NONE:
            result.source = MatchSource.CATALOG
        if not result.matched_name:
            result.matched_name = result.tz_item.name
        if not result.source_detail:
            result.source_detail = "Подбор по сравнению"

    @staticmethod
    def _is_product_web_url(url: str | None) -> bool:
        if not url:
            return False
        lower = url.lower()
        return "search" not in lower and "catalog/0/search" not in lower

    def _filter_comparison_quotes(
        self,
        tz_item: TZItem,
        quotes: list[PriceQuote],
    ) -> list[PriceQuote]:
        filtered: list[PriceQuote] = []
        for quote in quotes:
            name = quote.matched_name or ""
            if name and (
                product_type_conflict(tz_item, name)
                or self.matcher.is_distinctive_mismatch(tz_item.name, name)
            ):
                continue
            filtered.append(quote)
        return filtered

    def _filter_acceptable_web_quotes(
        self,
        tz_item: TZItem,
        quotes: list[PriceQuote],
    ) -> list[PriceQuote]:
        accepted: list[PriceQuote] = []
        for quote in quotes:
            name = quote.matched_name or ""
            if not name:
                accepted.append(quote)
                continue
            if product_type_conflict(tz_item, name):
                continue
            if self.matcher.is_distinctive_mismatch(tz_item.name, name):
                continue
            min_score = (
                LOCAL_MATCH_THRESHOLD
                if quote.url and is_competitor_url(quote.url)
                else WEB_SEARCH_EXACT_THRESHOLD
            )
            if not is_relevant_match(
                tz_item,
                name,
                score=float(quote.match_score or 0),
                min_score=min_score,
            ):
                continue
            if not is_acceptable_web_pricing_quote(quote):
                continue
            accepted.append(quote)
        return accepted

    def _ensure_internet_comparison(self, result: MatchResult) -> None:
        web_quotes = [q for q in result.comparison if q.source == "web"]
        if result.internet_priced and result.unit_base_price is not None:
            priced = [
                q
                for q in web_quotes
                if q.price is not None or q.cost is not None
            ]
            if not priced:
                result.comparison.insert(
                    0,
                    PriceQuote(
                        source="web",
                        label=result.source_detail or "Интернет",
                        matched_name=result.matched_name or result.tz_item.name,
                        price=result.unit_base_price,
                        cost=result.unit_base_price,
                        match_score=result.match_score,
                        url=self._pick_internet_url(
                            web_quotes,
                            unit_base_price=result.unit_base_price,
                        ),
                        notes=(
                            f"Подобрано из интернета | Цена КП: "
                            f"−{WEB_PRICE_DISCOUNT_PERCENT}%"
                        ),
                    ),
                )
                return

            best = pick_best_web_priced_quote(priced) or priced[0]
            if not best.url:
                best.url = self._pick_internet_url(
                    web_quotes,
                    unit_base_price=result.unit_base_price,
                )
            if best.price is None and best.cost is None:
                best.price = result.unit_base_price
                best.cost = result.unit_base_price

        if not web_quotes and result.internet_priced:
            result.comparison.insert(
                0,
                PriceQuote(
                    source="web",
                    label=result.source_detail or "Интернет",
                    matched_name=result.matched_name or result.tz_item.name,
                    price=result.unit_base_price,
                    cost=result.unit_base_price,
                    match_score=result.match_score,
                    notes=result.notes or "Подобрано из интернета",
                ),
            )

    @staticmethod
    def _pick_internet_url(
        web_quotes: list[PriceQuote],
        *,
        unit_base_price: float | None = None,
    ) -> str | None:
        return pick_internet_url(web_quotes, unit_base_price=unit_base_price)

    def _compute_local_floor(
        self,
        tz_item: TZItem,
        candidates: dict,
    ) -> float | None:
        scores: list[float] = []
        for key in ("price", "catalog", "registry"):
            for hit in candidates.get(key, []):
                if self.matcher.is_distinctive_mismatch(tz_item.name, hit.name):
                    continue
                score = effective_score(tz_item, hit.name, hit.score)
                if is_relevant_match(
                    tz_item,
                    hit.name,
                    score=hit.score,
                    min_score=SIMILAR_MATCH_THRESHOLD,
                ):
                    scores.append(score)

        goods_candidate = self._match_goods_report(
            tz_item,
            min_score=SIMILAR_MATCH_THRESHOLD,
        )
        if goods_candidate:
            scores.append(relevance_score(tz_item, goods_candidate.name))

        return resolve_local_floor(scores)

    def _has_confident_local_match(
        self,
        tz_item: TZItem,
        catalog_hit: FuzzyHit | None,
        price_hit: FuzzyHit | None,
        goods_hit: GoodsReportItem | None,
    ) -> bool:
        for hit in (catalog_hit, price_hit):
            if not hit or hit.score < LOCAL_MATCH_THRESHOLD:
                continue
            if not is_relevant_match(tz_item, hit.name, score=hit.score):
                continue
            if self.matcher.is_distinctive_mismatch(tz_item.name, hit.name):
                continue
            return True
        if goods_hit and (goods_hit.cost is not None or goods_hit.price is not None):
            if is_relevant_match(tz_item, goods_hit.name):
                return True
        return False

    def _fetch_internet_comparison(
        self,
        tz_item: TZItem,
        preferences: KpPreferences,
        use_ai: bool = True,
        local_miss: bool = True,
        skip_competitors: bool = False,
    ) -> tuple[PriceQuote | None, list[PriceQuote]]:
        if "web" in preferences.disabled_sources:
            return None, []

        quotes: list[PriceQuote] = []
        seen_urls: set[str] = set()

        def _append_quotes(new_quotes: list[PriceQuote]) -> None:
            for quote in new_quotes:
                if quote.url and quote.url in seen_urls:
                    continue
                if quote.url:
                    seen_urls.add(quote.url)
                quotes.append(quote)

        if local_miss and WEB_SEARCH_ENABLED and not skip_competitors:
            search_text = primary_search_text(tz_item)
            _append_quotes(
                self.web_search.search_internet_cascade(
                    search_text,
                    skip_competitors=skip_competitors,
                    competitor_fallback=False,
                )
            )

        if USE_AI_INTERNET_SEARCH and use_ai and self.ai.enabled:
            _append_quotes(self._fetch_internet_comparison_ai(tz_item))

        quotes = filter_web_quotes(quotes, preferences)
        quotes = self._filter_acceptable_web_quotes(tz_item, quotes)
        quotes = sort_web_quotes(quotes)
        web_quote = self._pick_best_web_quote(quotes)
        return web_quote, quotes

    def _pick_best_web_quote(self, quotes: list[PriceQuote]) -> PriceQuote | None:
        return self._best_web_priced_quote(quotes)

    def _fetch_internet_comparison_fast(
        self,
        tz_item: TZItem,
        preferences: KpPreferences,
    ) -> tuple[PriceQuote | None, list[PriceQuote]]:
        platform_labels = ("Ozon", "Яндекс.Маркет", "Wildberries")
        urls = competitor_urls_for_item([], tz_item.name, limit=3)
        quotes: list[PriceQuote] = []
        for index, url in enumerate(urls):
            platform = platform_labels[index] if index < len(platform_labels) else "Интернет"
            quotes.append(
                PriceQuote(
                    source="web",
                    label=f"Интернет: {platform}",
                    matched_name=tz_item.name,
                    url=url,
                    notes="Поисковая ссылка",
                )
            )
        quotes = filter_web_quotes(quotes, preferences)
        web_quote = quotes[0] if quotes else None
        return web_quote, quotes

    def _fetch_internet_comparison_ai(
        self,
        tz_item: TZItem,
    ) -> list[PriceQuote]:
        if not self.ai.enabled:
            return []

        quotes: list[PriceQuote] = []
        for offer in self.ai.search_competitors(tz_item, limit=3):
            match_score = float(offer.get("match_score", 0) or 0)
            if match_score < WEB_SEARCH_EXACT_THRESHOLD:
                continue
            platform = str(offer.get("platform") or "Интернет")
            matched_name = str(offer.get("name") or tz_item.name)
            quotes.append(
                PriceQuote(
                    source="web",
                    label=f"Интернет: {platform}",
                    matched_name=matched_name,
                    price=float(offer["price"]) if offer.get("price") is not None else None,
                    match_score=match_score,
                    url=resolve_competitor_url(
                        platform,
                        matched_name,
                        offer.get("url"),
                    ),
                    notes=str(offer.get("notes") or ""),
                )
            )

        if not quotes:
            web_result = self.ai.estimate_web_price(tz_item)
            match_score = float(web_result.get("match_score", 0) or 0)
            if (
                web_result.get("unit_cost") is not None
                and match_score >= WEB_SEARCH_EXACT_THRESHOLD
            ):
                url = None
                urls = competitor_urls_for_item([], tz_item.name, limit=1)
                if urls and not is_search_listing_url(urls[0]):
                    url = urls[0]
                quotes.append(
                    PriceQuote(
                        source="web",
                        label="Интернет (оценка рынка)",
                        matched_name=str(web_result.get("matched_name") or ""),
                        cost=float(web_result["unit_cost"]),
                        price=float(web_result["unit_cost"]),
                        match_score=match_score,
                        url=url,
                        notes=str(web_result.get("notes") or ""),
                    )
                )

        return quotes

    def _extra_price_quotes(
        self,
        hits: list[FuzzyHit],
        query: str,
        *,
        min_score: float | None = None,
    ) -> list[PriceQuote]:
        if min_score is None:
            return []
        quotes: list[PriceQuote] = []
        seen_suppliers: set[str] = set()
        ranked = sorted(hits, key=lambda hit: hit.score, reverse=True)
        for hit in ranked[:5]:
            if hit.score < min_score:
                continue
            item: PriceListItem = hit.payload
            supplier_key = (item.supplier or "").lower()
            if supplier_key in seen_suppliers:
                continue
            seen_suppliers.add(supplier_key)
            quotes.append(self._quote_from_price(hit))
            if len(quotes) >= 3:
                break
        return quotes

    def _match_catalog_component(self, name: str) -> FuzzyHit | None:
        mini = TZItem(number=0, name=name, unit="шт", quantity=1)
        candidates = self.matcher.find_candidates(mini)
        catalog_hit = self._pick_hit(candidates["catalog"], mini)
        if not catalog_hit:
            return None
        if self.matcher.is_distinctive_mismatch(name, catalog_hit.name):
            return None
        if catalog_hit.score < LOCAL_MATCH_THRESHOLD:
            return None
        if not isinstance(catalog_hit.payload, CatalogItem):
            return None
        return catalog_hit

    def _goods_for_catalog_match(
        self,
        catalog_hit: FuzzyHit,
        tz_name: str,
    ) -> GoodsReportItem | None:
        catalog_item = TZItem(number=0, name=catalog_hit.name, unit="шт", quantity=1)
        goods_hit = self._match_goods_report(catalog_item)
        if goods_hit:
            return goods_hit
        return self._match_goods_report(
            TZItem(number=0, name=tz_name, unit="шт", quantity=1)
        )

    def _build_kit_from_tz_components(self, names: list[str]) -> list[KitComponentLine]:
        lines: list[KitComponentLine] = []
        for name in names:
            catalog_hit = self._match_catalog_component(name)
            unit_cost: float | None = None
            unit_price: float | None = None
            supplier: str | None = None
            purchase_date: str | None = None
            price_list_price: float | None = None
            found_in_catalog = catalog_hit is not None
            catalog_matched_name = catalog_hit.name if catalog_hit else None

            if catalog_hit and isinstance(catalog_hit.payload, CatalogItem):
                item = catalog_hit.payload
                if item.cost is not None:
                    unit_cost = item.cost
                unit_price = item.price or item.cost
                supplier = format_catalog_supplier(item)
                goods_hit = self._goods_for_catalog_match(catalog_hit, name)
                if goods_hit:
                    if not supplier:
                        supplier = goods_hit.supplier
                    purchase_date = goods_hit.purchase_date
                    if goods_hit.cost is not None:
                        unit_cost = goods_hit.cost
                    if goods_hit.price is not None and unit_price is None:
                        unit_price = goods_hit.price
            else:
                mini = TZItem(number=0, name=name, unit="шт", quantity=1)
                candidates = self.matcher.find_candidates(mini)
                price_hit = self._pick_hit(candidates["price"], mini)
                if price_hit and isinstance(price_hit.payload, PriceListItem):
                    unit_price = price_hit.payload.price
                    price_list_price = price_hit.payload.price

            lines.append(
                KitComponentLine(
                    name=name,
                    unit_cost=unit_cost,
                    unit_price=unit_price,
                    quantity=1.0,
                    supplier=supplier,
                    purchase_date=purchase_date,
                    price_list_price=price_list_price,
                    found_in_catalog=found_in_catalog,
                    catalog_matched_name=catalog_matched_name,
                )
            )
        return lines

    def _enrich_kit_components(
        self,
        components: list[KitComponentLine],
        supplier: str | None,
        preferences: KpPreferences,
    ) -> list[KitComponentLine]:
        enriched: list[KitComponentLine] = []
        for line in components:
            goods = (
                self._match_goods_report(
                    TZItem(
                        number=0,
                        name=line.catalog_matched_name or line.name,
                        unit="шт",
                        quantity=1,
                    )
                )
                if line.found_in_catalog
                else None
            )
            price_check = (
                self._price_by_supplier(line.name, goods.supplier)
                if goods and goods.supplier
                else None
            )
            competitor_url = line.competitor_url
            competitor_platform = line.competitor_platform
            if preferences.search_kit_component_links:
                if self.ai.enabled and not competitor_url:
                    mini = TZItem(number=0, name=line.name, unit="шт", quantity=1)
                    offers = self.ai.search_competitors(mini, limit=1)
                    if offers:
                        offer = offers[0]
                        platform = str(offer.get("platform") or "Интернет")
                        competitor_platform = platform
                        competitor_url = resolve_competitor_url(
                            platform,
                            str(offer.get("name") or line.name),
                            offer.get("url"),
                        )
                if not competitor_url:
                    competitor_url = build_marketplace_search_url("Ozon", line.name)
                    competitor_platform = competitor_platform or "Ozon"
            comp_supplier = line.supplier
            comp_purchase_date = line.purchase_date
            if line.found_in_catalog and goods:
                comp_supplier = goods.supplier or comp_supplier
                comp_purchase_date = goods.purchase_date or comp_purchase_date
            enriched.append(
                KitComponentLine(
                    name=line.name,
                    unit_cost=line.unit_cost,
                    unit_price=line.unit_price,
                    quantity=line.quantity,
                    supplier=comp_supplier if line.found_in_catalog else None,
                    purchase_date=comp_purchase_date if line.found_in_catalog else None,
                    price_list_price=price_check.price if price_check else line.price_list_price,
                    competitor_url=competitor_url,
                    competitor_platform=competitor_platform,
                    found_in_catalog=line.found_in_catalog,
                    catalog_matched_name=line.catalog_matched_name,
                )
            )
        return enriched

    def _resolve_primary_match(
        self,
        tz_item: TZItem,
        catalog_hit: FuzzyHit | None,
        price_hit: FuzzyHit | None,
        registry_hit: FuzzyHit | None,
        goods_hit: GoodsReportItem | None,
        web_quote: PriceQuote | None,
        kit_components: list[KitComponentLine],
        use_ai: bool,
        candidates: dict,
        *,
        local_miss: bool = False,
        local_floor: float | None = None,
    ) -> MatchResult:
        floor = local_floor if local_floor is not None else LOCAL_MATCH_THRESHOLD
        local = self.matcher.match_local(tz_item, min_score=floor) if local_floor else None
        if local and not self._accepted_match(
            tz_item, local.matched_name, use_ai, score=local.match_score, min_score=floor
        ):
            local = None

        if local and local.status == MatchStatus.EXACT and (
            local.unit_base_price is not None or local.unit_cost is not None
        ):
            return self._merge_kit_into_result(local, kit_components, goods_hit)

        if local and local.match_score >= floor and (
            local.unit_base_price is not None or local.unit_cost is not None
        ):
            return self._merge_kit_into_result(local, kit_components, goods_hit)

        if (
            price_hit
            and local_floor is not None
            and effective_score(tz_item, price_hit.name, price_hit.score) >= floor
            and self._accepted_match(
                tz_item, price_hit.name, use_ai, score=price_hit.score, min_score=floor
            )
        ):
            item = price_hit.payload
            if isinstance(item, PriceListItem):
                status = (
                    MatchStatus.EXACT
                    if price_hit.score >= EXACT_MATCH_THRESHOLD
                    else MatchStatus.SIMILAR
                )
                return MatchResult(
                    tz_item=tz_item,
                    status=status,
                    source=MatchSource.PRICE_LIST,
                    matched_name=price_hit.name,
                    match_score=price_hit.score,
                    unit_cost=item.price,
                    unit_base_price=item.price,
                    notes="Сопоставление с прайсом",
                    source_detail=f"Прайс: {price_hit.name}",
                    supplier=item.supplier,
                )

        if (
            catalog_hit
            and local_floor is not None
            and effective_score(tz_item, catalog_hit.name, catalog_hit.score) >= floor
            and self._accepted_match(
                tz_item, catalog_hit.name, use_ai, score=catalog_hit.score, min_score=floor
            )
            and not self.matcher.is_distinctive_mismatch(tz_item.name, catalog_hit.name)
        ):
            item = catalog_hit.payload
            if isinstance(item, CatalogItem):
                unit_cost = item.cost
                if kit_components:
                    aggregated = self.catalog_structure.aggregate_kit_cost(kit_components)
                    if aggregated is not None:
                        unit_cost = aggregated
                unit_base = item.price or unit_cost
                status = (
                    MatchStatus.EXACT
                    if catalog_hit.score >= 90
                    else MatchStatus.SIMILAR
                )
                return MatchResult(
                    tz_item=tz_item,
                    status=status,
                    source=MatchSource.CATALOG,
                    matched_name=catalog_hit.name,
                    match_score=catalog_hit.score,
                    unit_cost=unit_cost,
                    unit_base_price=unit_base,
                    notes="Сопоставление с каталогом",
                    source_detail=f"Каталог: {catalog_hit.name}",
                    supplier=goods_hit.supplier if goods_hit else None,
                    purchase_date=goods_hit.purchase_date if goods_hit else None,
                )

        if (
            registry_hit
            and local_floor is not None
            and effective_score(tz_item, registry_hit.name, registry_hit.score) >= floor
            and self._accepted_match(
                tz_item, registry_hit.name, use_ai, score=registry_hit.score, min_score=floor
            )
            and not self.matcher.is_distinctive_mismatch(tz_item.name, registry_hit.name)
        ):
            stock_cost = self._goods_cost_for_name(registry_hit.name)
            if stock_cost is not None:
                return MatchResult(
                    tz_item=tz_item,
                    status=(
                        MatchStatus.EXACT
                        if registry_hit.score >= EXACT_MATCH_THRESHOLD
                        else MatchStatus.SIMILAR
                    ),
                    source=MatchSource.REGISTRY,
                    matched_name=registry_hit.name,
                    match_score=registry_hit.score,
                    unit_cost=stock_cost.cost,
                    unit_base_price=stock_cost.price or stock_cost.cost,
                    notes="Сопоставление с остатками на складе",
                    source_detail=f"Остатки: {registry_hit.name}",
                    supplier=stock_cost.supplier,
                    purchase_date=stock_cost.purchase_date,
                )

        if (
            goods_hit
            and local_floor is not None
            and goods_hit.cost is not None
            and relevance_score(tz_item, goods_hit.name) >= floor
            and self._accepted_match(tz_item, goods_hit.name, use_ai, min_score=floor)
        ):
            goods_score = relevance_score(tz_item, goods_hit.name)
            source_label = "остатками" if (goods_hit.source_file or "").startswith("stock:") else "товарным отчётом"
            return MatchResult(
                tz_item=tz_item,
                status=(
                    MatchStatus.EXACT
                    if goods_score >= EXACT_MATCH_THRESHOLD
                    else MatchStatus.SIMILAR
                ),
                source=MatchSource.CATALOG,
                matched_name=goods_hit.name,
                match_score=goods_score,
                unit_cost=goods_hit.cost,
                unit_base_price=goods_hit.price or goods_hit.cost,
                notes=f"Сопоставление с {source_label}",
                source_detail=f"Товарный отчёт: {goods_hit.name}",
                supplier=goods_hit.supplier,
                purchase_date=goods_hit.purchase_date,
            )

        if not use_ai:
            if local and local.match_score >= LOCAL_MATCH_THRESHOLD and (
                local.unit_base_price is not None or local.unit_cost is not None
            ):
                return self._merge_kit_into_result(local, kit_components, goods_hit)
            if web_quote and (web_quote.cost is not None or web_quote.price is not None):
                return self._result_from_web_quote(tz_item, web_quote)
            return MatchResult(
                tz_item=tz_item,
                status=MatchStatus.SIMILAR,
                source=MatchSource.NONE,
                notes="Подбор цены из интернета",
            )

        if local_miss and WEB_SEARCH_ENABLED:
            if web_quote and (web_quote.cost is not None or web_quote.price is not None):
                return self._result_from_web_quote(tz_item, web_quote)
            if local and local.match_score >= LOCAL_MATCH_THRESHOLD and (
                local.unit_base_price is not None or local.unit_cost is not None
            ):
                return self._merge_kit_into_result(local, kit_components, goods_hit)
            return MatchResult(
                tz_item=tz_item,
                status=MatchStatus.SIMILAR,
                source=MatchSource.NONE,
                matched_name=tz_item.name,
                notes="Поиск цены в интернете",
            )

        ai_candidates = self.matcher.candidates_for_ai(tz_item)
        ai_result = self.ai.match_item(
            tz_item,
            ai_candidates["catalog"],
            ai_candidates["price"],
            ai_candidates["registry"],
        )
        status = AIAgent.parse_status(ai_result.get("status", "not_found"))
        source = AIAgent.parse_source(ai_result.get("source", "none"))
        unit_cost = ai_result.get("unit_cost")
        unit_base_price = ai_result.get("unit_price") or ai_result.get("unit_cost")
        notes = ai_result.get("notes", "")
        alternatives = ai_result.get("alternatives") or []
        matched_name = ai_result.get("matched_name", "")
        match_score = float(ai_result.get("match_score", 0) or 0)
        if matched_name and not self._accepted_match(
            tz_item, matched_name, use_ai, score=match_score
        ):
            matched_name = ""
            if status != MatchStatus.NOT_FOUND:
                status = MatchStatus.NOT_FOUND
                source = MatchSource.NONE
                notes = (
                    f"{notes} | AI-кандидат отклонён: другой тип товара"
                    if notes
                    else "AI-кандидат отклонён: другой тип товара"
                )

        internet_priced = source == MatchSource.WEB
        if (
            status == MatchStatus.NOT_FOUND
            and source == MatchSource.NONE
            and web_quote
            and (web_quote.cost is not None or web_quote.price is not None)
        ):
            status = MatchStatus.SIMILAR
            source = MatchSource.WEB
            unit_cost = web_quote.cost
            unit_base_price = web_quote.price or web_quote.cost
            matched_name = web_quote.matched_name or tz_item.name
            match_score = web_quote.match_score
            notes = web_quote.notes
            internet_priced = True

        if local and local.match_score > match_score and (
            local.unit_base_price is not None or local.unit_cost is not None
        ):
            return self._merge_kit_into_result(local, kit_components, goods_hit)

        if local:
            if unit_base_price is None:
                unit_base_price = local.unit_base_price
            if unit_cost is None:
                unit_cost = local.unit_cost
            if local.unit_base_price is not None or local.unit_cost is not None:
                internet_priced = False
                if source in (MatchSource.NONE, MatchSource.WEB) and local.source != MatchSource.WEB:
                    source = local.source

        return MatchResult(
            tz_item=tz_item,
            status=status,
            source=source,
            matched_name=matched_name or (local.matched_name if local else ""),
            match_score=max(match_score, local.match_score if local else 0),
            unit_cost=unit_cost,
            unit_base_price=unit_base_price,
            notes=notes or (local.notes if local else "Не найдено"),
            source_detail=self._source_detail(source, matched_name, local),
            alternatives=alternatives or (local.alternatives if local else []),
            supplier=goods_hit.supplier if goods_hit else None,
            purchase_date=goods_hit.purchase_date if goods_hit else None,
            internet_priced=internet_priced and source == MatchSource.WEB,
        )

    @staticmethod
    def _result_from_web_quote(tz_item: TZItem, web_quote: PriceQuote) -> MatchResult:
        base_price = web_quote.cost if web_quote.cost is not None else web_quote.price
        match_score = float(web_quote.match_score or WEB_SEARCH_EXACT_THRESHOLD)
        status = (
            MatchStatus.EXACT
            if match_score >= EXACT_MATCH_THRESHOLD
            else MatchStatus.SIMILAR
        )
        return MatchResult(
            tz_item=tz_item,
            status=status,
            source=MatchSource.WEB,
            matched_name=web_quote.matched_name or tz_item.name,
            match_score=match_score,
            unit_cost=base_price,
            unit_base_price=base_price,
            notes=(
                f"{web_quote.notes} | Цена КП: −{WEB_PRICE_DISCOUNT_PERCENT}% "
                f"от найденной в интернете"
            ).strip(" |"),
            source_detail=enrich_source_detail_with_price_url(
                web_quote.label,
                [web_quote],
                unit_base_price=base_price,
                preferred=web_quote,
            ),
            internet_priced=True,
        )

    def _finalize_result_pricing(
        self,
        result: MatchResult,
        use_ai: bool = True,
        prefs: KpPreferences | None = None,
        *,
        skip_internet_search: bool = False,
    ) -> None:
        needs_price = result.unit_base_price is None
        web_without_price = (
            result.source == MatchSource.WEB
            and result.unit_base_price is None
            and result.unit_cost is None
        )
        if not needs_price and not web_without_price:
            self._normalize_result_metadata(result)
            return

        best_local = self._best_local_priced_quote(result.comparison, result.tz_item)
        if best_local:
            self._apply_priced_quote(result, best_local)
            return

        best_web = self._best_web_priced_quote(result.comparison)
        if best_web:
            self._apply_priced_quote(
                result,
                best_web,
                unpriced_competitor_reference=(
                    has_unpriced_competitor_display_quote(result.comparison)
                    and is_marketplace_url(best_web.url)
                ),
            )
            return

        if (
            WEB_SEARCH_ENABLED
            and needs_price
            and not skip_internet_search
            and not has_acceptable_web_pricing_in_comparison(result.comparison)
        ):
            search_text = primary_search_text(result.tz_item)
            extra_quotes = self.web_search.search_web_price_fallback(search_text)
            if prefs:
                extra_quotes = filter_web_quotes(extra_quotes, prefs)
            if extra_quotes:
                self._extend_comparison(result, extra_quotes)
                result.competitors = [
                    q for q in result.comparison if q.source == "web"
                ]
                best_web = pick_marketplace_priced_quote(extra_quotes)
                if best_web is None:
                    best_web = self._best_web_priced_quote(extra_quotes)
                if best_web:
                    self._apply_priced_quote(
                        result,
                        best_web,
                        unpriced_competitor_reference=has_unpriced_competitor_display_quote(
                            result.comparison
                        ),
                    )
                    return

        if (
            USE_AI_INTERNET_SEARCH
            and use_ai
            and self.ai.enabled
            and needs_price
        ):
            ai_quotes = self._fetch_internet_comparison_ai(result.tz_item)
            if prefs:
                ai_quotes = filter_web_quotes(ai_quotes, prefs)
            if ai_quotes:
                existing_urls = {
                    q.url for q in result.comparison if q.url
                }
                for quote in ai_quotes:
                    if quote.url and quote.url in existing_urls:
                        continue
                    result.comparison.append(quote)
                    if quote.url:
                        existing_urls.add(quote.url)
                result.competitors = [
                    q for q in result.comparison if q.source == "web"
                ]
                best_web = self._best_web_priced_quote(ai_quotes)
                if best_web:
                    self._apply_priced_quote(result, best_web)
                    return

        if web_without_price:
            result.status = MatchStatus.SIMILAR
            result.notes = (
                f"{result.notes} | Цена не определена — проверьте сравнение"
            ).strip(" |")

        self._normalize_result_metadata(result)

    def _apply_priced_quote(
        self,
        result: MatchResult,
        best: PriceQuote,
        *,
        unpriced_competitor_reference: bool = False,
    ) -> None:
        base_price = best.cost if best.cost is not None else best.price
        if base_price is None:
            return

        source_map = {
            "catalog": MatchSource.CATALOG,
            "price_list": MatchSource.PRICE_LIST,
            "registry": MatchSource.REGISTRY,
            "web": MatchSource.WEB,
        }
        result.source = source_map.get(best.source, result.source)
        result.internet_priced = best.source == "web"
        result.matched_name = best.matched_name or result.matched_name
        result.unit_cost = best.cost if best.cost is not None else base_price
        result.unit_base_price = base_price
        result.match_score = max(result.match_score, best.match_score or 0)
        if result.status == MatchStatus.NOT_FOUND:
            result.status = MatchStatus.SIMILAR
        elif result.match_score < EXACT_MATCH_THRESHOLD:
            result.status = MatchStatus.SIMILAR
        if best.supplier and not result.supplier:
            result.supplier = best.supplier
        if best.purchase_date and not result.purchase_date:
            result.purchase_date = best.purchase_date
        if best.source == "web":
            note = (
                f"Подбор по сравнению: {best.label} | "
                f"Цена КП: −{WEB_PRICE_DISCOUNT_PERCENT}% от найденной в интернете"
            )
            if unpriced_competitor_reference and is_marketplace_url(best.url):
                note = (
                    f"{note} | На сайте конкурента совпадение без цены — "
                    f"цена КП из маркетплейса"
                )
        else:
            note = f"Подбор по сравнению: {best.label}"
        result.notes = note if not result.notes else f"{result.notes} | {note}"
        if best.source == "web":
            result.source_detail = enrich_source_detail_with_price_url(
                best.label,
                result.comparison,
                unit_base_price=base_price,
                preferred=best,
            )
        else:
            result.source_detail = best.label

    def _ensure_mandatory_internet_pricing(
        self,
        result: MatchResult,
        prefs: KpPreferences,
        *,
        use_ai: bool,
        skip_internet_search: bool = False,
    ) -> None:
        if "web" in prefs.disabled_sources or not WEB_SEARCH_ENABLED:
            self._promote_internet_status(result)
            return
        if self._has_confident_local_pricing(result):
            return

        needs_price = result.unit_base_price is None and result.unit_cost is None
        if needs_price:
            candidates: list[PriceQuote] = []
            has_priced_web = any(
                q.source == "web"
                and is_acceptable_web_pricing_quote(q)
                for q in result.comparison
            )
            search_text = primary_search_text(result.tz_item)
            if not has_priced_web and not skip_internet_search:
                candidates.extend(
                    self.web_search.search_web_price_fallback(search_text)
                )
            if (
                use_ai
                and self.ai.enabled
                and not has_priced_web
                and USE_AI_INTERNET_SEARCH
            ):
                candidates.extend(self._fetch_internet_comparison_ai(result.tz_item))

            if (
                use_ai
                and self.ai.enabled
                and not any(q.price or q.cost for q in candidates)
                and not has_priced_web
            ):
                ai_quote = self._quote_from_ai_estimate(result.tz_item)
                if ai_quote:
                    candidates.append(ai_quote)

            filtered = filter_web_quotes(candidates, prefs)
            self._extend_comparison(result, filtered)
            result.comparison = sort_web_quotes(result.comparison)

            best = pick_marketplace_priced_quote(filtered)
            if best is None:
                best = self._best_web_priced_quote(filtered)
            if best is None:
                best = self._best_web_priced_quote_with_url(filtered)
            if best:
                self._apply_priced_quote(
                    result,
                    best,
                    unpriced_competitor_reference=has_unpriced_competitor_display_quote(
                        result.comparison
                    ),
                )

        self._sort_comparison_web_quotes(result)
        self._promote_internet_status(result)

    @staticmethod
    def _sort_comparison_web_quotes(result: MatchResult) -> None:
        result.comparison = sort_web_quotes(result.comparison)

    def _append_marketplace_product_cards(
        self,
        result: MatchResult,
        prefs: KpPreferences,
    ) -> None:
        del prefs
        self._sort_comparison_web_quotes(result)

    @staticmethod
    def _extend_comparison(result: MatchResult, quotes: list[PriceQuote]) -> None:
        seen = {
            (q.source, q.url or "", q.matched_name or "", q.price, q.cost)
            for q in result.comparison
        }
        for quote in quotes:
            key = (
                quote.source,
                quote.url or "",
                quote.matched_name or "",
                quote.price,
                quote.cost,
            )
            if key in seen:
                continue
            seen.add(key)
            result.comparison.append(quote)

    @staticmethod
    def _has_confident_local_pricing(result: MatchResult) -> bool:
        if result.unit_base_price is None and result.unit_cost is None:
            return False
        if result.internet_priced or result.source == MatchSource.WEB:
            return False
        if result.is_kit and result.kit_components and (
            result.unit_base_price is not None or result.unit_cost is not None
        ):
            return True
        if result.source in (MatchSource.CATALOG, MatchSource.PRICE_LIST):
            return result.match_score >= LOCAL_MATCH_THRESHOLD
        return (
            result.source not in (MatchSource.NONE, MatchSource.WEB)
            and result.match_score >= LOCAL_MATCH_THRESHOLD
        )

    def _quote_from_ai_estimate(self, tz_item: TZItem) -> PriceQuote | None:
        if not self.ai.enabled:
            return None
        web_result = self.ai.estimate_web_price(tz_item)
        unit_cost = web_result.get("unit_cost")
        if unit_cost is None:
            return None
        urls = competitor_urls_for_item([], tz_item.name, limit=1)
        url = urls[0] if urls else None
        if url and is_search_listing_url(url):
            url = None
        matched_name = str(web_result.get("matched_name") or tz_item.name)
        return PriceQuote(
            source="web",
            label="Интернет (оценка рынка)",
            matched_name=matched_name,
            price=float(unit_cost),
            cost=float(unit_cost),
            match_score=100.0,
            url=url,
            notes=(
                f"{web_result.get('notes') or 'AI-оценка'} | "
                f"Источник: {web_result.get('price_source') or 'открытые данные'}"
            ),
        )

    @staticmethod
    def _promote_internet_status(result: MatchResult) -> None:
        from src.services.matcher import ItemMatcher

        tz_item = result.tz_item

        def _web_quote_ok(quote: PriceQuote) -> bool:
            name = quote.matched_name or ""
            if not name:
                return is_acceptable_web_pricing_quote(quote)
            if ItemMatcher.is_distinctive_mismatch(tz_item.name, name):
                return False
            if product_type_conflict(tz_item, name):
                return False
            min_score = (
                LOCAL_MATCH_THRESHOLD
                if quote.url and is_competitor_url(quote.url)
                else WEB_SEARCH_EXACT_THRESHOLD
            )
            if not is_relevant_match(
                tz_item,
                name,
                score=float(quote.match_score or 0),
                min_score=min_score,
            ):
                return False
            return is_acceptable_web_pricing_quote(quote)

        has_web_price = result.internet_priced and result.unit_base_price is not None
        web_with_url = [
            q
            for q in result.comparison
            if q.source == "web"
            and q.url
            and _web_quote_ok(q)
        ]

        if has_web_price or web_with_url:
            if result.unit_base_price is None and web_with_url:
                best = pick_best_web_priced_quote(web_with_url)
                if best is None:
                    best = web_with_url[0]
                self_price = best.cost if best.cost is not None else best.price
                if self_price is not None:
                    result.unit_cost = self_price
                    result.unit_base_price = self_price
                    result.internet_priced = True
                    result.matched_name = best.matched_name or result.tz_item.name
                    result.match_score = max(
                        result.match_score,
                        float(best.match_score or WEB_SEARCH_EXACT_THRESHOLD),
                    )

            result.status = (
                MatchStatus.EXACT
                if result.match_score >= EXACT_MATCH_THRESHOLD
                else MatchStatus.SIMILAR
            )
            result.source = MatchSource.WEB
            if not result.matched_name:
                result.matched_name = result.tz_item.name
            TZMatchService._ensure_internet_source_detail(result)
            if result.internet_priced and "−" not in (result.notes or ""):
                result.notes = (
                    f"{result.notes} | Цена КП: −{WEB_PRICE_DISCOUNT_PERCENT}% "
                    f"от найденной в интернете"
                ).strip(" |")
            return

        if result.status == MatchStatus.NOT_FOUND or result.source == MatchSource.NONE:
            links = [
                q
                for q in result.comparison
                if q.source == "web"
                and q.url
                and not is_search_listing_url(q.url)
            ]
            if links:
                result.status = MatchStatus.SIMILAR
                result.source = MatchSource.WEB
                result.matched_name = result.tz_item.name
                result.notes = (
                    f"{result.notes} | Цена не извлечена — проверьте ссылки"
                ).strip(" |")
                TZMatchService._ensure_internet_source_detail(result)

    @staticmethod
    def _priced_web_quote_for_result(result: MatchResult) -> PriceQuote | None:
        best = pick_best_web_priced_quote(result.comparison)
        if best is not None:
            return best
        if result.unit_base_price is None:
            return None
        for quote in result.comparison:
            if quote.source != "web":
                continue
            base = quote.cost if quote.cost is not None else quote.price
            if base is not None and abs(base - result.unit_base_price) < 0.01:
                return quote
        return None

    @staticmethod
    def _ensure_internet_source_detail(result: MatchResult) -> None:
        if not result.internet_priced and result.source != MatchSource.WEB:
            return
        if result.unit_base_price is None and result.unit_cost is None:
            return

        preferred = TZMatchService._priced_web_quote_for_result(result)
        result.source_detail = enrich_source_detail_with_price_url(
            result.source_detail,
            result.comparison,
            unit_base_price=result.unit_base_price,
            preferred=preferred,
        )

    @staticmethod
    def _best_web_priced_quote_with_url(
        quotes: list[PriceQuote],
    ) -> PriceQuote | None:
        eligible = [
            quote
            for quote in quotes
            if quote.source == "web"
            and quote.url
            and is_acceptable_web_pricing_quote(quote)
        ]
        if not eligible:
            return None
        eligible.sort(key=web_quote_rank_key)
        return eligible[0]

    @staticmethod
    def _best_web_priced_quote(quotes: list[PriceQuote]) -> PriceQuote | None:
        return pick_best_web_priced_quote(quotes)

    @staticmethod
    def _best_local_priced_quote(
        quotes: list[PriceQuote],
        tz_item: TZItem,
    ) -> PriceQuote | None:
        priority = ("price_list", "catalog", "registry")
        best: PriceQuote | None = None
        best_score = -1.0
        for source in priority:
            for quote in quotes:
                if quote.source != source:
                    continue
                base = quote.cost if quote.cost is not None else quote.price
                if base is None:
                    continue
                score = float(quote.match_score or 0)
                if score < LOCAL_MATCH_THRESHOLD:
                    continue
                if not is_relevant_match(
                    tz_item,
                    quote.matched_name or "",
                    score=score,
                ):
                    continue
                if ItemMatcher.is_distinctive_mismatch(
                    tz_item.name,
                    quote.matched_name or "",
                ):
                    continue
                if score > best_score:
                    best_score = score
                    best = quote
            if best is not None:
                return best
        return None

    @staticmethod
    def _aggregate_kit_components(
        components: list[KitComponentLine],
    ) -> tuple[float | None, float | None]:
        costs = [line.unit_cost for line in components if line.unit_cost is not None]
        prices = [line.unit_price for line in components if line.unit_price is not None]
        total_cost = round(sum(costs), 2) if costs else None
        total_price = round(sum(prices), 2) if prices else None
        return total_cost, total_price

    @staticmethod
    def _merge_kit_into_result(
        result: MatchResult,
        kit_components: list[KitComponentLine],
        goods_hit: GoodsReportItem | None,
    ) -> MatchResult:
        if goods_hit:
            result.supplier = goods_hit.supplier
            result.purchase_date = goods_hit.purchase_date
        if kit_components:
            agg_cost, agg_price = TZMatchService._aggregate_kit_components(kit_components)
            if agg_cost is not None and result.unit_cost is None:
                result.unit_cost = agg_cost
            if agg_price is not None:
                result.unit_base_price = agg_price
            elif agg_cost is not None and result.unit_base_price is None:
                result.unit_base_price = agg_cost
        return result

    @staticmethod
    def _source_detail(
        source: MatchSource,
        matched_name: str,
        local: MatchResult | None,
    ) -> str:
        if source == MatchSource.WEB:
            return "Оценка по открытым источникам (AI)"
        if source == MatchSource.CATALOG:
            return f"Каталог: {matched_name}"
        if source == MatchSource.PRICE_LIST:
            return f"Прайс: {matched_name}"
        if source == MatchSource.REGISTRY:
            return f"Реестр: {matched_name}"
        return local.source_detail if local else ""

    def _pick_hit(self, hits: list[FuzzyHit], tz_item: TZItem) -> FuzzyHit | None:
        if not hits:
            return None
        return self.matcher.pick_best_hit(tz_item, hits)

    def _accepted_match(
        self,
        tz_item: TZItem,
        matched_name: str,
        use_ai: bool,
        *,
        score: float | None = None,
        min_score: float = LOCAL_MATCH_THRESHOLD,
    ) -> bool:
        if not matched_name:
            return False
        if not is_relevant_match(
            tz_item,
            matched_name,
            score=score,
            min_score=min_score,
        ):
            return False
        if use_ai and self.ai.enabled:
            verdict = self.ai.validate_tz_candidate(tz_item, matched_name)
            if not verdict.get("accept"):
                logger.info(
                    "AI rejected %r for TZ %r: %s",
                    matched_name,
                    tz_item.name,
                    verdict.get("reason"),
                )
                return False
        return True

    def _pick_validated_hit(
        self,
        hits: list[FuzzyHit],
        tz_item: TZItem,
        use_ai: bool,
        *,
        min_score: float | None = LOCAL_MATCH_THRESHOLD,
    ) -> FuzzyHit | None:
        if min_score is None or not hits:
            return None
        ranked = sorted(
            hits,
            key=lambda hit: (
                effective_score(tz_item, hit.name, hit.score),
                hit.score,
            ),
            reverse=True,
        )
        shortlist: list[FuzzyHit] = []
        for hit in ranked[:12]:
            score = effective_score(tz_item, hit.name, hit.score)
            if score < min_score:
                continue
            if not is_relevant_match(
                tz_item,
                hit.name,
                score=hit.score,
                min_score=min_score,
            ):
                continue
            if self.matcher.is_distinctive_mismatch(tz_item.name, hit.name):
                continue
            shortlist.append(hit)
        for hit in shortlist[:3]:
            if use_ai and self.ai.enabled:
                verdict = self.ai.validate_tz_candidate(tz_item, hit.name)
                if not verdict.get("accept"):
                    logger.info(
                        "AI rejected %r for TZ %r: %s",
                        hit.name,
                        tz_item.name,
                        verdict.get("reason"),
                    )
                    continue
            return hit
        return None

    def _find_direct_catalog_hit(
        self, tz_item: TZItem, *, use_ai: bool = True, min_score: float | None = LOCAL_MATCH_THRESHOLD
    ) -> FuzzyHit | None:
        if min_score is None:
            return None
        best: FuzzyHit | None = None
        best_score = -1.0
        for item in self.catalog:
            if item.entry_type not in {"item", "kit_total", "sub_kit"}:
                continue
            score = combined_match_score(tz_item, item.name)
            if score < min_score:
                continue
            if not is_relevant_match(tz_item, item.name, score=score, min_score=min_score):
                continue
            if self.matcher.is_distinctive_mismatch(tz_item.name, item.name):
                continue
            if score > best_score:
                best_score = score
                best = FuzzyHit(
                    name=item.name,
                    score=score,
                    payload=item,
                    source=MatchSource.CATALOG,
                    detail=item.source_file,
                )
        if best and use_ai and self.ai.enabled:
            verdict = self.ai.validate_tz_candidate(tz_item, best.name)
            if not verdict.get("accept"):
                logger.info(
                    "AI rejected direct catalog %r for TZ %r: %s",
                    best.name,
                    tz_item.name,
                    verdict.get("reason"),
                )
                return None
        return best

    @staticmethod
    def _goods_source_priority(item: GoodsReportItem) -> int:
        source_file = item.source_file or ""
        if source_file.startswith("stock:"):
            return 3
        if source_file.startswith("procurement:"):
            return 1
        return 2

    def _goods_cost_for_name(self, name: str) -> GoodsReportItem | None:
        query = normalize_name(name)
        if not query or not self.goods_report:
            return None
        results = process.extract(
            query,
            self._goods_names,
            scorer=name_match_score,
            limit=3,
        )
        best: GoodsReportItem | None = None
        best_rank = (-1.0, -1)
        for _, score, idx in results:
            item = self.goods_report[idx]
            if item.cost is None:
                continue
            if normalize_name(item.name) != query and score < LOCAL_MATCH_THRESHOLD:
                continue
            rank = (score, self._goods_source_priority(item))
            if rank > best_rank:
                best_rank = rank
                best = item
        return best

    def _match_goods_report(
        self,
        tz_item: TZItem,
        *,
        min_score: float | None = LOCAL_MATCH_THRESHOLD,
    ) -> GoodsReportItem | None:
        if min_score is None or not self.goods_report:
            return None
        query = normalize_name(tz_item.name)
        results = process.extract(
            query,
            self._goods_names,
            scorer=name_match_score,
            limit=3,
        )
        best: GoodsReportItem | None = None
        best_rank = (-1.0, -1)
        for _, score, idx in results:
            item = self.goods_report[idx]
            item_score = relevance_score(tz_item, item.name)
            if item_score < min_score:
                continue
            if not is_relevant_match(
                tz_item,
                item.name,
                score=item_score,
                min_score=min_score,
            ):
                continue
            if self.matcher.is_distinctive_mismatch(tz_item.name, item.name):
                continue
            rank = (item_score, self._goods_source_priority(item))
            if rank > best_rank:
                best_rank = rank
                best = item
        return best

    def _price_by_supplier(
        self,
        name: str,
        supplier: str | None,
    ) -> PriceQuote | None:
        if not supplier or not self.matcher.price_lists:
            return None

        supplier_lower = supplier.lower().strip()
        filtered = [
            item
            for item in self.matcher.price_lists
            if supplier_lower in (item.supplier or "").lower()
        ]
        if not filtered:
            return None

        names = [normalize_name(item.name) for item in filtered]
        query = normalize_name(name)
        results = process.extract(
            query,
            names,
            scorer=name_match_score,
            limit=1,
        )
        if not results:
            return None
        _, score, idx = results[0]
        if score < SIMILAR_MATCH_THRESHOLD:
            return None

        item: PriceListItem = filtered[idx]
        return PriceQuote(
            source="price_list",
            label=f"Прайс ({item.supplier})",
            matched_name=item.name,
            price=item.price,
            supplier=item.supplier,
            match_score=float(score),
            notes=f"код {item.code}" if item.code else "",
        )

    @staticmethod
    def _quote_from_catalog(
        hit: FuzzyHit,
        kit_components: list[KitComponentLine],
    ) -> PriceQuote:
        item: CatalogItem = hit.payload
        cost = item.cost
        if kit_components:
            total = sum(
                (line.unit_cost or 0) * line.quantity
                for line in kit_components
                if line.unit_cost is not None
            )
            if total > 0:
                cost = round(total, 2)
        notes = f"состав: {len(kit_components)} поз." if kit_components else ""
        supplier = format_catalog_supplier(item)
        return PriceQuote(
            source="catalog",
            label="Каталог",
            matched_name=hit.name,
            cost=cost,
            price=item.price or cost,
            supplier=supplier,
            match_score=hit.score,
            notes=notes,
        )

    @staticmethod
    def _quote_from_goods_report(item: GoodsReportItem) -> PriceQuote:
        label = (
            "Отчёт по закупкам"
            if item.source_file and item.source_file.startswith("procurement:")
            else "Товарный отчёт"
        )
        return PriceQuote(
            source="goods_report",
            label=label,
            matched_name=item.name,
            cost=item.cost,
            price=item.price or item.cost,
            supplier=item.supplier,
            purchase_date=item.purchase_date,
            match_score=85.0,
        )

    @staticmethod
    def _quote_from_price(hit: FuzzyHit) -> PriceQuote:
        item: PriceListItem = hit.payload
        return PriceQuote(
            source="price_list",
            label=f"Прайс ({item.supplier})",
            matched_name=hit.name,
            price=item.price,
            supplier=item.supplier,
            match_score=hit.score,
            notes=f"код {item.code}" if item.code else "",
        )

    @staticmethod
    def _quote_from_registry(hit: FuzzyHit) -> PriceQuote:
        item: RegistryItem = hit.payload
        return PriceQuote(
            source="registry",
            label="Реестр остатков",
            matched_name=hit.name,
            match_score=hit.score,
            notes=f"остаток: {item.quantity} шт.",
        )
