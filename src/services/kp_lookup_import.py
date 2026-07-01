"""Импорт позиций из быстрого поиска в сессию КП."""

from __future__ import annotations

import re
from typing import Any

from rapidfuzz import process

from src.config import LOCAL_MATCH_THRESHOLD
from src.services.data_loader import normalize_name
from src.services.fuzzy_scoring import name_match_score
from src.services.models import (
    GoodsReportItem,
    MatchResult,
    MatchSource,
    MatchStatus,
    PriceQuote,
    TZItem,
)
from src.services.pricing_rules import apply_kp_pricing

_SIMILAR_THRESHOLD = 80.0
_EXACT_THRESHOLD = 95.0


def lookup_item_key(item: dict[str, Any]) -> str:
    source = str(item.get("source_type") or "").strip().lower()
    if not source and item.get("url"):
        source = "competitor"

    name = str(item.get("name") or item.get("matched_name") or "").strip()

    if source == "catalog":
        row_index = item.get("row_index")
        if row_index is not None:
            return f"catalog|{name}|row:{row_index}"
        return f"catalog|{name}"

    if source in {"price_list", "price"}:
        code = str(item.get("code") or "").strip()
        supplier = str(item.get("supplier") or "").strip()
        return f"price_list|{name}|code:{code}|supplier:{supplier}"

    if source == "registry":
        link = str(item.get("link") or "").strip()
        if link:
            return f"registry|{name}|link:{link}"
        return f"registry|{name}"

    url = str(item.get("url") or "").strip()
    if url:
        return url
    label = str(item.get("label") or "").strip()
    return f"{label}|{name}"


def lookup_competitor_key(item: dict[str, Any]) -> str:
    """Обратная совместимость для конкурентных позиций."""
    return lookup_item_key(item)


def parse_lookup_price_amount(item: dict[str, Any]) -> float | None:
    raw = item.get("price_amount")
    if raw is not None:
        try:
            value = float(raw)
            return value if value >= 0 else None
        except (TypeError, ValueError):
            pass

    for field in ("price", "price_label", "cost", "cost_amount"):
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


def _goods_source_priority(item: GoodsReportItem) -> int:
    source_file = str(item.source_file or "")
    if source_file.startswith("stock:"):
        return 3
    if source_file.startswith("procurement:"):
        return 1
    return 2


def _goods_cost_for_name(name: str) -> GoodsReportItem | None:
    from src.services.app_state import get_processor

    processor = get_processor()
    goods_report = getattr(processor, "goods_report", None) or []
    query = normalize_name(name)
    if not query or not goods_report:
        return None

    goods_names = [normalize_name(item.name) for item in goods_report]
    results = process.extract(
        query,
        goods_names,
        scorer=name_match_score,
        limit=3,
    )
    best: GoodsReportItem | None = None
    best_rank = (-1.0, -1)
    for _, score, idx in results:
        item = goods_report[idx]
        if item.cost is None:
            continue
        if normalize_name(item.name) != query and score < LOCAL_MATCH_THRESHOLD:
            continue
        rank = (score, _goods_source_priority(item))
        if rank > best_rank:
            best_rank = rank
            best = item
    return best


def _build_tz_item(query_name: str, item: dict[str, Any], *, quantity: float = 1.0) -> TZItem:
    name = str(item.get("name") or query_name).strip() or query_name
    unit = str(item.get("unit") or "шт").strip() or "шт"
    return TZItem(
        number=0,
        name=query_name.strip() or name,
        unit=unit,
        quantity=float(quantity or 1),
        specifications="",
    )


def build_match_result_from_lookup_catalog(
    query_name: str,
    number: int,
    item: dict[str, Any],
    *,
    quantity: float = 1.0,
) -> MatchResult:
    name = str(item.get("name") or query_name).strip() or query_name
    score = float(item.get("match_score") or 0)
    unit_cost = parse_lookup_price_amount(item)
    unit_base = parse_lookup_price_amount(
        {"price": item.get("price"), "price_amount": item.get("price_amount")}
    ) or unit_cost
    tz_item = _build_tz_item(query_name, item, quantity=quantity)
    tz_item.number = number

    result = MatchResult(
        tz_item=tz_item,
        status=_match_status(score),
        source=MatchSource.CATALOG,
        matched_name=name,
        match_score=score,
        unit_cost=unit_cost,
        unit_base_price=unit_base,
        notes="Добавлено из быстрого поиска · Каталог",
        source_detail=f"Каталог: {name}",
        supplier=item.get("supplier"),
        lookup_kp_key=lookup_item_key({**item, "source_type": "catalog"}),
    )
    apply_kp_pricing(result)
    return result


