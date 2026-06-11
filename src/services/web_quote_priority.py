from __future__ import annotations

import re

from src.config import WEB_SEARCH_EXACT_THRESHOLD
from src.services.models import PriceQuote

_MARKETPLACE_HOSTS = (
    "ozon.ru",
    "wildberries.ru",
    "market.yandex.ru",
)

_SEARCH_PAGE_MARKERS = (
    "/search?",
    "/search/",
    "search?text=",
    "search.aspx?search=",
    "catalog/0/search",
    "?q=",
)

_WB_PRODUCT_RE = re.compile(
    r"wildberries\.ru/catalog/\d+/detail\.aspx",
    re.IGNORECASE,
)


def is_marketplace_url(url: str | None) -> bool:
    if not url:
        return False
    lower = url.lower()
    return any(host in lower for host in _MARKETPLACE_HOSTS)


def is_search_listing_url(url: str | None) -> bool:
    if not url:
        return False
    lower = url.lower()
    return any(marker in lower for marker in _SEARCH_PAGE_MARKERS)


def is_product_page_url(url: str | None) -> bool:
    if not url:
        return False
    lower = url.lower()
    if is_search_listing_url(url):
        return False
    if "ozon.ru/product/" in lower:
        return True
    if "market.yandex.ru/product/" in lower:
        return True
    if _WB_PRODUCT_RE.search(lower):
        return True
    if is_marketplace_url(url):
        return False
    return lower.startswith(("http://", "https://"))


def is_acceptable_web_pricing_quote(quote: PriceQuote) -> bool:
    if quote.source != "web":
        return True
    has_price = quote.price is not None or quote.cost is not None
    if float(quote.match_score or 0) < WEB_SEARCH_EXACT_THRESHOLD:
        return False
    url = quote.url or ""
    if not url:
        return has_price
    if is_search_listing_url(url):
        return False
    if is_marketplace_url(url):
        return is_product_page_url(url) and has_price
    return has_price


def web_quote_rank_key(quote: PriceQuote) -> tuple[int, int, float, int]:
    url = quote.url or ""
    score = float(quote.match_score or 0)
    has_price = quote.price is not None or quote.cost is not None
    marketplace = is_marketplace_url(url)
    product_page = is_product_page_url(url)
    search_page = is_search_listing_url(url)

    if search_page or not has_price or score < WEB_SEARCH_EXACT_THRESHOLD:
        tier = 9
    elif not marketplace:
        tier = 0
    elif marketplace and product_page:
        tier = 1
    else:
        tier = 2

    return (tier, 0 if product_page else 1, -score, 0 if has_price else 1)


def sort_web_quotes(quotes: list[PriceQuote]) -> list[PriceQuote]:
    web = [quote for quote in quotes if quote.source == "web"]
    other = [quote for quote in quotes if quote.source != "web"]
    web.sort(key=web_quote_rank_key)
    return other + web


def pick_best_web_priced_quote(quotes: list[PriceQuote]) -> PriceQuote | None:
    eligible = [
        quote
        for quote in quotes
        if quote.source == "web" and is_acceptable_web_pricing_quote(quote)
    ]
    if not eligible:
        return None
    return min(eligible, key=web_quote_rank_key)


def pick_internet_url(quotes: list[PriceQuote]) -> str | None:
    eligible = [
        quote
        for quote in quotes
        if quote.source == "web"
        and quote.url
        and is_acceptable_web_pricing_quote(quote)
    ]
    eligible.sort(key=web_quote_rank_key)
    for quote in eligible:
        if is_product_page_url(quote.url):
            return quote.url
    for quote in eligible:
        return quote.url
    return None
