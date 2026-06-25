"""Проверка локальной цены в каталоге и прайсах (без интернета)."""

from __future__ import annotations

from src.config import LOCAL_MATCH_THRESHOLD
from src.services.matcher import FuzzyHit, ItemMatcher
from src.services.models import CatalogItem, PriceListItem, TZItem
from src.services.tz_search import is_relevant_match


def has_local_catalog_or_price_list_price(
    tz_item: TZItem,
    catalog_hit: FuzzyHit | None,
    price_hit: FuzzyHit | None,
    matcher: ItemMatcher,
    *,
    min_score: float = LOCAL_MATCH_THRESHOLD,
) -> bool:
    """True, если в каталоге или прайсе есть уверенное совпадение с ценой/себестоимостью."""
    if catalog_hit and catalog_hit.score >= min_score:
        if isinstance(catalog_hit.payload, CatalogItem):
            item = catalog_hit.payload
            if item.cost is not None or item.price is not None:
                if is_relevant_match(tz_item, catalog_hit.name, score=catalog_hit.score, min_score=min_score):
                    if not matcher.is_distinctive_mismatch(tz_item.name, catalog_hit.name):
                        return True

    if price_hit and price_hit.score >= min_score:
        if isinstance(price_hit.payload, PriceListItem):
            if price_hit.payload.price is not None:
                if is_relevant_match(tz_item, price_hit.name, score=price_hit.score, min_score=min_score):
                    return True

    return False
