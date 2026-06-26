from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz, process

from src.config import EXACT_MATCH_THRESHOLD, LOCAL_MATCH_THRESHOLD, SIMILAR_MATCH_THRESHOLD
from src.services.data_loader import format_catalog_supplier, normalize_name
from src.services.fuzzy_scoring import name_match_score
from src.services.meilisearch_service import meilisearch_available, search_products
from src.services.match_tier import effective_score
from src.services.tz_search import (
    build_search_queries,
    combined_match_score,
    is_relevant_match,
    tz_match_query,
)
from src.services.models import (
    CatalogItem,
    MatchResult,
    MatchSource,
    MatchStatus,
    PriceListItem,
    RegistryItem,
    TZItem,
)


@dataclass
class FuzzyHit:
    name: str
    score: float
    payload: object
    source: MatchSource
    detail: str = ""


_CATALOG_DISTINCTIVE_MARKERS = (
    ("голов", "голов"),
    ("натюрморт", "натюрморт"),
    ("натюрмор", "натюрмор"),
    ("растен", "растен"),
    ("гипсов", "гипсов"),
    ("геометрич", "геометрич"),
    ("фрукт", "фрукт"),
    ("овощ", "овощ"),
    ("гриб", "гриб"),
    ("портрет", "портрет"),
    ("художник", "художник"),
    ("муляж", "муляж"),
)


