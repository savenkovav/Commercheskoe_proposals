"""Выбор позиций и вариантов совпадения для формирования КП."""

from __future__ import annotations

import copy
from dataclasses import dataclass

from src.config import WEB_PRICE_DISCOUNT_PERCENT
from src.services.models import KitComponentLine, MatchResult, MatchSource, MatchStatus, PriceQuote
from src.services.pricing_rules import apply_kp_pricing
from src.services.web_quote_priority import is_search_listing_url, meets_web_display_threshold, web_quote_rank_key


@dataclass(frozen=True)
class KpSelectionItem:
    number: int
    included: bool = True
    variant: str = "primary"
    kit_indices: tuple[int, ...] | None = None
    web_indices: tuple[int, ...] | None = None
    web_manual_prices: tuple[tuple[int, float], ...] | None = None
    custom_enabled: bool = False
    custom_unit_price: float | None = None
    custom_quantity: float | None = None


def _is_market_estimate_quote(quote: PriceQuote) -> bool:
    label = (quote.label or "").lower()
    return "оценка рынка" in label or "оценка ai" in label


def _local_comparison_quotes(result: MatchResult) -> list[PriceQuote]:
    return [quote for quote in result.comparison if quote.source != "web"]


def _web_comparison_quotes(result: MatchResult) -> list[PriceQuote]:
    seen: set[str] = set()
    quotes: list[PriceQuote] = []
    for quote in [*result.comparison, *result.competitors]:
        if quote.source != "web" or _is_market_estimate_quote(quote):
            continue
        key = quote.url or f"{quote.label}|{quote.matched_name}|{quote.price}"
        if key in seen:
            continue
        seen.add(key)
        quotes.append(quote)
    return quotes


def _web_product_quotes(result: MatchResult) -> list[PriceQuote]:
    quotes: list[PriceQuote] = []
    for quote in _web_comparison_quotes(result):
        if is_search_listing_url(quote.url):
            continue
        score = float(quote.match_score or 0)
        if quote.match_score is not None and not meets_web_display_threshold(
            quote.url,
            score,
        ):
            continue
        quotes.append(quote)
    quotes.sort(key=web_quote_rank_key)
    return quotes


def _should_defer_internet_pricing(result: MatchResult) -> bool:
    if not _web_product_quotes(result):
        return False
    if result.is_kit and result.kit_components:
        return False
    if result.source in (MatchSource.CATALOG, MatchSource.PRICE_LIST, MatchSource.REGISTRY):
        if not result.internet_priced and result.unit_base_price is not None:
            return False
    return bool(result.internet_priced or result.source == MatchSource.WEB)


def _clear_auto_internet_pricing(result: MatchResult) -> None:
    if not _should_defer_internet_pricing(result):
        return
    result.unit_cost = None
    result.unit_base_price = None
    result.unit_price = None
    result.total_cost = None
    result.total_price = None
    result.internet_priced = False


def _quote_by_variant(result: MatchResult, variant: str) -> PriceQuote | None:
    if variant == "primary":
        return None
    if variant.startswith("local:"):
        index = int(variant.split(":", 1)[1])
        quotes = _local_comparison_quotes(result)
        if 0 <= index < len(quotes):
            return quotes[index]
        return None
    if variant.startswith("web:"):
        index = int(variant.split(":", 1)[1])
        quotes = _web_product_quotes(result)
        if 0 <= index < len(quotes):
            return quotes[index]
        return None
    return None


def _source_from_quote(quote: PriceQuote) -> MatchSource:
    mapping = {
        "catalog": MatchSource.CATALOG,
        "registry": MatchSource.REGISTRY,
        "price_list": MatchSource.PRICE_LIST,
        "web": MatchSource.WEB,
        "ai": MatchSource.AI,
    }
    return mapping.get(quote.source, MatchSource.NONE)


def apply_variant_to_result(result: MatchResult, variant: str) -> MatchResult:
    if variant == "primary":
        cloned = copy.deepcopy(result)
        _clear_auto_internet_pricing(cloned)
        apply_kp_pricing(cloned)
        return cloned

    quote = _quote_by_variant(result, variant)
    if quote is None:
        cloned = copy.deepcopy(result)
        apply_kp_pricing(cloned)
        return cloned

    cloned = copy.deepcopy(result)
    cloned.matched_name = quote.matched_name or cloned.matched_name
    cloned.match_score = float(quote.match_score or cloned.match_score)
    cloned.unit_cost = quote.cost if quote.cost is not None else quote.price
    cloned.unit_base_price = quote.price if quote.price is not None else quote.cost
    cloned.supplier = quote.supplier or cloned.supplier
    cloned.purchase_date = quote.purchase_date or cloned.purchase_date
    cloned.source = _source_from_quote(quote)
    cloned.internet_priced = cloned.source == MatchSource.WEB
    if quote.url:
        cloned.source_detail = f"{quote.label} | {quote.url}"
    if quote.notes:
        cloned.notes = quote.notes
    if cloned.unit_base_price is None and cloned.status != MatchStatus.NOT_FOUND:
        cloned.status = MatchStatus.SIMILAR
    apply_kp_pricing(cloned)
    return cloned


def _aggregate_kit_components(
    components: list[KitComponentLine],
) -> tuple[float | None, float | None]:
    costs = [
        round(line.unit_cost * (line.quantity or 1), 2)
        for line in components
        if line.unit_cost is not None
    ]
    prices = [
        round(line.unit_price * (line.quantity or 1), 2)
        for line in components
        if line.unit_price is not None
    ]
    total_cost = round(sum(costs), 2) if costs else None
    total_price = round(sum(prices), 2) if prices else None
    return total_cost, total_price


