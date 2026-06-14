"""Сайты конкурентов для приоритетного поиска цен перед маркетплейсами."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from src.config import COMPETITOR_SEARCH_FALLBACK_THRESHOLD, WEB_SEARCH_EXACT_THRESHOLD


@dataclass(frozen=True)
class CompetitorSite:
    domain: str
    label: str
    search_url: str | None = None


COMPETITOR_SITES: tuple[CompetitorSite, ...] = (
    CompetitorSite(
        "xn----7sbbumkojddmeoc1a7r.xn--p1acf",
        "Punktum",
        "https://xn----7sbbumkojddmeoc1a7r.xn--p1acf/search/?q={query}",
    ),
    CompetitorSite("n-72.ru", "Новация", "https://n-72.ru/search/?q={query}"),
    CompetitorSite(
        "stronikum.ru",
        "Строникум",
        "https://stronikum.ru/search/?search={query}",
    ),
    CompetitorSite(
        "labkabinet.ru",
        "Labkabinet",
        "https://labkabinet.ru/search?q={query}",
    ),
    CompetitorSite(
        "vrtorg.ru",
        "ВнешРегионТорг",
        "https://vrtorg.ru/search?query={query}",
    ),
    CompetitorSite(
        "td-school.ru",
        "Школьный мир",
        "https://td-school.ru/index.php?search={query}",
    ),
    CompetitorSite("epp24.ru", "EPP24", "https://epp24.ru/search?query={query}"),
    CompetitorSite(
        "zarnitza.ru",
        "Зарница",
        "https://zarnitza.ru/search/?q={query}",
    ),
    CompetitorSite(
        "rostcom.com",
        "Rostcom",
        "https://www.rostcom.com/search/?q={query}",
    ),
    CompetitorSite(
        "rene-edu.ru",
        "Рене",
        "https://www.rene-edu.ru/search.html?search={query}",
    ),
    CompetitorSite(
        "prioritet1.com",
        "Приоритет",
        "https://prioritet1.com/search?search={query}",
    ),
    CompetitorSite("orionedu.ru", "Орион", "https://orionedu.ru/?s={query}"),
    CompetitorSite(
        "xn--54-vlc3b6bza.xn--p1ai",
        "Школьный мир",
        "https://xn--54-vlc3b6bza.xn--p1ai/index.php?search={query}",
    ),
    CompetitorSite(
        "skale.ru",
        "Скале",
        "https://skale.ru/prays-list?search={query}",
    ),
)

_DOMAIN_TO_LABEL: dict[str, str] = {
    site.domain.lower(): site.label for site in COMPETITOR_SITES
}

_SITES_WITH_SEARCH: tuple[CompetitorSite, ...] = tuple(
    site for site in COMPETITOR_SITES if site.search_url
)

_SKIP_PATH_MARKERS = (
    "/search",
    "/login",
    "/cart",
    "/basket",
    "/compare",
    "/wishlist",
    "/account",
    "/auth",
    "/register",
    "/policy",
    "/privacy",
    "/contact",
    "/news",
    "/blog",
    "/upload",
    "/media/",
    ".jpg",
    ".png",
    ".pdf",
    ".css",
    ".js",
)

_PRODUCT_PATH_MARKERS = (
    "/catalog/",
    "/product/",
    "/tovar",
    "/goods/",
    "/item/",
    "/shop/",
    "/prays",
    "/price",
    "/katalog/",
    "/card/",
)

_HREF_RE = re.compile(r'href="([^"]+)"', re.I)


def all_competitor_domains() -> list[str]:
    return [site.domain for site in COMPETITOR_SITES]


def competitor_sites_with_search() -> tuple[CompetitorSite, ...]:
    return _SITES_WITH_SEARCH


def _normalize_host(url_or_host: str) -> str:
    text = (url_or_host or "").strip().lower()
    if "://" in text:
        text = urlparse(text).netloc.lower()
    return text.removeprefix("www.")


def host_matches_competitor(host: str, domain: str) -> bool:
    normalized_host = _normalize_host(host)
    normalized_domain = domain.lower().removeprefix("www.")
    return normalized_host == normalized_domain or normalized_host.endswith(
        f".{normalized_domain}"
    )


def is_competitor_url(url: str | None) -> bool:
    if not url:
        return False
    host = _normalize_host(url)
    if not host:
        return False
    return any(host_matches_competitor(host, domain) for domain in _DOMAIN_TO_LABEL)


def competitor_label_for_url(url: str | None) -> str | None:
    if not url:
        return None
    host = _normalize_host(url)
    for domain, label in _DOMAIN_TO_LABEL.items():
        if host_matches_competitor(host, domain):
            return label
    return None


def competitor_site_for_url(url: str | None) -> CompetitorSite | None:
    if not url:
        return None
    host = _normalize_host(url)
    for site in COMPETITOR_SITES:
        if host_matches_competitor(host, site.domain):
            return site
    return None


def meets_competitor_match_threshold(score: float, *, strict: bool = True) -> bool:
    threshold = (
        WEB_SEARCH_EXACT_THRESHOLD if strict else COMPETITOR_SEARCH_FALLBACK_THRESHOLD
    )
    return float(score or 0) >= threshold


def meets_web_display_threshold(quote_url: str | None, score: float) -> bool:
    if is_competitor_url(quote_url):
        return meets_competitor_match_threshold(score, strict=False)
    return float(score or 0) >= WEB_SEARCH_EXACT_THRESHOLD


def iter_competitor_domain_batches(batch_size: int) -> list[list[str]]:
    domains = all_competitor_domains()
    size = max(1, batch_size)
    return [domains[index : index + size] for index in range(0, len(domains), size)]


def _looks_like_product_path(path: str) -> bool:
    lower = path.lower()
    if any(marker in lower for marker in _SKIP_PATH_MARKERS):
        return False
    if any(marker in lower for marker in _PRODUCT_PATH_MARKERS):
        return True
    if re.search(r"/\d{3,}(?:/|$|\?|#)", lower):
        return True
    if re.search(r"/[a-z0-9-]{8,}(?:/|$|\?|#)", lower):
        return True
    return False


def extract_competitor_product_urls(
    page_text: str,
    domain: str,
    *,
    limit: int = 5,
) -> list[str]:
    if not page_text:
        return []

    base = f"https://{domain.removeprefix('www.')}"
    seen: set[str] = set()
    urls: list[str] = []

    for href in _HREF_RE.findall(page_text):
        href = unescape_href(href)
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        if href.startswith("//"):
            absolute = f"https:{href}"
        elif href.startswith("/"):
            absolute = urljoin(base, href)
        elif href.startswith("http"):
            absolute = href
        else:
            absolute = urljoin(base, href)

        parsed = urlparse(absolute)
        if not host_matches_competitor(parsed.netloc, domain):
            continue
        if not _looks_like_product_path(parsed.path):
            continue

        clean = absolute.split("#")[0].split("&amp;")[0]
        if clean in seen:
            continue
        seen.add(clean)
        urls.append(clean)
        if len(urls) >= limit:
            break

    return urls


def unescape_href(href: str) -> str:
    return href.replace("&amp;", "&").strip()
