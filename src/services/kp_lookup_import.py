"""Импорт позиций из быстрого поиска в сессию КП."""

from __future__ import annotations

import re
from typing import Any

from src.services.models import (
    MatchResult,
    MatchSource,
    MatchStatus,
    PriceQuote,
    TZItem,
)
from src.services.pricing_rules import apply_kp_pricing

_SIMILAR_THRESHOLD = 80.0
_EXACT_THRESHOLD = 95.0


def lookup_competitor_key(item: dict[str, Any]) -> str:
    url = str(item.get("url") or "").strip()
    if url:
        return url
    label = str(item.get("label") or "").strip()
    name = str(item.get("name") or "").strip()
    return f"{label}|{name}"


def parse_lookup_price_amount(item: dict[str, Any]) -> float | None:
    raw = item.get("price_amount")
    if raw is not None:
        try:
            value = float(raw)
            return value if value >= 0 else None
        except (TypeError, ValueError):
            pass

    for field in ("price", "price_label"):
        text = item.get(field)
        if not text:
            continue
        if isinstance(text, (int, float)):
            value = float(text)
            return value if value >= 0 else None
        match = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", str(text))
        if not match:
            continue
        cleaned = match.group(1).replace(" ", "").replace(",", ".")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        if value >= 0:
            return value
    return None


def _match_status(score: float) -> MatchStatus:
    if score >= _EXACT_THRESHOLD:
        return MatchStatus.EXACT
    if score >= _SIMILAR_THRESHOLD:
        return MatchStatus.SIMILAR
    return MatchStatus.NOT_FOUND


def build_match_result_from_lookup_competitor(
    query_name: str,
    number: int,
    item: dict[str, Any],
    *,
    quantity: float = 1.0,
) -> MatchResult:
    name = str(item.get("name") or query_name).strip() or query_name
    label = str(item.get("label") or "Конкурент").strip() or "Конкурент"
    score = float(item.get("match_score") or 0)
    base_price = parse_lookup_price_amount(item)
    has_price = bool(item.get("has_price", base_price is not None))
    quote = PriceQuote(
        source="web",
        label=label,
        matched_name=name,
        price=base_price,
        cost=base_price,
        price_label=item.get("price_label") or item.get("price"),
        articul=item.get("articul"),
        match_score=score,
        url=str(item.get("url") or "").strip() or None,
        notes=str(item.get("notes") or ""),
        image_url=item.get("image_url"),
        description=item.get("description"),
    )
    tz_item = TZItem(
        number=number,
        name=query_name.strip() or name,
        unit="шт",
        quantity=float(quantity or 1),
        specifications="",
    )
    notes = f"Добавлено из быстрого поиска · {label}"
    if not has_price:
        notes = f"{notes} · цена не указана на сайте"

    result = MatchResult(
        tz_item=tz_item,
        status=_match_status(score),
        source=MatchSource.WEB,
        matched_name=name,
        match_score=score,
        unit_cost=base_price if has_price else None,
        unit_base_price=base_price if has_price else None,
        notes=notes,
        comparison=[quote],
        competitors=[quote],
        internet_priced=has_price,
    )
    apply_kp_pricing(result)
    return result


def find_result_by_lookup_key(
    results: list[MatchResult],
    item: dict[str, Any],
) -> MatchResult | None:
    key = lookup_competitor_key(item)
    url = str(item.get("url") or "").strip()
    for result in results:
        if url:
            for quote in [*result.comparison, *result.competitors]:
                if quote.url == url:
                    return result
        for quote in [*result.comparison, *result.competitors]:
            quote_key = lookup_competitor_key(
                {
                    "url": quote.url,
                    "label": quote.label,
                    "name": quote.matched_name,
                }
            )
            if quote_key == key:
                return result
    return None