def apply_kit_component_selection(
    result: MatchResult,
    kit_indices: list[int] | None,
) -> MatchResult:
    if not result.is_kit or not result.kit_components or kit_indices is None:
        return result

    cloned = copy.deepcopy(result)
    valid = sorted({i for i in kit_indices if 0 <= i < len(cloned.kit_components)})
    if not valid:
        cloned.kit_components = []
        cloned.unit_cost = None
        cloned.unit_base_price = None
        cloned.unit_price = None
        cloned.total_cost = None
        cloned.total_price = None
        return cloned

    cloned.kit_components = [cloned.kit_components[i] for i in valid]
    agg_cost, agg_price = _aggregate_kit_components(cloned.kit_components)
    if agg_cost is not None:
        cloned.unit_cost = agg_cost
    if agg_price is not None:
        cloned.unit_base_price = agg_price
    apply_kp_pricing(cloned)
    return cloned


def apply_web_addon_selection(
    result: MatchResult,
    web_indices: list[int] | None,
    web_manual_prices: dict[int, float] | None = None,
) -> MatchResult:
    quotes = _web_product_quotes(result)
    if not quotes:
        return result

    cloned = copy.deepcopy(result)
    if not web_indices:
        _clear_auto_internet_pricing(cloned)
        apply_kp_pricing(cloned)
        return cloned

    valid = sorted({i for i in web_indices if 0 <= i < len(quotes)})
    if not valid:
        _clear_auto_internet_pricing(cloned)
        apply_kp_pricing(cloned)
        return cloned

    manual = {int(key): float(value) for key, value in (web_manual_prices or {}).items()}

    addon_cost = 0.0
    addon_base = 0.0
    addon_kp = 0.0
    for index in valid:
        quote = quotes[index]
        if index in manual:
            base = manual[index]
            cost = manual[index]
        else:
            cost = quote.cost if quote.cost is not None else quote.price
            base = quote.price if quote.price is not None else quote.cost
        if cost is not None:
            addon_cost += float(cost)
        if base is not None:
            addon_base += float(base)
            addon_kp += round(base * (1 - WEB_PRICE_DISCOUNT_PERCENT / 100), 2)

    qty = cloned.tz_item.quantity
    kit_has_components = bool(cloned.is_kit and cloned.kit_components)
    if kit_has_components:
        if addon_cost:
            cloned.unit_cost = round((cloned.unit_cost or 0) + addon_cost, 2)
            cloned.total_cost = round(cloned.unit_cost * qty, 2)
        if addon_base:
            cloned.unit_base_price = round((cloned.unit_base_price or 0) + addon_base, 2)
            cloned.internet_priced = True
        if addon_kp:
            cloned.unit_price = round((cloned.unit_price or 0) + addon_kp, 2)
            cloned.total_price = round(cloned.unit_price * qty, 2)
        return cloned

    cloned.unit_cost = round(addon_cost, 2) if addon_cost else None
    cloned.unit_base_price = round(addon_base, 2) if addon_base else None
    cloned.unit_price = round(addon_kp, 2) if addon_kp else None
    cloned.internet_priced = bool(addon_base)
    cloned.total_cost = round(cloned.unit_cost * qty, 2) if cloned.unit_cost is not None else None
    cloned.total_price = round(cloned.unit_price * qty, 2) if cloned.unit_price is not None else None
    return cloned


def apply_custom_manual_selection(
    result: MatchResult,
    *,
    enabled: bool,
    unit_price: float | None,
    quantity: float | None,
) -> MatchResult:
    if not enabled or unit_price is None or unit_price <= 0:
        return result

    cloned = copy.deepcopy(result)
    if quantity is not None and quantity > 0:
        cloned.tz_item.quantity = float(quantity)
    cloned.matched_name = cloned.tz_item.name
    cloned.source = MatchSource.NONE
    cloned.match_score = 0.0
    cloned.unit_cost = round(float(unit_price), 2)
    cloned.unit_base_price = round(float(unit_price), 2)
    cloned.internet_priced = False
    cloned.notes = "Ручной ввод цены"
    cloned.source_detail = ""
    apply_kp_pricing(cloned)
    return cloned


def apply_kp_selections(
    results: list[MatchResult],
    selections: list[KpSelectionItem],
) -> list[MatchResult]:
    if not selections:
        return [apply_variant_to_result(result, "primary") for result in results]

    by_number = {item.number: item for item in selections}
    selected: list[MatchResult] = []
    for result in results:
        number = result.tz_item.number
        selection = by_number.get(number)
        if selection is None:
            continue
        if not selection.included:
            continue
        applied = apply_variant_to_result(result, selection.variant)
        kit_indices = list(selection.kit_indices) if selection.kit_indices is not None else None
        web_indices = list(selection.web_indices) if selection.web_indices is not None else None
        manual_prices = (
            {index: price for index, price in selection.web_manual_prices}
            if selection.web_manual_prices
            else None
        )
        with_kit = apply_kit_component_selection(applied, kit_indices)
        with_web = apply_web_addon_selection(with_kit, web_indices, manual_prices)
        if selection.custom_enabled and selection.custom_unit_price is not None:
            selected.append(
                apply_custom_manual_selection(
                    with_web,
                    enabled=True,
                    unit_price=selection.custom_unit_price,
                    quantity=selection.custom_quantity,
                )
            )
        else:
            selected.append(with_web)
    return selected
