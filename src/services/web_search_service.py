from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from src.config import (
    COMPETITOR_NATIVE_SEARCH_ENABLED,
    COMPETITOR_NATIVE_SEARCH_MAX_FETCHES,
    COMPETITOR_SEARCH_BATCH_SIZE,
    COMPETITOR_SEARCH_ENABLED,
    COMPETITOR_SEARCH_FALLBACK_THRESHOLD,
    COMPETITOR_SEARCH_MAX_RESULTS,
    COMPETITOR_SEARCH_PER_DOMAIN_MAX,
    COMPETITOR_SEARCH_PARALLEL_WORKERS,
    COMPETITOR_SEARCH_TIMEOUT,
    INTERNET_SEARCH_BUDGET_SECONDS,
    WEB_SEARCH_EXACT_THRESHOLD,
    WEB_SEARCH_FETCH_PAGES,
    WEB_SEARCH_MAX_PAGE_FETCHES,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_PAGE_MAX_CHARS,
    WEB_SEARCH_RESULTS_PAGE_MAX_CHARS,
    WEB_SEARCH_TIMEOUT,
)
from src.services.competitor_sites import (
    CompetitorSearchHit,
    CompetitorSite,
    build_competitor_search_url,
    competitor_label_for_url,
    competitor_sites_with_search,
    extract_competitor_product_urls,
    is_competitor_asset_url,
    is_competitor_product_page_url,
    is_competitor_url,
    iter_competitor_domain_batches,
    parse_competitor_search_results,
)
from src.services.fuzzy_scoring import name_match_score
from src.services.data_loader import normalize_name
from src.services.models import PriceQuote
from src.services.web_quote_priority import (
    has_indexed_competitor_catalog_quote,
    has_priced_competitor_quote,
    is_marketplace_url,
    is_product_page_url,
    pick_best_web_priced_quote,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SearchDeadline:
    """Ограничение времени на интернет-поиск по одной позиции."""

    monotonic_deadline: float | None = None

    @classmethod
    def from_budget(cls, budget_seconds: float) -> "_SearchDeadline":
        if budget_seconds <= 0:
            return cls(None)
        return cls(time.monotonic() + budget_seconds)

    def expired(self) -> bool:
        return (
            self.monotonic_deadline is not None
            and time.monotonic() >= self.monotonic_deadline
        )

    def remaining(self) -> float | None:
        if self.monotonic_deadline is None:
            return None
        return max(0.0, self.monotonic_deadline - time.monotonic())

    def request_timeout(self, default: float) -> float:
        remaining = self.remaining()
        if remaining is None:
            return default
        if remaining <= 0:
            return 0.1
        return min(default, remaining)


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
    re.compile(r'itemprop="price"[^>]*content="(\d[\d\s.]{0,12})"', re.I),
    re.compile(r'class="price_value"[^>]*>\s*([^<]+)\s*</span>', re.I),
    re.compile(r'class="price"[^>]*data-value="(\d[\d\s.]{0,12})"', re.I),
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
    if is_competitor_asset_url(url):
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


PRICE_ON_REQUEST_LABEL = "По запросу"
_PRICE_ON_REQUEST_RE = re.compile(
    r"цена\s+по\s+запросу|class=\"price\"[^>]{0,80}>[^<]{0,160}по\s+запросу",
    re.I | re.S,
)


def price_on_request_label(text: str) -> str | None:
    if not text:
        return None
    if _PRICE_ON_REQUEST_RE.search(text):
        return PRICE_ON_REQUEST_LABEL
    return None


def extract_prices_from_text(text: str) -> list[float]:
    prices: list[float] = []
    for pattern in _PRICE_PATTERNS:
        for match in pattern.finditer(text):
            value = _parse_price_value(match.group(1))
            if value is not None:
                prices.append(value)
    return prices


def _extract_articul_from_page(page_text: str) -> str | None:
    if not page_text:
        return None
    snippets: list[str] = []
    if len(page_text) > 200_000:
        snippets.append(page_text[-200_000:])
    snippets.append(page_text[:200_000])
    for snippet in snippets:
        match = re.search(
            r'class="block_articule"[^>]*>.*?Артикул:\s*([A-Za-z0-9\-_.]+)',
            snippet,
            re.I | re.S,
        )
        if match:
            return match.group(1)
        match = re.search(r"Артикул:\s*(?:<span>)?([A-Za-z0-9\-_.]+)", snippet, re.I)
        if match:
            return match.group(1)
    return None


def _pick_best_price(prices: list[float]) -> float | None:
    if not prices:
        return None
    if len(prices) == 1:
        return prices[0]
    return min(prices)


def _quote_has_display_price(quote: PriceQuote) -> bool:
    return (
        quote.price is not None
        or quote.cost is not None
        or bool(quote.price_label)
    )


def merge_competitor_quotes(
    quotes: list[PriceQuote],
    new_quotes: list[PriceQuote],
    *,
    seen_urls: set[str] | None = None,
) -> None:
    for quote in new_quotes:
        if quote.url and not is_competitor_product_page_url(quote.url):
            continue
        if quote.url:
            replaced = False
            for index, existing in enumerate(quotes):
                if existing.url != quote.url:
                    continue
                if _quote_has_display_price(quote) and not _quote_has_display_price(existing):
                    quotes[index] = quote
                elif (
                    quote.wholesale_price is not None
                    and existing.wholesale_price is None
                    and _quote_has_display_price(existing)
                ):
                    quotes[index] = PriceQuote(
                        source=existing.source,
                        label=existing.label,
                        matched_name=existing.matched_name,
                        price=existing.price,
                        cost=existing.cost,
                        price_label=existing.price_label,
                        wholesale_price=quote.wholesale_price,
                        articul=existing.articul or quote.articul,
                        supplier=existing.supplier,
                        purchase_date=existing.purchase_date,
                        match_score=existing.match_score,
                        url=existing.url,
                        notes=existing.notes,
                        image_url=existing.image_url or quote.image_url,
                    )
                elif quote.articul and not existing.articul:
                    quotes[index] = PriceQuote(
                        source=existing.source,
                        label=existing.label,
                        matched_name=existing.matched_name,
                        price=existing.price,
                        cost=existing.cost,
                        price_label=existing.price_label,
                        wholesale_price=existing.wholesale_price,
                        articul=quote.articul,
                        supplier=existing.supplier,
                        purchase_date=existing.purchase_date,
                        match_score=existing.match_score,
                        url=existing.url,
                        notes=existing.notes,
                        image_url=existing.image_url or quote.image_url,
                    )
                replaced = True
                break
            if replaced:
                continue
            if any(existing.url == quote.url for existing in quotes):
                continue
            if seen_urls is not None:
                seen_urls.add(quote.url)
        quotes.append(quote)


class WebSearchService:
    def search_offers(
        self,
        product_name: str,
        *,
        limit: int | None = None,
        exact_threshold: int | None = None,
        deadline: _SearchDeadline | None = None,
    ) -> list[PriceQuote]:
        if deadline and deadline.expired():
            return []
        query = product_name.strip()
        if not query:
            return []

        max_results = limit or WEB_SEARCH_MAX_RESULTS
        threshold = exact_threshold if exact_threshold is not None else WEB_SEARCH_EXACT_THRESHOLD
        quotes = self._collect_offers(
            query,
            threshold=threshold,
            limit=max_results,
            quoted=True,
            deadline=deadline,
        )
        if quotes:
            return quotes
        if deadline and deadline.expired():
            return []
        return self._collect_offers(
            query,
            threshold=threshold,
            limit=max_results,
            quoted=False,
            deadline=deadline,
        )

    def search_competitor_offers(
        self,
        product_name: str,
        *,
        limit: int | None = None,
        exact_threshold: int | None = None,
        deadline: _SearchDeadline | None = None,
        allow_fallback: bool = True,
        sort_by_match: bool = False,
    ) -> list[PriceQuote]:
        if not COMPETITOR_SEARCH_ENABLED:
            return []
        if deadline and deadline.expired():
            return []

        query = product_name.strip()
        if not query:
            return []

        max_results = limit or max(
            COMPETITOR_SEARCH_MAX_RESULTS,
            len(competitor_sites_with_search()) * COMPETITOR_SEARCH_PER_DOMAIN_MAX,
        )
        strict_threshold = (
            exact_threshold if exact_threshold is not None else WEB_SEARCH_EXACT_THRESHOLD
        )
        quotes: list[PriceQuote] = []
        seen_urls: set[str] = set()

        def _extend(new_quotes: list[PriceQuote]) -> None:
            merge_competitor_quotes(quotes, new_quotes, seen_urls=seen_urls)

        def _ready_from_index() -> bool:
            return has_priced_competitor_quote(quotes) or has_indexed_competitor_catalog_quote(
                quotes
            )

        if not (deadline and deadline.expired()):
            _extend(
                self._search_competitor_via_rag(
                    query,
                    limit=max_results,
                )
            )
        if _ready_from_index():
            return self._finalize_competitor_quotes(
                quotes, max_results, sort_by_match=sort_by_match
            )

        if COMPETITOR_NATIVE_SEARCH_ENABLED and not (deadline and deadline.expired()):
            _extend(
                self._search_competitor_via_native(
                    query,
                    threshold=strict_threshold,
                    limit=max_results,
                    seen_urls=seen_urls,
                    deadline=deadline,
                )
            )
        if has_priced_competitor_quote(quotes):
            return self._finalize_competitor_quotes(
                quotes, max_results, sort_by_match=sort_by_match
            )

        if deadline and deadline.expired():
            return self._finalize_competitor_quotes(
                quotes, max_results, sort_by_match=sort_by_match
            )

        _extend(
            self._search_competitor_via_ddg(
                query,
                threshold=strict_threshold,
                limit=max_results,
                deadline=deadline,
            )
        )
        if has_priced_competitor_quote(quotes):
            return self._finalize_competitor_quotes(
                quotes, max_results, sort_by_match=sort_by_match
            )

        if (
            allow_fallback
            and COMPETITOR_SEARCH_FALLBACK_THRESHOLD < strict_threshold
            and not (deadline and deadline.expired())
            and (deadline is None or (deadline.remaining() or 0) >= 4)
        ):
            if COMPETITOR_NATIVE_SEARCH_ENABLED:
                _extend(
                    self._search_competitor_via_native(
                        query,
                        threshold=COMPETITOR_SEARCH_FALLBACK_THRESHOLD,
                        limit=max_results,
                        seen_urls=seen_urls,
                        deadline=deadline,
                    )
                )
            if has_priced_competitor_quote(quotes):
                return self._finalize_competitor_quotes(
                    quotes, max_results, sort_by_match=sort_by_match
                )

            if not (deadline and deadline.expired()):
                _extend(
                    self._search_competitor_via_ddg(
                        query,
                        threshold=COMPETITOR_SEARCH_FALLBACK_THRESHOLD,
                        limit=max_results,
                        deadline=deadline,
                    )
                )

        return self._finalize_competitor_quotes(
            quotes, max_results, sort_by_match=sort_by_match
        )

    def _search_competitor_via_rag(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[PriceQuote]:
        try:
            from src.config import RAG_ENABLED

            if not RAG_ENABLED:
                return []

            from src.services.competitor_catalog_service import (
                bootstrap_competitor_catalogs,
                search_competitor_catalog_rag,
            )
            from src.services.competitor_product_store import get_competitor_product_store
            from src.services.app_state import get_processor
            from src.services.document_rag_index import get_document_rag_index
            from src.services.tz_rag_service import TZRagService

            store = get_competitor_product_store()
            rag_index = get_document_rag_index(TZRagService(get_processor().ai))

            if not store.iter_products():
                bootstrap_competitor_catalogs(rag_index, max_new_sites=2)
                quotes = search_competitor_catalog_rag(
                    query,
                    rag_index,
                    limit=limit,
                )
                if quotes:
                    return quotes
                bootstrap_competitor_catalogs(rag_index, max_new_sites=4)

            return search_competitor_catalog_rag(
                query,
                rag_index,
                limit=limit,
            )
        except Exception:
            logger.exception("RAG competitor catalog search failed for %r", query)
            return []

    @staticmethod
    def _finalize_competitor_quotes(
        quotes: list[PriceQuote],
        limit: int,
        *,
        sort_by_match: bool = False,
    ) -> list[PriceQuote]:
        from src.services.competitor_catalog_service import (
            _diversify_catalog_quotes,
            diversify_competitor_quotes_by_domain,
        )

        valid = [
            quote
            for quote in quotes
            if not quote.url or is_competitor_product_page_url(quote.url)
        ]
        if sort_by_match:
            return _diversify_catalog_quotes(valid, limit=limit)
        return diversify_competitor_quotes_by_domain(valid, limit=limit)

    def _search_competitor_via_ddg(
        self,
        query: str,
        *,
        threshold: int,
        limit: int,
        deadline: _SearchDeadline | None = None,
    ) -> list[PriceQuote]:
        if deadline and deadline.expired():
            return []

        batches = list(iter_competitor_domain_batches(COMPETITOR_SEARCH_BATCH_SIZE))
        if not batches:
            return []

        quotes: list[PriceQuote] = []
        seen_urls: set[str] = set()
        workers = min(COMPETITOR_SEARCH_PARALLEL_WORKERS, len(batches))

        def _search_batch(batch: list[str]) -> list[PriceQuote]:
            if deadline and deadline.expired():
                return []
            batch_quotes = self._collect_offers(
                query,
                threshold=threshold,
                limit=limit,
                quoted=True,
                site_domains=batch,
                stop_at_first_priced=False,
                deadline=deadline,
            )
            if not batch_quotes:
                batch_quotes = self._collect_offers(
                    query,
                    threshold=threshold,
                    limit=limit,
                    quoted=False,
                    site_domains=batch,
                    stop_at_first_priced=False,
                    deadline=deadline,
                )
            return batch_quotes

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_search_batch, batch) for batch in batches]
            wait_timeout = deadline.remaining() if deadline else None
            try:
                completed = as_completed(futures, timeout=wait_timeout)
                for future in completed:
                    if deadline and deadline.expired():
                        break
                    try:
                        batch_quotes = future.result(timeout=0.1)
                    except Exception:
                        logger.warning(
                            "Parallel DuckDuckGo competitor batch failed for %r",
                            query,
                            exc_info=True,
                        )
                        continue
                    for quote in batch_quotes:
                        if quote.url and quote.url in seen_urls:
                            continue
                        if quote.url:
                            seen_urls.add(quote.url)
                        quotes.append(quote)
                        if len(quotes) >= limit:
                            return quotes[:limit]
            except FuturesTimeoutError:
                pass
        return quotes

    def _search_competitor_site_native(
        self,
        query: str,
        site: CompetitorSite,
        *,
        threshold: int,
        max_page_fetches: int,
        deadline: _SearchDeadline | None = None,
    ) -> list[PriceQuote]:
        if deadline and deadline.expired():
            return []

        quotes: list[PriceQuote] = []
        page_fetches = 0
        http_timeout = (
            deadline.request_timeout(COMPETITOR_SEARCH_TIMEOUT)
            if deadline
            else COMPETITOR_SEARCH_TIMEOUT
        )

        search_url = build_competitor_search_url(site, query)
        if not search_url:
            return quotes

        page_fetches += 1
        search_text, _search_price = self._fetch_page_price(
            search_url,
            max_chars=WEB_SEARCH_RESULTS_PAGE_MAX_CHARS,
            timeout=http_timeout,
        )

        profile_hits = parse_competitor_search_results(
            search_text,
            site,
            limit=3,
        )
        if profile_hits:
            for hit in profile_hits:
                quote = self._quote_from_competitor_hit(
                    query,
                    hit,
                    site,
                    threshold=threshold,
                )
                if quote:
                    quotes.append(quote)
            return quotes

        product_urls = extract_competitor_product_urls(
            search_text,
            site.domain,
            limit=3,
        )
        if not product_urls:
            return quotes

        for product_url in product_urls:
            if deadline and deadline.expired():
                break
            if page_fetches >= max_page_fetches:
                break
            page_fetches += 1
            page_text, page_price = self._fetch_page_price(
                product_url,
                timeout=(
                    deadline.request_timeout(COMPETITOR_SEARCH_TIMEOUT)
                    if deadline
                    else COMPETITOR_SEARCH_TIMEOUT
                ),
            )
            quote = self._quote_from_competitor_page(
                query,
                product_url,
                site,
                threshold=threshold,
                title=self._extract_page_title(page_text) or query,
                page_text=page_text,
                prefetched_price=page_price,
            )
            if quote:
                quotes.append(quote)
        return quotes

    def _search_competitor_via_native(
        self,
        query: str,
        *,
        threshold: int,
        limit: int,
        seen_urls: set[str],
        deadline: _SearchDeadline | None = None,
    ) -> list[PriceQuote]:
        if deadline and deadline.expired():
            return []

        sites = list(competitor_sites_with_search())
        if not sites:
            return []

        quotes: list[PriceQuote] = []
        per_site_fetches = max(1, min(3, COMPETITOR_NATIVE_SEARCH_MAX_FETCHES))
        workers = min(COMPETITOR_SEARCH_PARALLEL_WORKERS, len(sites))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    self._search_competitor_site_native,
                    query,
                    site,
                    threshold=threshold,
                    max_page_fetches=per_site_fetches,
                    deadline=deadline,
                )
                for site in sites
            ]
            wait_timeout = deadline.remaining() if deadline else None
            try:
                completed = as_completed(futures, timeout=wait_timeout)
                for future in completed:
                    if deadline and deadline.expired():
                        break
                    try:
                        site_quotes = future.result(timeout=0.1)
                    except Exception:
                        logger.warning(
                            "Parallel native competitor search failed for %r",
                            query,
                            exc_info=True,
                        )
                        continue
                    for quote in site_quotes:
                        if quote.url and quote.url in seen_urls:
                            continue
                        if quote.url:
                            seen_urls.add(quote.url)
                        quotes.append(quote)
                        if has_priced_competitor_quote(quotes) and len(quotes) >= limit:
                            return quotes[:limit]
            except FuturesTimeoutError:
                pass
        return quotes

    def _quote_from_competitor_hit(
        self,
        query: str,
        hit: CompetitorSearchHit,
        site: CompetitorSite,
        *,
        threshold: int,
    ) -> PriceQuote | None:
        if _is_blocked_url(hit.url) or _is_search_listing_url(hit.url):
            return None
        page_text = ""
        prefetched_price = hit.price
        prefetched_wholesale_price = hit.wholesale_price
        prefetched_price_label = hit.price_label
        if (
            site.domain.lower().removeprefix("www.") == "stronikum.ru"
            and hit.url
        ):
            from src.services.competitor_catalog_service import fetch_catalog_page

            modal_products = fetch_catalog_page(
                f"{hit.url.rstrip('/')}.modal",
                domain=site.domain,
                site_label=site.label,
            )
            if modal_products:
                modal = modal_products[0]
                if modal.price is not None:
                    prefetched_price = modal.price
                if modal.wholesale_price is not None:
                    prefetched_wholesale_price = modal.wholesale_price
                elif hit.wholesale_price is not None:
                    prefetched_wholesale_price = hit.wholesale_price
        elif (
            site.domain.lower().removeprefix("www.") == "labkabinet.ru"
            and hit.url
        ):
            from src.services.competitor_catalog_service import fetch_catalog_page

            catalog_products = fetch_catalog_page(
                hit.url,
                domain=site.domain,
                site_label=site.label,
            )
            if catalog_products:
                catalog = catalog_products[0]
                if catalog.price is not None:
                    prefetched_price = catalog.price
                if catalog.articul:
                    page_text = f'class="block_articule"><span>Артикул: {catalog.articul}</span>'
        elif hit.url and prefetched_price is None and prefetched_price_label is None:
            page_text, prefetched_price = self._fetch_page_price(
                hit.url,
                max_chars=120_000,
            )
        return self._quote_from_competitor_page(
            query,
            hit.url,
            site,
            threshold=threshold,
            title=hit.name or query,
            page_text=page_text,
            prefetched_price=prefetched_price,
            prefetched_wholesale_price=prefetched_wholesale_price,
            prefetched_price_label=prefetched_price_label,
            notes=(
                "Конкурент | цена в выдаче поиска"
                if hit.price is not None
                else (
                    "Конкурент | цена со страницы товара"
                    if prefetched_price is not None and page_text
                    else (
                        f"Конкурент | {hit.price_label}"
                        if hit.price_label
                        else "Конкурент | совпадение в выдаче поиска, цена не указана"
                    )
                )
            ),
        )

    def _quote_from_competitor_page(
        self,
        query: str,
        url: str,
        site: CompetitorSite,
        *,
        threshold: int,
        title: str,
        page_text: str,
        prefetched_price: float | None,
        prefetched_price_label: str | None = None,
        prefetched_wholesale_price: float | None = None,
        notes: str | None = None,
    ) -> PriceQuote | None:
        if _is_blocked_url(url) or _is_search_listing_url(url):
            return None
        if not is_competitor_product_page_url(url):
            return None
        if not is_exact_title_match(query, title, threshold=threshold, snippet=page_text[:500]):
            return None

        normalized_query = normalize_name(query)
        cleaned_title = _clean_title_for_match(title)
        if (
            normalized_query in normalize_name(_strip_html(title))
            or _token_set(normalized_query) == _token_set(cleaned_title)
        ):
            match_score = 100.0
        else:
            match_score = float(name_match_score(normalized_query, cleaned_title))

        if match_score < threshold:
            return None

        price = prefetched_price
        price_label = prefetched_price_label
        note = notes or "Поиск на сайте конкурента"
        if price is None and page_text:
            page_prices = extract_prices_from_text(page_text[:120_000])
            price = _pick_best_price(page_prices)
            if price is not None:
                note = "Конкурент | цена со страницы товара"
        if price is None and price_label is None and page_text:
            price_label = price_on_request_label(page_text[:120_000])
        if price is None:
            if price_label:
                note = notes or f"Конкурент | {price_label}"
            else:
                note = notes or "Конкурент | совпадение названия, цена не указана"

        articul = _extract_articul_from_page(page_text)
        domain = urlparse(url).netloc.lower().removeprefix("www.") if url else ""
        image_url: str | None = None
        if domain == "vrtorg.ru" and page_text:
            from src.services.competitor_catalog_service import _extract_vrtorg_product_image

            image_url = _extract_vrtorg_product_image(
                page_text,
                domain=site.domain,
                page_url=url,
            )
        needs_catalog_fetch = (
            url
            and (
                not articul
                or (domain == "labkabinet.ru" and price is None)
            )
        )
        if needs_catalog_fetch:
            from src.services.competitor_catalog_service import fetch_catalog_page

            fetched = fetch_catalog_page(url, domain=site.domain, site_label=site.label)
            if fetched:
                if fetched[0].articul:
                    articul = fetched[0].articul
                if price is None and fetched[0].price is not None:
                    price = fetched[0].price
                    note = "Конкурент | цена со страницы товара"
                if fetched[0].image_url:
                    image_url = fetched[0].image_url

        competitor_label = site.label or competitor_label_for_url(url) or site.domain
        return PriceQuote(
            source="web",
            label=f"Конкурент: {competitor_label}",
            matched_name=_strip_html(title) or query,
            price=price,
            cost=price,
            price_label=price_label,
            wholesale_price=prefetched_wholesale_price,
            articul=articul,
            match_score=match_score,
            url=url,
            notes=note,
            image_url=image_url,
        )

    def search_internet_cascade(
        self,
        product_name: str,
        *,
        limit: int | None = None,
        skip_competitors: bool = False,
        deadline: _SearchDeadline | None = None,
        competitor_fallback: bool = True,
    ) -> list[PriceQuote]:
        """Сначала сайты конкурентов, затем прочий интернет, затем маркетплейсы."""
        query = product_name.strip()
        if not query:
            return []

        if deadline is None and INTERNET_SEARCH_BUDGET_SECONDS > 0:
            deadline = _SearchDeadline.from_budget(INTERNET_SEARCH_BUDGET_SECONDS)
        if deadline and deadline.expired():
            return []

        max_results = limit or WEB_SEARCH_MAX_RESULTS
        quotes: list[PriceQuote] = []
        seen_urls: set[str] = set()

        def _extend(new_quotes: list[PriceQuote]) -> None:
            for quote in new_quotes:
                if quote.url and any(existing.url == quote.url for existing in quotes):
                    continue
                if quote.url:
                    seen_urls.add(quote.url)
                quotes.append(quote)

        if not skip_competitors and not (deadline and deadline.expired()):
            competitor_quotes = self.search_competitor_offers(
                query,
                limit=max_results,
                deadline=deadline,
                allow_fallback=competitor_fallback,
            )
            _extend(competitor_quotes)
            if has_priced_competitor_quote(quotes):
                return quotes

        if deadline and deadline.expired():
            return quotes

        _extend(
            self.search_offers(
                query,
                limit=max_results,
                deadline=deadline,
            )
        )
        if pick_best_web_priced_quote(quotes):
            return quotes

        if deadline and deadline.expired():
            return quotes

        _extend(
            self.search_marketplace_offers(
                query,
                limit=1,
                deadline=deadline,
            )
        )
        return quotes

    def search_web_price_fallback(
        self,
        product_name: str,
        *,
        limit: int | None = None,
        deadline: _SearchDeadline | None = None,
    ) -> list[PriceQuote]:
        """Интернет и маркетплейсы, если у конкурента совпадение без цены."""
        if deadline is None and INTERNET_SEARCH_BUDGET_SECONDS > 0:
            deadline = _SearchDeadline.from_budget(INTERNET_SEARCH_BUDGET_SECONDS)
        if deadline and deadline.expired():
            return []
        query = product_name.strip()
        if not query:
            return []

        max_results = limit or WEB_SEARCH_MAX_RESULTS
        quotes: list[PriceQuote] = []
        seen_urls: set[str] = set()

        def _extend(new_quotes: list[PriceQuote]) -> None:
            for quote in new_quotes:
                if quote.url and any(existing.url == quote.url for existing in quotes):
                    continue
                if quote.url:
                    seen_urls.add(quote.url)
                quotes.append(quote)

        _extend(
            self.search_offers(
                query,
                limit=max_results,
                deadline=deadline,
            )
        )
        if pick_best_web_priced_quote(quotes):
            return quotes

        if deadline and deadline.expired():
            return quotes

        _extend(
            self.search_marketplace_offers(
                query,
                limit=1,
                deadline=deadline,
            )
        )
        return quotes

    def search_marketplace_offers(
        self,
        product_name: str,
        *,
        limit: int | None = None,
        deadline: _SearchDeadline | None = None,
    ) -> list[PriceQuote]:
        if deadline and deadline.expired():
            return []
        query = product_name.strip()
        if not query:
            return []

        max_results = limit or WEB_SEARCH_MAX_RESULTS
        normalized_query = normalize_name(query)
        quotes: list[PriceQuote] = []
        seen_urls: set[str] = set()

        for platform, template in _MARKETPLACE_SEARCH:
            if deadline and deadline.expired():
                break
            if len(quotes) >= max_results:
                break
            search_url = template.format(query=quote_plus(query))
            fetch_timeout = (
                deadline.request_timeout(WEB_SEARCH_TIMEOUT)
                if deadline
                else WEB_SEARCH_TIMEOUT
            )
            search_text, _search_price = self._fetch_page_price(
                search_url,
                timeout=fetch_timeout,
            )
            product_url = self._extract_product_url(search_text, platform)
            if not product_url:
                continue

            page_text, page_price = self._fetch_page_price(
                product_url,
                timeout=fetch_timeout,
            )
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
        site_domains: list[str] | None = None,
        stop_at_first_priced: bool = True,
        deadline: _SearchDeadline | None = None,
    ) -> list[PriceQuote]:
        if deadline and deadline.expired():
            return []
        serp_hits = self._search_duckduckgo(
            query,
            quoted=quoted,
            site_domains=site_domains,
            deadline=deadline,
        )
        if not serp_hits and site_domains and quoted and not (deadline and deadline.expired()):
            serp_hits = self._search_duckduckgo(
                query,
                quoted=False,
                site_domains=site_domains,
                deadline=deadline,
            )
        quotes: list[PriceQuote] = []
        seen_urls: set[str] = set()
        page_fetches = 0
        allowed_hosts = {domain.lower() for domain in site_domains} if site_domains else None

        for hit in serp_hits:
            if deadline and deadline.expired():
                break
            if len(quotes) >= limit:
                break
            title = hit.get("title") or ""
            url = hit.get("url") or ""
            snippet = hit.get("snippet") or ""
            if not url or url in seen_urls or _is_blocked_url(url):
                continue
            if allowed_hosts:
                host = _host_from_url(url)
                if not any(
                    host == domain or host.endswith(f".{domain}")
                    for domain in allowed_hosts
                ):
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
            competitor_hit = bool(allowed_hosts) or is_competitor_url(url)
            if competitor_hit:
                price = None
                notes = "Конкурент | совпадение названия, цена не указана"
            else:
                prices = extract_prices_from_text(snippet)
                price = _pick_best_price(prices)
                notes = "Совпадение названия в поисковой выдаче"
            if (
                price is None
                and WEB_SEARCH_FETCH_PAGES
                and page_fetches < WEB_SEARCH_MAX_PAGE_FETCHES
                and not (deadline and deadline.expired())
            ):
                page_fetches += 1
                fetch_timeout = (
                    deadline.request_timeout(
                        COMPETITOR_SEARCH_TIMEOUT if competitor_hit else WEB_SEARCH_TIMEOUT
                    )
                    if deadline
                    else (COMPETITOR_SEARCH_TIMEOUT if competitor_hit else WEB_SEARCH_TIMEOUT)
                )
                page_text, page_price = self._fetch_page_price(
                    url,
                    timeout=fetch_timeout,
                )
                if page_price is not None:
                    price = page_price
                    notes = (
                        "Конкурент | цена со страницы товара"
                        if competitor_hit
                        else "Совпадение названия, цена со страницы"
                    )
                elif page_text:
                    page_prices = extract_prices_from_text(page_text[:120_000])
                    price = _pick_best_price(page_prices)
                    if price is not None:
                        notes = (
                            "Конкурент | цена со страницы товара"
                            if competitor_hit
                            else "Совпадение названия, цена со страницы"
                        )
                    elif competitor_hit:
                        notes = "Конкурент | совпадение названия, цена не указана"

            if match_score < threshold:
                continue

            if require_price and price is None:
                continue

            if is_competitor_url(url) or allowed_hosts:
                competitor_label = competitor_label_for_url(url) or _platform_label(url)
                label = f"Конкурент: {competitor_label}"
                if allowed_hosts and not notes.startswith("Конкурент"):
                    notes = (
                        f"Конкурент | {notes}"
                        if price is not None
                        else "Конкурент | совпадение названия, цена не указана"
                    )
                elif price is None:
                    notes = "Конкурент | совпадение названия, цена не указана"
            elif is_marketplace_url(url):
                if not is_product_page_url(url):
                    continue
                platform = _platform_label(url)
                label = f"Маркетплейс: {platform}"
            else:
                platform = _platform_label(url)
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
            if (
                price is not None
                and stop_at_first_priced
                and not is_marketplace_url(url)
            ):
                break

        return quotes

    def _search_duckduckgo(
        self,
        query: str,
        *,
        quoted: bool = True,
        site_domains: list[str] | None = None,
        deadline: _SearchDeadline | None = None,
    ) -> list[dict[str, str]]:
        if deadline and deadline.expired():
            return []
        if site_domains:
            site_clause = " OR ".join(f"site:{domain}" for domain in site_domains)
            search_query = (
                f"({site_clause}) \"{query}\"" if quoted else f"({site_clause}) {query}"
            )
        else:
            search_query = f'"{query}"' if quoted else query
        hits = self._execute_duckduckgo_search(search_query, deadline=deadline)
        if hits or quoted or site_domains:
            return hits
        if deadline and deadline.expired():
            return []
        return self._search_duckduckgo(query, quoted=True, site_domains=site_domains, deadline=deadline)

    def _execute_duckduckgo_search(
        self,
        search_query: str,
        *,
        deadline: _SearchDeadline | None = None,
    ) -> list[dict[str, str]]:
        if deadline and deadline.expired():
            return []
        url = "https://html.duckduckgo.com/html/"
        headers = {"User-Agent": _USER_AGENT}
        data = {"q": search_query}
        timeout = (
            deadline.request_timeout(WEB_SEARCH_TIMEOUT)
            if deadline
            else WEB_SEARCH_TIMEOUT
        )

        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=timeout,
                headers=headers,
            ) as client:
                response = client.post(url, data=data)
                if response.status_code == 202:
                    return []
                response.raise_for_status()
                html = response.text
        except Exception:
            logger.warning(
                "DuckDuckGo search failed for %r", search_query, exc_info=True
            )
            return []

        return self._parse_duckduckgo_html(html)

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

    def _fetch_page_price(
        self,
        url: str,
        *,
        max_chars: int | None = None,
        timeout: float | None = None,
    ) -> tuple[str, float | None]:
        headers = {"User-Agent": _USER_AGENT}
        limit = max_chars if max_chars is not None else WEB_SEARCH_PAGE_MAX_CHARS
        request_timeout = timeout if timeout is not None else WEB_SEARCH_TIMEOUT
        if request_timeout <= 0:
            return "", None
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=request_timeout,
                headers=headers,
            ) as client:
                response = client.get(url)
                if response.status_code >= 400:
                    return "", None
                text = response.text[:limit]
        except Exception:
            logger.debug("Page fetch failed for %s", url, exc_info=True)
            return "", None

        prices = extract_prices_from_text(text)
        related_idx = text.find("Покупают вместе")
        if related_idx > 0:
            main_prices = extract_prices_from_text(text[:related_idx])
            if main_prices:
                prices = main_prices
        return text, _pick_best_price(prices)