def build_match_result_from_lookup_price_list(
    query_name: str,
    number: int,
    item: dict[str, Any],
    *,
    quantity: float = 1.0,
) -> MatchResult:
    name = str(item.get("name") or query_name).strip() or query_name
    score = float(item.get("match_score") or 0)
    unit_price = parse_lookup_price_amount(item)
    tz_item = _build_tz_item(query_name, item, quantity=quantity)
    tz_item.number = number

    result = MatchResult(
        tz_item=tz_item,
        status=_match_status(score),
        source=MatchSource.PRICE_LIST,
        matched_name=name,
        match_score=score,
        unit_cost=unit_price,
        unit_base_price=unit_price,
        notes="Добавлено из быстрого поиска · Прайс",
        source_detail=f"Прайс: {name}",
        supplier=item.get("supplier"),
        lookup_kp_key=lookup_item_key({**item, "source_type": "price_list"}),
    )
    apply_kp_pricing(result)
    return result


def build_match_result_from_lookup_registry(
    query_name: str,
    number: int,
    item: dict[str, Any],
    *,
    quantity: float = 1.0,
) -> MatchResult:
    name = str(item.get("name") or query_name).strip() or query_name
    score = float(item.get("match_score") or 0)
    stock_cost = _goods_cost_for_name(name)
    unit_cost = stock_cost.cost if stock_cost else None
    unit_base = None
    if stock_cost:
        unit_base = stock_cost.price or stock_cost.cost
    tz_item = _build_tz_item(query_name, item, quantity=quantity)
    tz_item.number = number

    notes = "Добавлено из быстрого поиска · Реестр остатков"
    if stock_cost is None:
        notes = f"{notes} · себестоимость не найдена в товарном отчёте"

    result = MatchResult(
        tz_item=tz_item,
        status=_match_status(score),
        source=MatchSource.REGISTRY,
        matched_name=name,
        match_score=score,
        unit_cost=unit_cost,
        unit_base_price=unit_base,
        notes=notes,
        source_detail=f"Остатки: {name}",
        supplier=stock_cost.supplier if stock_cost else None,
        purchase_date=stock_cost.purchase_date if stock_cost else None,
        lookup_kp_key=lookup_item_key({**item, "source_type": "registry"}),
    )
    apply_kp_pricing(result)
    return result


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
    )
    tz_item = _build_tz_item(query_name, item, quantity=quantity)
    tz_item.number = number
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
        lookup_kp_key=lookup_item_key({**item, "source_type": "competitor"}),
    )
    apply_kp_pricing(result)
    return result


def build_match_result_from_lookup_item(
    query_name: str,
    number: int,
    item: dict[str, Any],
    *,
    quantity: float = 1.0,
) -> MatchResult:
    source = str(item.get("source_type") or "").strip().lower()
    if not source and item.get("url"):
        source = "competitor"
    if source == "catalog":
        return build_match_result_from_lookup_catalog(
            query_name, number, item, quantity=quantity
        )
    if source in {"price_list", "price"}:
        return build_match_result_from_lookup_price_list(
            query_name, number, item, quantity=quantity
        )
    if source == "registry":
        return build_match_result_from_lookup_registry(
            query_name, number, item, quantity=quantity
        )
    return build_match_result_from_lookup_competitor(
        query_name, number, item, quantity=quantity
    )


def find_result_by_lookup_key(
    results: list[MatchResult],
    item: dict[str, Any],
) -> MatchResult | None:
    key = lookup_item_key(item)
    url = str(item.get("url") or "").strip()
    for result in results:
        if result.lookup_kp_key == key:
            return result
        if url:
            for quote in [*result.comparison, *result.competitors]:
                if quote.url == url:
                    return result
        for quote in [*result.comparison, *result.competitors]:
            quote_key = lookup_item_key(
                {
                    "source_type": "competitor",
                    "url": quote.url,
                    "label": quote.label,
                    "name": quote.matched_name,
                }
            )
            if quote_key == key:
                return result
        if result.source == MatchSource.CATALOG and key.startswith("catalog|"):
            catalog_key = lookup_item_key(
                {"source_type": "catalog", "name": result.matched_name}
            )
            if catalog_key == key:
                return result
        if result.source == MatchSource.PRICE_LIST and key.startswith("price_list|"):
            price_key = lookup_item_key(
                {
                    "source_type": "price_list",
                    "name": result.matched_name,
                    "code": "",
                    "supplier": result.supplier or "",
                }
            )
            if price_key == key:
                return result
        if result.source == MatchSource.REGISTRY and key.startswith("registry|"):
            registry_key = lookup_item_key(
                {"source_type": "registry", "name": result.matched_name}
            )
            if registry_key == key:
                return result
    return None
