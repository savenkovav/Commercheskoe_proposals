"""Ручной ввод цены для позиций без локального и конкурентного совпадения."""

from __future__ import annotations

from src.config import LOCAL_MATCH_THRESHOLD
from src.services.models import MatchResult, MatchSource, MatchStatus
from src.services.web_quote_priority import (
    is_marketplace_url,
    is_search_listing_url,
    meets_web_display_threshold,
)


def _local_comparison_has_match(result: MatchResult) -> bool:
    for quote in result.comparison:
        if quote.source not in ("catalog", "registry", "price_list"):
            continue
        if quote.match_score is not None and quote.match_score < LOCAL_MATCH_THRESHOLD:
            continue
        if quote.price is not None or quote.cost is not None:
            return True
        if quote.source == "registry":
            return True
    return False


def _competitor_site_match(result: MatchResult) -> bool:
    for quote in [*result.comparison, *result.competitors]:
        if quote.source != "web":
            continue
        url = quote.url or ""
        if is_marketplace_url(url) or is_search_listing_url(url):
            continue
        score = float(quote.match_score or 0)
        if quote.match_score is None or meets_web_display_threshold(url, score):
            return True
    return False


def allows_custom_manual_entry(result: MatchResult) -> bool:
    """Позиция не найдена в прайсах/каталоге/реестре и на сайтах конкурентов."""
    if result.is_kit and result.kit_components:
        return False
    if result.source in (MatchSource.CATALOG, MatchSource.PRICE_LIST, MatchSource.REGISTRY):
        if result.status != MatchStatus.NOT_FOUND and (
            result.unit_base_price is not None or result.unit_cost is not None
        ):
            return False
    if _local_comparison_has_match(result):
        return False
    if _competitor_site_match(result):
        return False
    return True
