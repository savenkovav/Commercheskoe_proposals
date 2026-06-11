from __future__ import annotations

import logging
import re
from html import unescape
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from src.config import (
    WEB_SEARCH_EXACT_THRESHOLD,
    WEB_SEARCH_FETCH_PAGES,
    WEB_SEARCH_MAX_PAGE_FETCHES,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_TIMEOUT,
)
from src.services.fuzzy_scoring import name_match_score
from src.services.data_loader import normalize_name
from src.services.models import PriceQuote
from src.services.web_quote_priority import is_marketplace_url, is_product_page_url

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_BLOCKED_HOST_FRAGMENTS = (
    "youtube.com",
    "youtu.be",
    "wikipedia.org",
    "vk.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "pinterest.com",
    "duckduckgo.com",
    "google.com",
    "yandex.ru/search",
    "ya.ru/search",
)

_SEARCH_PAGE_MARKERS = (
    "/search?",
    "/search/",
    "search?text=",
    "search.aspx?search=",
    "catalog/0/search",
    "?q=",
)

_MARKETING_PREFIXES = (
    "купить ",
    "заказать ",
    "цена ",
    "цены ",
    "интернет-магазин ",
    "магазин ",
)

_TITLE_SUFFIXES = (
    " - купить",
    " купить в",
    " купить по",
    " цена",
    " цены",
    " в москве",
    " в спб",
    " в магазине",
    " в интернет-магазине",
    " официальный сайт",
    " интернет-магазин",
)

_ALLOWED_TITLE_TAILS = (
    "",
    "купить",
    "цена",
    "цены",
    "в магазине",
    "в интернет-магазине",
    "официальный сайт",
    "интернет-магазин",
    "от производителя",
    "с доставкой",
)

_RUBLE_AMOUNT = r"(\d{1,3}(?:\s\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"

_PRICE_PATTERNS = (
    re.compile(
        r'(?:"price"|"lowPrice"|"highPrice")\s*:\s*"?(\d[\d\s]{0,9})"?',
        re.I,
    ),
    re.compile(r'data-price=["\'](\d[\d\s]{0,9})["\']', re.I),
    re.compile(
        rf"{_RUBLE_AMOUNT}\s*(?:₽|руб\.?|rub)(?:\s|$|[^\w])",
        re.I,
    ),
    re.compile(r'price["\']?\s*[:=]\s*["\']?(\d[\d\s]{0,9})', re.I),
)

_RESULT_LINK_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
_RESULT_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)',
    re.I | re.S,
)

_MARKETPLACE_SEARCH = (
    ("Ozon", "https://www.ozon.ru/search/?text={query}"),
    ("Яндекс.Маркет", "https://market.yandex.ru/search?text={query}"),
    (
        "Wildberries",
        "https://www.wildberries.ru/catalog/0/search.aspx?search={query}",
    ),
)

_PRODUCT_PATH_RES = (
    re.compile(r'href="(https://www\.ozon\.ru/product/[^"?#]+)"', re.I),
    re.compile(r'href="(https://market\.yandex\.ru/product[^"?#]+)"', re.I),
    re.compile(
        r'href="(https://www\.wildberries\.ru/catalog/\d+/detail\.aspx[^"]*)"',
        re.I,
    ),
)

_OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property="og:title"|name="title")[^>]+content="([^"]+)"',
    re.I,
)

_DOMAIN_LABELS: dict[str, str] = {
    "ozon.ru": "Ozon",
    "market.yandex.ru": "Яндекс.Маркет",
    "wildberries.ru": "Wildberries",
    "citilink.ru": "Ситилинк",
    "dns-shop.ru": "DNS",
    "labstek.ru": "Labstek",
    "medcomp.ru": "Medcomp",
    "chipdip.ru": "ChipDip",
    "vseinstrumenti.ru": "ВсеИнструменты",
}


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _unwrap_redirect_url(url: str) -> str:
    if not url:
        return ""
    if "uddg=" in url:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        encoded = params.get("uddg", [""])[0]
        if encoded:
            return unquote(encoded)
    return url


def _host_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _platform_label(url: str) -> str:
    host = _host_from_url(url)
    for fragment, label in _DOMAIN_LABELS.items():
        if fragment in host:
            return label
    if not host:
        return "Интернет"
    return host.split(".")[0].capitalize()


def _is_blocked_url(url: str) -> bool:
    lower = url.lower()
    if not lower.startswith(("http://", "https://")):
        return True
    host = _host_from_url(url)
    if any(fragment in host or fragment in lower for fragment in _BLOCKED_HOST_FRAGMENTS):
        return True
    return False


def _is_search_listing_url(url: str) -> bool:
    lower = url.lower()
    return any(marker in lower for marker in _SEARCH_PAGE_MARKERS)


def _clean_title_for_match(title: str) -> str:
    text = normalize_name(_strip_html(title))
    for prefix in _MARKETING_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    for suffix in _TITLE_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    text = re.sub(r"\s*[-|]\s*[^-|]+$", "", text).strip()
    return text


