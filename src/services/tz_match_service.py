from __future__ import annotations

import logging

from rapidfuzz import fuzz, process

from src.config import EXACT_MATCH_THRESHOLD, SIMILAR_MATCH_THRESHOLD, USE_AI_INTERNET_SEARCH
from src.services.ai_agent import AIAgent
from src.services.catalog_structure import CatalogStructure
from src.services.data_loader import normalize_name
from src.config import SEARCH_KIT_COMPONENT_LINKS
from src.services.kp_preferences import KpPreferences, filter_web_quotes
from src.services.competitor_urls import (
    build_marketplace_search_url,
    competitor_urls_for_item,
    resolve_competitor_url,
)
from src.services.kit_spec_parser import parse_kit_components_from_specs
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
        catalog_hit = self._pick_hit(candidates["catalog"], tz_item.name)
        if catalog_hit and self.matcher.is_distinctive_mismatch(
            tz_item.name, catalog_hit.name
        ):
            catalog_hit = None
        price_hit = self._pick_hit(candidates["price"], tz_item.name)
        registry_hit = self._pick_hit(candidates["registry"], tz_item.name)
        goods_hit = self._match_goods_report(tz_item.name)

        comparison: list[PriceQuote] = []
        kit_components: list[KitComponentLine] = []
        supplier: str | None = None
        purchase_date: str | None = None
        is_kit = False
        tz_kit_names = parse_kit_components_from_specs(tz_item.specifications)

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

        if price_hit:
            comparison.append(self._quote_from_price(price_hit))

        if registry_hit:
            comparison.append(self._quote_from_registry(registry_hit))

        price_list_check = self._price_by_supplier(tz_item.name, supplier)
        if price_list_check:
            comparison.append(price_list_check)

        for extra_price in self._extra_price_quotes(candidates["price"], tz_item.name):
            if not any(
                q.source == "price_list" and q.matched_name == extra_price.matched_name
                for q in comparison
            ):
                comparison.append(extra_price)

        web_quote, competitors = self._fetch_internet_comparison(
            tz_item, prefs, use_ai=use_ai
        )
        comparison.extend(competitors)

        primary = self._resolve_primary_match(
            tz_item,
            catalog_hit,
            price_hit,
            goods_hit,
            web_quote,
            kit_components,
            use_ai=use_ai,
            candidates=candidates,
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
        self._finalize_result_pricing(primary, use_ai=use_ai, prefs=prefs)
        return primary

    def _fetch_internet_comparison(
        self,
        tz_item: TZItem,
        preferences: KpPreferences,
        use_ai: bool = True,
    ) -> tuple[PriceQuote | None, list[PriceQuote]]:
        if "web" in preferences.disabled_sources:
            return None, []

        if USE_AI_INTERNET_SEARCH and use_ai and self.ai.enabled:
            quotes = self._fetch_internet_comparison_ai(tz_item)
            if quotes:
                quotes = filter_web_quotes(quotes, preferences)
                web_quote = quotes[0] if quotes else None
                return web_quote, quotes

        return self._fetch_internet_comparison_fast(tz_item, preferences)

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
            platform = str(offer.get("platform") or "Интернет")
            matched_name = str(offer.get("name") or tz_item.name)
            quotes.append(
                PriceQuote(
                    source="web",
                    label=f"Интернет: {platform}",
                    matched_name=matched_name,
                    price=float(offer["price"]) if offer.get("price") is not None else None,
                    match_score=float(offer.get("match_score", 50) or 50),
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
            if web_result.get("unit_cost") is not None:
                quotes.append(
                    PriceQuote(
                        source="web",
                        label="Интернет (оценка рынка)",
                        matched_name=str(web_result.get("matched_name") or ""),
                        cost=float(web_result["unit_cost"]),
                        price=float(web_result["unit_cost"]),
                        match_score=float(web_result.get("match_score", 0) or 0),
                        notes=str(web_result.get("notes") or ""),
                    )
                )

        return quotes

    def _extra_price_quotes(
        self,
        hits: list[FuzzyHit],
        query: str,
    ) -> list[PriceQuote]:
        quotes: list[PriceQuote] = []
        seen_suppliers: set[str] = set()
        ranked = sorted(hits, key=lambda hit: hit.score, reverse=True)
        for hit in ranked[:5]:
            if hit.score < SIMILAR_MATCH_THRESHOLD:
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
        catalog_hit = self._pick_hit(candidates["catalog"], name)
        if not catalog_hit:
            return None
        if self.matcher.is_distinctive_mismatch(name, catalog_hit.name):
            return None
        if catalog_hit.score < SIMILAR_MATCH_THRESHOLD:
            return None
        if not isinstance(catalog_hit.payload, CatalogItem):
            return None
        return catalog_hit

    def _goods_for_catalog_match(
        self,
        catalog_hit: FuzzyHit,
        tz_name: str,
    ) -> GoodsReportItem | None:
        goods_hit = self._match_goods_report(catalog_hit.name)
        if goods_hit:
            return goods_hit
        return self._match_goods_report(tz_name)

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
                goods_hit = self._goods_for_catalog_match(catalog_hit, name)
                if goods_hit:
                    supplier = goods_hit.supplier
                    purchase_date = goods_hit.purchase_date
                    if goods_hit.cost is not None:
                        unit_cost = goods_hit.cost
                    if goods_hit.price is not None and unit_price is None:
                        unit_price = goods_hit.price
            else:
                mini = TZItem(number=0, name=name, unit="шт", quantity=1)
                candidates = self.matcher.find_candidates(mini)
                price_hit = self._pick_hit(candidates["price"], name)
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
                self._match_goods_report(line.catalog_matched_name or line.name)
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
        goods_hit: GoodsReportItem | None,
        web_quote: PriceQuote | None,
        kit_components: list[KitComponentLine],
        use_ai: bool,
        candidates: dict,
    ) -> MatchResult:
        local = self.matcher.match_local(tz_item)

        if local and local.status == MatchStatus.EXACT and (
            local.unit_base_price is not None or local.unit_cost is not None
        ):
            return self._merge_kit_into_result(local, kit_components, goods_hit)

        if local and local.match_score >= SIMILAR_MATCH_THRESHOLD and (
            local.unit_base_price is not None or local.unit_cost is not None
        ):
            return self._merge_kit_into_result(local, kit_components, goods_hit)

        if (
            catalog_hit
            and catalog_hit.score >= SIMILAR_MATCH_THRESHOLD
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

        if goods_hit and goods_hit.cost is not None:
            return MatchResult(
                tz_item=tz_item,
                status=MatchStatus.SIMILAR,
                source=MatchSource.CATALOG,
                matched_name=goods_hit.name,
                match_score=85.0,
                unit_cost=goods_hit.cost,
                unit_base_price=goods_hit.price or goods_hit.cost,
                notes="Сопоставление с товарным отчётом",
                source_detail=f"Товарный отчёт: {goods_hit.name}",
                supplier=goods_hit.supplier,
                purchase_date=goods_hit.purchase_date,
            )

        if price_hit and price_hit.score >= SIMILAR_MATCH_THRESHOLD:
            item = price_hit.payload
            if isinstance(item, PriceListItem):
                status = (
                    MatchStatus.EXACT
                    if price_hit.score >= 90
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
                    notes=f"Сопоставление с прайсом ({item.supplier})",
                    source_detail=f"Прайс: {price_hit.name}",
                    supplier=item.supplier,
                )

        if not use_ai:
            if local:
                return self._merge_kit_into_result(local, kit_components, goods_hit)
            return MatchResult(
                tz_item=tz_item,
                status=MatchStatus.NOT_FOUND,
                source=MatchSource.NONE,
                notes="Позиция не найдена в каталогах и прайсах",
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
        matched_name = ai_result.get("matched_name", "")
        match_score = float(ai_result.get("match_score", 0) or 0)
        notes = ai_result.get("notes", "")
        alternatives = ai_result.get("alternatives") or []

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

        if local and local.match_score > match_score and (
            local.unit_base_price is not None or local.unit_cost is not None
        ):
            return self._merge_kit_into_result(local, kit_components, goods_hit)

        if local:
            if unit_base_price is None:
                unit_base_price = local.unit_base_price
            if unit_cost is None:
                unit_cost = local.unit_cost

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
        )

    def _finalize_result_pricing(
        self,
        result: MatchResult,
        use_ai: bool = True,
        prefs: KpPreferences | None = None,
    ) -> None:
        needs_price = result.unit_base_price is None
        web_without_price = (
            result.source == MatchSource.WEB
            and result.unit_base_price is None
            and result.unit_cost is None
        )
        if not needs_price and not web_without_price:
            return

        tz_query = result.tz_item.name
        best_local = self._best_local_priced_quote(result.comparison, tz_query)
        if best_local:
            self._apply_priced_quote(result, best_local)
            return

        best_web = self._best_web_priced_quote(result.comparison)
        if best_web:
            self._apply_priced_quote(result, best_web)
            return

        if use_ai and self.ai.enabled and needs_price:
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

    def _apply_priced_quote(self, result: MatchResult, best: PriceQuote) -> None:
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
        note = f"Подбор по сравнению: {best.label}"
        result.notes = note if not result.notes else f"{result.notes} | {note}"
        result.source_detail = best.label

    @staticmethod
    def _best_local_priced_quote(
        quotes: list[PriceQuote],
        tz_query: str = "",
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
                if score < SIMILAR_MATCH_THRESHOLD:
                    continue
                if tz_query and quote.matched_name and ItemMatcher.is_distinctive_mismatch(
                    tz_query, quote.matched_name
                ):
                    continue
                if score > best_score:
                    best_score = score
                    best = quote
            if best is not None:
                return best
        return None

    @staticmethod
    def _best_web_priced_quote(quotes: list[PriceQuote]) -> PriceQuote | None:
        best: PriceQuote | None = None
        best_score = -1.0
        for quote in quotes:
            if quote.source != "web":
                continue
            base = quote.cost if quote.cost is not None else quote.price
            if base is None:
                continue
            score = float(quote.match_score or 0)
            if score > best_score:
                best_score = score
                best = quote
        return best

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

    def _pick_hit(self, hits: list[FuzzyHit], query: str) -> FuzzyHit | None:
        if not hits:
            return None
        return self.matcher.pick_best_hit(query, hits)

    def _match_goods_report(self, name: str) -> GoodsReportItem | None:
        if not self.goods_report:
            return None
        query = normalize_name(name)
        results = process.extract(
            query,
            self._goods_names,
            scorer=fuzz.token_set_ratio,
            limit=1,
        )
        if not results:
            return None
        _, score, idx = results[0]
        if score < SIMILAR_MATCH_THRESHOLD:
            return None
        return self.goods_report[idx]

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
            scorer=fuzz.token_set_ratio,
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
        return PriceQuote(
            source="catalog",
            label="Каталог",
            matched_name=hit.name,
            cost=cost,
            price=item.price or cost,
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
