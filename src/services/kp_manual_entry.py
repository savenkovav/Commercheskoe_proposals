"""Ручной ввод цены для позиций без локального и конкурентного совпадения."""

from __future__ import annotations

from src.config import LOCAL_MATCH_THRESHOLD
from src.services.models import MatchResult, MatchSource, MatchStatus
from src.services.web_quote_priority import (
    is_marketplace_url,
    is_search_listing_url,
    meets_web_display_threshold,
)


def _quote_score(quote) -> float | None:
    if quote.match_score is None:
        return None
    return float(quote.match_score)


def _has_confident_local_price_quote(result: MatchResult) -> bool:
    for quote in result.comparison:
        if quote.source not in ("catalog", "price_list"):
            continue
        score = _quote_score(quote)
        if score is not None and score < LOCAL_MATCH_THRESHOLD:
            continue
        if quote.price is not None or quote.cost is not None:
            return True
    return False


def _has_priced_competitor_quote(result: MatchResult) -> bool:
    """Конкурент с ценой — ручной ввод не нужен (есть строка в интернете)."""
    for quote in [*result.comparison, *result.competitors]:
        if quote.source != "web":
            continue
        url = quote.url or ""
        if is_marketplace_url(url) or is_search_listing_url(url):
            continue
        score = _quote_score(quote)
        if score is None:
            continue
        if not meets_web_display_threshold(url, score):
            continue
        if quote.price is not None or quote.cost is not None:
            return True
    return False


def _has_usable_local_price(result: MatchResult) -> bool:
    if result.source in (MatchSource.CATALOG, MatchSource.PRICE_LIST):
        if (
            result.status != MatchStatus.NOT_FOUND
            and result.unit_base_price is not None
            and result.match_score >= LOCAL_MATCH_THRESHOLD
        ):
            return True
    if result.source == MatchSource.REGISTRY and result.status != MatchStatus.NOT_FOUND:
        return True
    if result.source == MatchSource.WEB and result.unit_base_price is not None and result.internet_priced:
        return True
    return _has_confident_local_price_quote(result)


def allows_custom_manual_entry(result: MatchResult) -> bool:
    """Нет цены из прайса/каталога/реестра и нет цены у конкурента."""
    if result.is_kit and result.kit_components:
        return False
    if _has_usable_local_price(result):
        return False
    if _has_priced_competitor_quote(result):
        return False
    if result.unit_base_price is not None or result.unit_cost is not None:
        return False
    return True
