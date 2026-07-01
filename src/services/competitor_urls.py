from __future__ import annotations

import re
from urllib.parse import quote_plus

_PLATFORM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ozon", re.I), "https://www.ozon.ru/search/?text={query}"),
    (re.compile(r"яндекс|yandex|маркет", re.I), "https://market.yandex.ru/search?text={query}"),
    (re.compile(r"wildberries|вайлдберриз", re.I), "https://www.wildberries.ru/catalog/0/search.aspx?search={query}"),
    (re.compile(r"ситилинк|citilink", re.I), "https://www.citilink.ru/search/?text={query}"),
]

_DEFAULT_PLATFORMS = (
    "https://www.ozon.ru/search/?text={query}",
    "https://market.yandex.ru/search?text={query}",
    "https://www.wildberries.ru/catalog/0/search.aspx?search={query}",
)


def build_marketplace_search_url(platform: str, query: str) -> str:
    text = query.strip()
    if not text:
        return ""

    for pattern, template in _PLATFORM_PATTERNS:
        if pattern.search(platform):
            return template.format(query=quote_plus(text))

    return _DEFAULT_PLATFORMS[0].format(query=quote_plus(text))


def resolve_competitor_url(
    platform: str,
    product_name: str,
    url: str | None = None,
) -> str:
    if url and str(url).strip().lower().startswith(("http://", "https://")):
        return str(url).strip()
    return build_marketplace_search_url(platform, product_name)


def _url_is_excluded(url: str, excluded_platforms: list[str]) -> bool:
    if not excluded_platforms:
        return False
    haystack = url.lower()
    for token in excluded_platforms:
        if not token:
            continue
        token_lower = token.lower()
        if token_lower in haystack:
            return True
        for pattern, _ in _PLATFORM_PATTERNS:
            if pattern.search(token_lower) and pattern.search(haystack):
                return True
    return False


def competitor_urls_for_item(
    competitors: list,
    product_name: str,
    limit: int = 3,
    excluded_platforms: list[str] | None = None,
    allow_fallback: bool = True,
) -> list[str]:
    excluded = excluded_platforms or []
    urls: list[str] = []
    seen: set[str] = set()

    for offer in competitors:
        if not isinstance(offer, dict):
            continue
        platform = str(offer.get("platform") or offer.get("label") or "Интернет")
        if any(token.lower() in platform.lower() for token in excluded if token):
            continue
        name = str(offer.get("matched_name") or offer.get("name") or product_name)
        url = resolve_competitor_url(platform, name, offer.get("url"))
        if url and url not in seen and not _url_is_excluded(url, excluded):
            seen.add(url)
            urls.append(url)
        if len(urls) >= limit:
            return urls

    if not allow_fallback:
        return urls[:limit]

    platform_labels = ("Ozon", "Яндекс.Маркет", "Wildberries")
    for label in platform_labels:
        if len(urls) >= limit:
            break
        if any(token.lower() in label.lower() for token in excluded if token):
            continue
        url = build_marketplace_search_url(label, product_name)
        if url and url not in seen and not _url_is_excluded(url, excluded):
            seen.add(url)
            urls.append(url)

    return urls[:limit]


def competitor_urls_from_quotes(
    quotes: list,
    product_name: str,
    limit: int = 3,
    excluded_platforms: list[str] | None = None,
    allow_fallback: bool = True,
) -> list[str]:
    offers = [
        {
            "platform": quote.label.replace("Интернет: ", ""),
            "name": quote.matched_name,
            "url": quote.url,
        }
        for quote in quotes
        if getattr(quote, "source", None) == "web"
    ]
    return competitor_urls_for_item(
        offers,
        product_name,
        limit=limit,
        excluded_platforms=excluded_platforms,
        allow_fallback=allow_fallback,
    )