def _token_set(text: str) -> set[str]:
    return {token for token in text.split() if token}


def is_exact_title_match(
    query: str,
    title: str,
    threshold: int = WEB_SEARCH_EXACT_THRESHOLD,
    snippet: str = "",
) -> bool:
    normalized_query = normalize_name(query)
    normalized_title = _clean_title_for_match(title)
    raw_title = normalize_name(_strip_html(title))
    raw_snippet = normalize_name(_strip_html(snippet))
    if not normalized_query:
        return False
    if normalized_query in raw_title or normalized_query in raw_snippet:
        return True
    if not normalized_title:
        return False
    if normalized_query == normalized_title:
        return True
    if _token_set(normalized_query) == _token_set(normalized_title):
        return True
    if normalized_title.startswith(normalized_query):
        tail = normalized_title[len(normalized_query) :].strip(" -|:")
        if tail in _ALLOWED_TITLE_TAILS:
            return True
    query_words = normalized_query.split()
    if len(query_words) > 1 and all(word in raw_title.split() for word in query_words):
        return True
    return name_match_score(normalized_query, normalized_title) >= threshold


def _parse_price_value(raw: str) -> float | None:
    text = raw.replace("\xa0", " ").strip()
    text = re.sub(r"\s+", "", text)
    if "," in text and "." in text:
        text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    if 10 <= value <= 50_000_000:
        return round(value, 2)
    return None


def extract_prices_from_text(text: str) -> list[float]:
    prices: list[float] = []
    for pattern in _PRICE_PATTERNS:
        for match in pattern.finditer(text):
            value = _parse_price_value(match.group(1))
            if value is not None:
                prices.append(value)
    return prices