class ItemMatcher:
    def __init__(
        self,
        catalog: list[CatalogItem],
        registry: list[RegistryItem],
        price_lists: list[PriceListItem],
    ) -> None:
        self.catalog = catalog
        self.registry = registry
        self.price_lists = price_lists

        searchable_catalog = [
            item
            for item in catalog
            if item.entry_type in {"item", "kit_total", "sub_kit"}
        ]
        self._searchable_catalog = searchable_catalog
        self._catalog_names = [normalize_name(i.name) for i in searchable_catalog]
        self._registry_names = [normalize_name(i.name) for i in registry]
        self._price_names = [normalize_name(i.name) for i in price_lists]

    def _best_fuzzy(
        self,
        query: str,
        choices: list[str],
        payloads: list,
        source: MatchSource,
        limit: int = 5,
        tz_item: TZItem | None = None,
    ) -> list[FuzzyHit]:
        if not choices:
            return []

        results = process.extract(
            query,
            choices,
            scorer=name_match_score,
            limit=max(limit, 8),
        )

        hits: list[FuzzyHit] = []
        for choice, score, idx in results:
            payload = payloads[idx]
            payload_name = getattr(payload, "name", str(payload))
            if tz_item and not is_relevant_match(
                tz_item,
                payload_name,
                score=float(score),
            ):
                continue
            if tz_item and self.is_distinctive_mismatch(tz_item.name, payload_name):
                continue
            adjusted_score = combined_match_score(tz_item, payload_name) if tz_item else float(score)
            adjusted_score = self._adjust_score(query, choice, adjusted_score)
            detail = ""
            if source == MatchSource.PRICE_LIST and isinstance(payload, PriceListItem):
                detail = f"{payload.supplier} / {payload.sheet} / код {payload.code}"
            elif source == MatchSource.CATALOG and isinstance(payload, CatalogItem):
                supplier = format_catalog_supplier(payload)
                detail_parts = [payload.source_file]
                if payload.row_index:
                    detail_parts.append(f"строка {payload.row_index}")
                if supplier:
                    detail_parts.append(supplier.replace("\n", "; "))
                detail = " / ".join(detail_parts)
            elif source == MatchSource.REGISTRY and isinstance(payload, RegistryItem):
                detail = f"остаток: {payload.quantity} шт."

            hits.append(
                FuzzyHit(
                    name=payload_name,
                    score=adjusted_score,
                    payload=payload,
                    source=source,
                    detail=detail,
                )
            )

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def _search_source(
        self,
        tz_item: TZItem,
        choices: list[str],
        payloads: list,
        source: MatchSource,
        limit: int = 5,
    ) -> list[FuzzyHit]:
        meili_hits = self._search_source_meili(
            tz_item,
            payloads,
            source,
            limit=limit,
        )
        if meili_hits:
            return meili_hits

        return self._search_source_fuzzy(
            tz_item,
            choices,
            payloads,
            source,
            limit=limit,
        )

    def _meili_source_key(self, source: MatchSource) -> str:
        if source == MatchSource.CATALOG:
            return "catalog"
        if source == MatchSource.REGISTRY:
            return "registry"
        return "price_list"

    def _search_source_meili(
        self,
        tz_item: TZItem,
        payloads: list,
        source: MatchSource,
        *,
        limit: int = 5,
    ) -> list[FuzzyHit]:
        if not meilisearch_available():
            return []

        merged: dict[str, FuzzyHit] = {}
        source_key = self._meili_source_key(source)

        try:
            for query in build_search_queries(tz_item):
                for meili_hit in search_products(query, source=source_key, limit=max(limit, 8)):
                    index = meili_hit.source_index
                    if index < 0 or index >= len(payloads):
                        continue
                    payload = payloads[index]
                    payload_name = getattr(payload, "name", str(payload))
                    if not is_relevant_match(
                        tz_item,
                        payload_name,
                        score=float(meili_hit.score),
                    ):
                        continue
                    if self.is_distinctive_mismatch(tz_item.name, payload_name):
                        continue
                    adjusted_score = combined_match_score(tz_item, payload_name)
                    adjusted_score = self._adjust_score(
                        normalize_name(query),
                        normalize_name(payload_name),
                        adjusted_score,
                    )
                    detail = meili_hit.detail or ""
                    if source == MatchSource.PRICE_LIST and isinstance(payload, PriceListItem):
                        detail = detail or f"{payload.supplier} / {payload.sheet} / код {payload.code}"
                    elif source == MatchSource.CATALOG and isinstance(payload, CatalogItem):
                        detail = detail or payload.source_file
                    elif source == MatchSource.REGISTRY and isinstance(payload, RegistryItem):
                        detail = detail or f"остаток: {payload.quantity} шт."

                    hit = FuzzyHit(
                        name=payload_name,
                        score=adjusted_score,
                        payload=payload,
                        source=source,
                        detail=detail,
                    )
                    existing = merged.get(hit.name)
                    if existing is None or hit.score > existing.score:
                        merged[hit.name] = hit
        except Exception:
            return []

        hits = sorted(merged.values(), key=lambda item: item.score, reverse=True)
        return hits[:limit]

    def _search_source_fuzzy(
        self,
        tz_item: TZItem,
        choices: list[str],
        payloads: list,
        source: MatchSource,
        limit: int = 5,
    ) -> list[FuzzyHit]:
        merged: dict[str, FuzzyHit] = {}
        for query in build_search_queries(tz_item):
            norm_query = normalize_name(query)
            for hit in self._best_fuzzy(
                norm_query,
                choices,
                payloads,
                source,
                limit=limit,
                tz_item=tz_item,
            ):
                existing = merged.get(hit.name)
                if existing is None or hit.score > existing.score:
                    merged[hit.name] = hit
        hits = sorted(merged.values(), key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    @staticmethod
    def _adjust_score(query: str, choice: str, base_score: float) -> float:
        score = base_score
        if query == choice:
            return 100.0
        if query in choice or choice in query:
            score = max(score, 95.0)
        # При близких score предпочитаем более длинные и точные названия
        length_bonus = min(len(choice) - len(query), 20) * 0.3
        if query in choice:
            score += length_bonus
        return min(score, 100.0)

    @staticmethod
    def is_distinctive_mismatch(query: str, choice: str) -> bool:
        q = normalize_name(query)
        c = normalize_name(choice)
        for query_marker, choice_marker in _CATALOG_DISTINCTIVE_MARKERS:
            if query_marker in q and choice_marker not in c:
                return True
        return False

    @staticmethod
    def distinctive_markers_agree(query: str, choice: str) -> bool:
        """Наименование ТЗ и кандидат согласованы по отличительным маркерам."""
        q = normalize_name(query)
        c = normalize_name(choice)
        tz_markers = [marker for marker, _ in _CATALOG_DISTINCTIVE_MARKERS if marker in q]
        if not tz_markers:
            return False
        return all(marker in c for marker in tz_markers)

    def pick_best_hit(
        self,
        tz_item: TZItem,
        hits: list[FuzzyHit],
        *,
        min_score: float = LOCAL_MATCH_THRESHOLD,
    ) -> FuzzyHit | None:
        if not hits:
            return None

        filtered = [
            hit
            for hit in hits
            if effective_score(tz_item, hit.name, hit.score) >= min_score
            and is_relevant_match(
                tz_item,
                hit.name,
                score=hit.score,
                min_score=min_score,
            )
            and not (
                hit.source == MatchSource.CATALOG
                and self.is_distinctive_mismatch(tz_item.name, hit.name)
            )
        ]
        hits = filtered
        if not hits:
            return None

        if len(hits) == 1:
            return hits[0]

        norm_query = normalize_name(tz_match_query(tz_item))
        query_tokens = set(norm_query.split())

        def rank_key(hit: FuzzyHit) -> tuple[float, float, int, int, float, int, int]:
            norm_name = normalize_name(hit.name)
            name_tokens = set(norm_name.split())
            token_overlap = len(query_tokens & name_tokens) / max(len(query_tokens), 1)
            combined = combined_match_score(tz_item, hit.name)
            return (
                combined,
                hit.score,
                int(norm_query == norm_name),
                int(norm_query in norm_name or norm_name in norm_query),
                token_overlap,
                len(hit.name),
                int(norm_name in norm_query),
            )

        return max(hits[:8], key=rank_key)

    def rank_hits(self, query: str, hits: list[FuzzyHit], limit: int = 5) -> list[FuzzyHit]:
        relevant = [hit for hit in hits if hit.score >= SIMILAR_MATCH_THRESHOLD]
        if not relevant:
            return []

        norm_query = normalize_name(query)
        query_tokens = set(norm_query.split())

        def rank_key(hit: FuzzyHit) -> tuple[float, int, int, float, int, int]:
            norm_name = normalize_name(hit.name)
            name_tokens = set(norm_name.split())
            token_overlap = len(query_tokens & name_tokens) / max(len(query_tokens), 1)
            return (
                hit.score,
                int(norm_query == norm_name),
                int(norm_query in norm_name),
                token_overlap,
                len(hit.name),
                int(norm_name in norm_query),
            )

        ranked = sorted(relevant[:12], key=rank_key, reverse=True)
        unique: list[FuzzyHit] = []
        seen: set[str] = set()
        for hit in ranked:
            if hit.name in seen:
                continue
            seen.add(hit.name)
            unique.append(hit)
            if len(unique) >= limit:
                break
        return unique

    def find_candidates(self, tz_item: TZItem) -> dict:
        catalog_hits = self._search_source(
            tz_item,
            self._catalog_names,
            self._searchable_catalog,
            MatchSource.CATALOG,
        )
        registry_hits = self._search_source(
            tz_item,
            self._registry_names,
            self.registry,
            MatchSource.REGISTRY,
        )
        price_hits = self._search_source(
            tz_item,
            self._price_names,
            self.price_lists,
            MatchSource.PRICE_LIST,
        )

        return {
            "catalog": catalog_hits,
            "registry": registry_hits,
            "price": price_hits,
        }

    def match_local(
        self,
        tz_item: TZItem,
        *,
        min_score: float | None = None,
    ) -> Optional[MatchResult]:
        floor = float(min_score if min_score is not None else LOCAL_MATCH_THRESHOLD)
        candidates = self.find_candidates(tz_item)

        prioritized: list[FuzzyHit] = []
        for key in ("price", "catalog", "registry"):
            hits = candidates[key]
            if hits:
                best = self.pick_best_hit(tz_item, hits, min_score=floor)
                if best:
                    prioritized.append(best)

        if not prioritized:
            return None

        prioritized.sort(key=lambda h: (effective_score(tz_item, h.name, h.score), len(h.name)), reverse=True)
        best = prioritized[0]

        # При сопоставимом качестве предпочитаем прайс, затем каталог с себестоимостью
        price_best = self.pick_best_hit(tz_item, candidates["price"], min_score=floor)
        if (
            price_best
            and not self.is_distinctive_mismatch(tz_item.name, price_best.name)
            and effective_score(tz_item, price_best.name, price_best.score) >= floor
            and isinstance(price_best.payload, PriceListItem)
            and price_best.payload.price is not None
            and price_best.score >= best.score - 5
        ):
            best = price_best

        catalog_best = self._best_catalog_hit(candidates["catalog"], tz_item)
        if (
            catalog_best
            and not self.is_distinctive_mismatch(tz_item.name, catalog_best.name)
            and effective_score(tz_item, catalog_best.name, catalog_best.score) >= floor
            and isinstance(catalog_best.payload, CatalogItem)
            and catalog_best.payload.cost is not None
            and catalog_best.score >= best.score - 5
            and (not isinstance(best.payload, PriceListItem) or best.score <= catalog_best.score)
        ):
            best = catalog_best

        if self.is_distinctive_mismatch(tz_item.name, best.name):
            return None
        if not is_relevant_match(
            tz_item,
            best.name,
            score=best.score,
            min_score=floor,
        ):
            return None

        min_required = floor
        if best.score < min_required and effective_score(tz_item, best.name, best.score) < min_required:
            return None

        status = (
            MatchStatus.EXACT
            if best.score >= EXACT_MATCH_THRESHOLD
            else MatchStatus.SIMILAR
        )

        unit_cost = self._extract_cost(best)
        unit_base_price = self._extract_base_price(best, candidates)
        source = best.source
        source_detail = best.detail
        matched_name = best.name
        notes = self._build_note(best, status)

        if unit_base_price is None and best.source == MatchSource.CATALOG:
            price_hit = candidates["price"][0] if candidates["price"] else None
            if price_hit and price_hit.score >= LOCAL_MATCH_THRESHOLD:
                fallback_price = self._extract_price(price_hit)
                if fallback_price is not None:
                    unit_base_price = fallback_price
                    if unit_cost is None:
                        source = MatchSource.PRICE_LIST
                    source_detail = price_hit.detail
                    notes = (
                        f"Наименование из каталога; цена из прайса: {price_hit.name}"
                    )

        all_hits = candidates["catalog"] + candidates["registry"] + candidates["price"]
        all_hits.sort(key=lambda h: (h.score, len(h.name)), reverse=True)
        alternatives = [
            h.name
            for h in all_hits[1:4]
            if h.name != best.name
            and not self.is_distinctive_mismatch(tz_item.name, h.name)
            and is_relevant_match(tz_item, h.name, score=h.score)
        ]

        return MatchResult(
            tz_item=tz_item,
            status=status,
            source=source,
            matched_name=matched_name,
            match_score=best.score,
            unit_cost=unit_cost,
            unit_base_price=unit_base_price,
            notes=notes,
            source_detail=source_detail,
            alternatives=alternatives,
        )

    def candidates_for_ai(self, tz_item: TZItem) -> dict:
        hits = self.find_candidates(tz_item)

        def catalog_dict(hit: FuzzyHit) -> dict:
            item: CatalogItem = hit.payload
            return {
                "name": item.name,
                "cost": item.cost,
                "unit": item.unit,
                "score": round(hit.score, 1),
                "row_index": item.row_index,
                "supplier": format_catalog_supplier(item),
                "actual_markup_pct": item.actual_markup_pct,
                "stock": item.stock,
            }

        def registry_dict(hit: FuzzyHit) -> dict:
            item: RegistryItem = hit.payload
            return {
                "name": item.name,
                "quantity": item.quantity,
                "link": item.link,
                "score": round(hit.score, 1),
            }

        def price_dict(hit: FuzzyHit) -> dict:
            item: PriceListItem = hit.payload
            return {
                "name": item.name,
                "code": item.code,
                "price": item.price,
                "supplier": item.supplier,
                "score": round(hit.score, 1),
            }

        return {
            "catalog": [catalog_dict(h) for h in hits["catalog"][:8]],
            "registry": [registry_dict(h) for h in hits["registry"][:5]],
            "price": [price_dict(h) for h in hits["price"][:8]],
        }

    def _best_catalog_hit(
        self, hits: list[FuzzyHit], tz_item: TZItem
    ) -> Optional[FuzzyHit]:
        if not hits:
            return None

        query = normalize_name(tz_match_query(tz_item))
        query_tokens = set(query.split())

        scored_hits: list[tuple[float, FuzzyHit]] = []
        for hit in hits[:5]:
            if not isinstance(hit.payload, CatalogItem) or hit.payload.cost is None:
                continue
            choice = normalize_name(hit.name)
            token_overlap = len(query_tokens & set(choice.split())) / max(len(query_tokens), 1)
            specificity = len(choice) / max(len(query), 1)
            ranking = hit.score + token_overlap * 8 + min(specificity, 1.5) * 3
            scored_hits.append((ranking, hit))

        if not scored_hits:
            return hits[0]

        scored_hits.sort(key=lambda x: x[0], reverse=True)
        return scored_hits[0][1]

    @staticmethod
    def _extract_cost(hit: FuzzyHit) -> Optional[float]:
        payload = hit.payload
        if isinstance(payload, CatalogItem) and payload.cost is not None:
            return payload.cost
        return None

    @staticmethod
    def _extract_price(hit: FuzzyHit) -> Optional[float]:
        payload = hit.payload
        if isinstance(payload, CatalogItem) and payload.price is not None:
            return payload.price
        if isinstance(payload, PriceListItem):
            return payload.price
        return None

    def _extract_base_price(self, hit: FuzzyHit, candidates: dict) -> Optional[float]:
        price = self._extract_price(hit)
        if price is not None:
            return price

        if hit.source == MatchSource.CATALOG:
            price_hit = candidates["price"][0] if candidates["price"] else None
            if price_hit and price_hit.score >= LOCAL_MATCH_THRESHOLD:
                return self._extract_price(price_hit)

        return None

    @staticmethod
    def _build_note(hit: FuzzyHit, status: MatchStatus) -> str:
        if status == MatchStatus.EXACT:
            return "Точное совпадение по наименованию"
        return "Похожая позиция — требуется проверка менеджером"
