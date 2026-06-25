from __future__ import annotations

from src.config import WEB_PRICE_DISCOUNT_PERCENT
from src.services.markup_settings import get_markup_percent
from src.services.models import MatchResult, MatchSource


def uses_web_discount_pricing(result: MatchResult) -> bool:
    return result.internet_priced and result.unit_base_price is not None


def is_internet_sourced_result(result: MatchResult) -> bool:
    return bool(result.internet_priced or result.source == MatchSource.WEB)


def effective_markup_percent(result: MatchResult) -> float | None:
    if result.applied_markup_pct is not None:
        return result.applied_markup_pct
    if uses_web_discount_pricing(result):
        return -WEB_PRICE_DISCOUNT_PERCENT
    if (
        result.unit_base_price is not None
        and result.unit_price is not None
        and result.unit_base_price != 0
    ):
        return round(
            (result.unit_price / result.unit_base_price - 1) * 100,
            2,
        )
    return get_markup_percent()


def format_markup_percent(value: float | None) -> str:
    if value is None:
        return "—"
    rounded = round(float(value), 2)
    if rounded > 0:
        return f"+{rounded:g}%"
    if rounded < 0:
        return f"−{abs(rounded):g}%"
    return "0%"


def apply_kp_pricing(result: MatchResult) -> None:
    qty = result.tz_item.quantity

    if result.unit_cost is not None:
        result.total_cost = round(result.unit_cost * qty, 2)
    else:
        result.total_cost = None

    if result.unit_base_price is None:
        result.unit_price = None
        result.total_price = None
        return

    if uses_web_discount_pricing(result):
        multiplier = 1 - WEB_PRICE_DISCOUNT_PERCENT / 100
        result.unit_price = round(result.unit_base_price * multiplier, 2)
        result.applied_markup_pct = -WEB_PRICE_DISCOUNT_PERCENT
    else:
        multiplier = 1 + get_markup_percent() / 100
        result.unit_price = round(result.unit_base_price * multiplier, 2)
        result.applied_markup_pct = get_markup_percent()

    result.total_price = round(result.unit_price * qty, 2)


def pricing_adjustment_label(result: MatchResult) -> str:
    if uses_web_discount_pricing(result):
        return format_markup_percent(-WEB_PRICE_DISCOUNT_PERCENT)
    return format_markup_percent(get_markup_percent())