def _pick_best_price(prices: list[float]) -> float | None:
    if not prices:
        return None
    if len(prices) == 1:
        return prices[0]
    prices_sorted = sorted(prices)
    return prices_sorted[len(prices_sorted) // 2]


class WebSearchService:
    def search_offers(
        self,
        product_name: str,
        *,
        limit: int | None = None,
        exact_threshold: int | None = None,
    ) -> list[PriceQuote]:
        query = product_name.strip()
        if not query:
            return []

        max_results = limit or WEB_SEARCH_MAX_RESULTS
        threshold = exact_threshold if exact_threshold is not None else WEB_SEARCH_EXACT_THRESHOLD
        quotes = self._collect_offers(
            query, threshold=threshold, limit=max_results, quoted=True
        )
        if quotes:
            return quotes
        return self._collect_offers(
            query,
            threshold=threshold,
            limit=max_results,
            quoted=False,
        )

    def search_marketplace_offers(
        self,
        product_name: str,
        *,
        limit: int | None = None,
    ) -> list[PriceQuote]:
        query = product_name.strip()
        if not query:
            return []

        max_results = limit or WEB_SEARCH_MAX_RESULTS
        normalized_query = normalize_name(query)
        quotes: list[PriceQuote] = []
        seen_urls: set[str] = set()

        for platform, template in _MARKETPLACE_SEARCH:
            if len(quotes) >= max_results:
                break
            search_url = template.format(query=quote_plus(query))
            search_text, _search_price = self._fetch_page_price(search_url)
            product_url = self._extract_product_url(search_text, platform)
            if not product_url:
                continue

            page_text, page_price = self._fetch_page_price(product_url)
            price = page_price
            title = self._extract_page_title(page_text) or query
            if not is_exact_title_match(
                query,
                title,
                threshold=WEB_SEARCH_EXACT_THRESHOLD,
            ):
                continue
            matched_title = _strip_html(title)

            if price is None and page_text:
                page_prices = extract_prices_from_text(page_text[:120_000])
                price = _pick_best_price(page_prices)

            if price is None:
                continue

            if product_url in seen_urls:
                continue
            seen_urls.add(product_url)
            quotes.append(
                PriceQuote(
                    source="web",
                    label=f"Маркетплейс: {platform}",
                    matched_name=matched_title,
                    price=price,
                    cost=price,
                    match_score=100.0,
                    url=product_url,
                    notes="Карточка товара на маркетплейсе (100% совпадение)",
                )
            )
            if max_results == 1:
                break

        return quotes

    @staticmethod
    def _extract_product_url(page_text: str, platform: str) -> str | None:
        if not page_text:
            return None
        platform_lower = platform.lower()
        for pattern in _PRODUCT_PATH_RES:
            for match in pattern.finditer(page_text):
                url = match.group(1)
                if "ozon" in platform_lower and "ozon.ru/product" not in url.lower():
                    continue
                if "yandex" in platform_lower or "маркет" in platform_lower:
                    if "market.yandex.ru/product" not in url.lower():
                        continue
                if "wildberries" in platform_lower and "wildberries.ru/catalog" not in url.lower():
                    continue
                return url.split("&")[0]
        return None

    @staticmethod
    def _extract_page_title(page_text: str) -> str | None:
        if not page_text:
            return None
        match = _OG_TITLE_RE.search(page_text)
        if match:
            return _strip_html(match.group(1))
        title_match = re.search(r"<title[^>]*>(.*?)</title>", page_text, re.I | re.S)
        if title_match:
            return _strip_html(title_match.group(1))
        return None

    def _collect_offers(
        self,
        query: str,
        *,
        threshold: int,
        limit: int,
        quoted: bool = True,
        require_price: bool = False,
    ) -> list[PriceQuote]:
        serp_hits = self._search_duckduckgo(query, quoted=quoted)
        quotes: list[PriceQuote] = []
        seen_urls: set[str] = set()
        page_fetches = 0

        for hit in serp_hits:
            if len(quotes) >= limit:
                break
            title = hit.get("title") or ""
            url = hit.get("url") or ""
            snippet = hit.get("snippet") or ""
            if not url or url in seen_urls or _is_blocked_url(url):
                continue
            if not is_exact_title_match(
                query, title, threshold=threshold, snippet=snippet
            ):
                continue
            if _is_search_listing_url(url):
                continue

            seen_urls.add(url)
            normalized_query = normalize_name(query)
            raw_title = normalize_name(_strip_html(title))
            raw_snippet = normalize_name(snippet)
            cleaned_title = _clean_title_for_match(title)
            if (
                normalized_query in raw_title
                or normalized_query in raw_snippet
                or _token_set(normalized_query) == _token_set(cleaned_title)
            ):
                match_score = 100.0
            else:
                match_score = name_match_score(normalized_query, cleaned_title)
            prices = extract_prices_from_text(snippet)
            price = _pick_best_price(prices)
            notes = "Совпадение названия в поисковой выдаче"
            if (
                price is None
                and WEB_SEARCH_FETCH_PAGES
                and page_fetches < WEB_SEARCH_MAX_PAGE_FETCHES
            ):
                page_fetches += 1
                page_text, page_price = self._fetch_page_price(url)
                if page_price is not None:
                    price = page_price
                    notes = "Совпадение названия, цена со страницы"
                elif page_text:
                    page_prices = extract_prices_from_text(page_text[:120_000])
                    price = _pick_best_price(page_prices)
                    if price is not None:
                        notes = "Совпадение названия, цена со страницы"

            if match_score < threshold:
                continue

            if require_price and price is None:
                continue

            platform = _platform_label(url)
            if is_marketplace_url(url):
                if not is_product_page_url(url):
                    continue
                label = f"Маркетплейс: {platform}"
            else:
                label = f"Интернет: {platform}"
            quotes.append(
                PriceQuote(
                    source="web",
                    label=label,
                    matched_name=_strip_html(title) or query,
                    price=price,
                    cost=price,
                    match_score=match_score,
                    url=url,
                    notes=notes,
                )
            )
            if price is not None and not is_marketplace_url(url):
                break

        return quotes

    def _search_duckduckgo(self, query: str, *, quoted: bool = True) -> list[dict[str, str]]:
        search_query = f'"{query}"' if quoted else query
        url = "https://html.duckduckgo.com/html/"
        headers = {"User-Agent": _USER_AGENT}
        data = {"q": search_query}

        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=WEB_SEARCH_TIMEOUT,
                headers=headers,
            ) as client:
                response = client.post(url, data=data)
                if response.status_code == 202 and not quoted:
                    response = client.post(url, data={"q": f'"{query}"'})
                if response.status_code == 202:
                    return []
                response.raise_for_status()
                html = response.text
        except Exception:
            logger.warning("DuckDuckGo search failed for %r", query, exc_info=True)
            return []

        hits = self._parse_duckduckgo_html(html)
        if hits or quoted:
            return hits
        return self._search_duckduckgo(query, quoted=True)

    @staticmethod
    def _parse_duckduckgo_html(html: str) -> list[dict[str, str]]:
        links = _RESULT_LINK_RE.findall(html)
        snippets = _RESULT_SNIPPET_RE.findall(html)
        hits: list[dict[str, str]] = []

        for index, (href, title_html) in enumerate(links):
            snippet_html = snippets[index] if index < len(snippets) else ""
            hits.append(
                {
                    "url": _unwrap_redirect_url(unescape(href)),
                    "title": _strip_html(title_html),
                    "snippet": _strip_html(snippet_html),
                }
            )
        return hits

    def _fetch_page_price(self, url: str) -> tuple[str, float | None]:
        headers = {"User-Agent": _USER_AGENT}
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=WEB_SEARCH_TIMEOUT,
                headers=headers,
            ) as client:
                response = client.get(url)
                if response.status_code >= 400:
                    return "", None
                text = response.text[:200_000]
        except Exception:
            logger.debug("Page fetch failed for %s", url, exc_info=True)
            return "", None

        prices = extract_prices_from_text(text)
        return text, _pick_best_price(prices)
