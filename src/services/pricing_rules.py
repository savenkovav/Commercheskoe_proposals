from __future__ import annotations

from src.config import WEB_PRICE_DISCOUNT_PERCENT
from src.services.markup_settings import get_markup_percent
from src.services.models import MatchResult, MatchSource


def uses_web_discount_pricing(result: MatchResult) -> bool:
    return result.internet_priced and result.unit_base_price is not None


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
    else:
        multiplier = 1 + get_markup_percent() / 100
        result.unit_price = round(result.unit_base_price * multiplier, 2)

    result.total_price = round(result.unit_price * qty, 2)


def pricing_adjustment_label(result: MatchResult) -> str:
    if uses_web_discount_pricing(result):
        return f"−{WEB_PRICE_DISCOUNT_PERCENT}% от цены в интернете"
    markup = get_markup_percent()
    return f"+{markup}%"
