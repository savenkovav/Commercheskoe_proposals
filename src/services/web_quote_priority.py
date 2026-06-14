from __future__ import annotations

import re

from src.config import COMPETITOR_SEARCH_FALLBACK_THRESHOLD, WEB_SEARCH_EXACT_THRESHOLD
from src.services.competitor_sites import (
    is_competitor_url,
    meets_competitor_match_threshold,
    meets_web_display_threshold,
)
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
    if not has_price:
        return False
    url = quote.url or ""
    score = float(quote.match_score or 0)
    if is_competitor_url(url):
        if not meets_competitor_match_threshold(score, strict=False):
            return False
    elif score < WEB_SEARCH_EXACT_THRESHOLD:
        return False
    if not url:
        return has_price
    if is_search_listing_url(url):
        return False
    if is_marketplace_url(url):
        return is_product_page_url(url) and has_price
    return has_price


def is_competitor_display_quote(quote: PriceQuote) -> bool:
    if quote.source != "web":
        return False
    url = quote.url or ""
    if not is_competitor_url(url) or is_search_listing_url(url):
        return False
    return meets_web_display_threshold(url, float(quote.match_score or 0))


def web_quote_rank_key(quote: PriceQuote) -> tuple[int, float, int, float, int]:
    url = quote.url or ""
    score = float(quote.match_score or 0)
    has_price = quote.price is not None or quote.cost is not None
    marketplace = is_marketplace_url(url)
    product_page = is_product_page_url(url)
    search_page = is_search_listing_url(url)
    competitor = is_competitor_url(url)
    price_value = quote.price if quote.price is not None else quote.cost
    price_sort = float(price_value) if price_value is not None else float("inf")

    if search_page or not meets_web_display_threshold(url, score):
        tier = 9
    elif competitor and has_price:
        tier = 0
    elif competitor and not has_price:
        tier = 3
    elif not marketplace and has_price:
        tier = 1
    elif marketplace and product_page and has_price:
        tier = 2
    else:
        tier = 4

    return (tier, price_sort, 0 if product_page else 1, -score, 0 if has_price else 1)


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


def has_priced_competitor_quote(quotes: list[PriceQuote]) -> bool:
    return any(
        quote.source == "web"
        and is_competitor_url(quote.url)
        and is_acceptable_web_pricing_quote(quote)
        for quote in quotes
    )


def has_unpriced_competitor_display_quote(quotes: list[PriceQuote]) -> bool:
    return any(
        quote.source == "web"
        and is_competitor_display_quote(quote)
        and quote.price is None
        and quote.cost is None
        for quote in quotes
    )


def has_acceptable_web_pricing_in_comparison(quotes: list[PriceQuote]) -> bool:
    return any(
        quote.source == "web" and is_acceptable_web_pricing_quote(quote)
        for quote in quotes
    )


def pick_marketplace_priced_quote(quotes: list[PriceQuote]) -> PriceQuote | None:
    eligible = [
        quote
        for quote in quotes
        if quote.source == "web"
        and is_marketplace_url(quote.url)
        and is_acceptable_web_pricing_quote(quote)
    ]
    if not eligible:
        return None
    return min(eligible, key=web_quote_rank_key)


SOURCE_DETAIL_URL_RE = re.compile(r"https?://[^\s|]+")


def parse_source_detail(text: str) -> tuple[str, str | None]:
    if not text:
        return "", None
    match = SOURCE_DETAIL_URL_RE.search(text)
    if not match:
        return text.strip(), None
    url = match.group(0).rstrip("|,;")
    label = text[: match.start()].strip().rstrip("|–—-").strip()
    return label or text.strip(), url


def format_source_detail(label: str, url: str | None) -> str:
    text = (label or "Интернет").strip()
    if url:
        return f"{text} | {url}"
    return text


def resolve_price_source_url(
    quotes: list[PriceQuote],
    *,
    unit_base_price: float | None = None,
    preferred: PriceQuote | None = None,
) -> str | None:
    if preferred and preferred.url:
        return preferred.url

    url = pick_internet_url(quotes, unit_base_price=unit_base_price)
    if url:
        return url

    if unit_base_price is not None:
        for quote in quotes:
            if quote.source != "web" or not quote.url:
                continue
            base = quote.cost if quote.cost is not None else quote.price
            if base is not None and abs(base - unit_base_price) < 0.01:
                if not is_search_listing_url(quote.url):
                    return quote.url
        for quote in quotes:
            if quote.source != "web" or not quote.url:
                continue
            base = quote.cost if quote.cost is not None else quote.price
            if base is not None and abs(base - unit_base_price) < 0.01:
                return quote.url

    for quote in quotes:
        if quote.source == "web" and quote.url and not is_search_listing_url(quote.url):
            return quote.url
    return None


def enrich_source_detail_with_price_url(
    source_detail: str,
    quotes: list[PriceQuote],
    *,
    unit_base_price: float | None = None,
    preferred: PriceQuote | None = None,
) -> str:
    label, existing_url = parse_source_detail(source_detail)
    if existing_url:
        return source_detail

    url = resolve_price_source_url(
        quotes,
        unit_base_price=unit_base_price,
        preferred=preferred,
    )
    if preferred and preferred.label and not label:
        label = preferred.label
    return format_source_detail(label, url)


def pick_internet_url(
    quotes: list[PriceQuote],
    *,
    unit_base_price: float | None = None,
) -> str | None:
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
    if unit_base_price is not None:
        for quote in quotes:
            if quote.source != "web" or not quote.url:
                continue
            base = quote.cost if quote.cost is not None else quote.price
            if base is not None and abs(base - unit_base_price) < 0.01:
                return quote.url
    for quote in quotes:
        if quote.source == "web" and is_competitor_display_quote(quote) and quote.url:
            return quote.url
    for quote in quotes:
        if quote.source == "web" and quote.url and not is_search_listing_url(quote.url):
            return quote.url
    return None
