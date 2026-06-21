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


@dataclass(frozen=True)
class CompetitorSearchHit:
    url: str
    name: str
    price: float | None = None
    price_label: str | None = None
    wholesale_price: float | None = None


@dataclass(frozen=True)
class CompetitorSearchProfile:
    """Точные правила парсинга выдачи поиска на сайте конкурента."""

    search_url: str | None = None
    result_item_pattern: str | None = None
    # Маркеры блока выдачи — для сужения HTML до результатов поиска
    result_section_markers: tuple[str, ...] = ()


COMPETITOR_SITES: tuple[CompetitorSite, ...] = (
    CompetitorSite(
        "xn----7sbbumkojddmeoc1a7r.xn--p1acf",
        "Punktum",
        "https://xn----7sbbumkojddmeoc1a7r.xn--p1acf/search/search_do/?search_string={query}",
    ),
    CompetitorSite("n-72.ru", "Новация", "https://n-72.ru/catalog/?q={query}&s="),
    CompetitorSite(
        "stronikum.ru",
        "Строникум",
        "https://stronikum.ru/search?q={query}",
    ),
    CompetitorSite("labkabinet.ru", "Labkabinet", "https://labkabinet.ru/search/?q={query}"),
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
        "https://skale.ru/magazin/search?search={query}",
    ),
)

_DOMAIN_TO_LABEL: dict[str, str] = {
    site.domain.lower(): site.label for site in COMPETITOR_SITES
}

_SITES_WITH_SEARCH: tuple[CompetitorSite, ...] = tuple(
    site for site in COMPETITOR_SITES if site.search_url
)

# Профили точного парсинга (дополняют универсальный разбор href, не заменяют его).
_COMPETITOR_SEARCH_PROFILES: dict[str, CompetitorSearchProfile] = {
    "skale.ru": CompetitorSearchProfile(
        search_url="https://skale.ru/magazin/search?search={query}",
        result_item_pattern=(
            r'class="product-name"><a\s+href="(?P<url>[^"]+)">(?P<name>[^<]+)</a>.*?'
            r'class="price-current"><strong[^>]*>(?P<price>[^<]+)</strong>'
        ),
        result_section_markers=("product-name", "shop2-product", "Найдено"),
    ),
    "xn----7sbbumkojddmeoc1a7r.xn--p1acf": CompetitorSearchProfile(
        search_url=(
            "https://xn----7sbbumkojddmeoc1a7r.xn--p1acf/search/search_do/"
            "?search_string={query}"
        ),
        result_item_pattern=(
            r'itemprop="name"\s+href="(?P<url>[^"]+)"[^>]*>\s*(?P<name>[^<]+?)\s*</a>'
        ),
        result_section_markers=("preview_product", "найдено товаров", "itemListElement"),
    ),
    "n-72.ru": CompetitorSearchProfile(
        search_url="https://n-72.ru/catalog/?q={query}&s=",
        result_item_pattern=(
            r'href="(?P<url>/catalog/product/[^"]+)".*?'
            r'class="n72r-product-preview__title">(?P<name>[^<]+)</a>.*?'
            r'class="price_value">(?P<price>[^<]+)</span>'
        ),
        result_section_markers=("n72r-product-preview", "catalog/product", "list_item"),
    ),
    "labkabinet.ru": CompetitorSearchProfile(
        search_url="https://labkabinet.ru/search/?q={query}",
        result_item_pattern=(
            r'href="(?P<url>/product/[^"]+)".*?'
            r'class="item-title"[^>]*>\s*(?P<name>[^<]+?)\s*</.*?'
            r'class="price_value">(?P<price>[^<]+)</span>'
        ),
        result_section_markers=("catalog_block", "item-title", "price_value", "search-page"),
    ),
    "stronikum.ru": CompetitorSearchProfile(
        search_url="https://stronikum.ru/search?q={query}",
        result_item_pattern=(
            r'href="(?P<url>/\d+_[^/]+/\d+_[^"]+)">(?P<name>[^<]+)</a>.*?'
            r'<td[^>]*>(?P<price>\d[\d\s]*)</td>'
        ),
        result_section_markers=("price-products", "Поиск:", "table-striped"),
    ),
}

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
    "/assets/",
    "/static/",
    "/frontend/",
    "/dist/",
    "/icons/",
    "/images/icons/",
    "/favicon",
    "/templates/",
    "/local/",
    "/fonts/",
    "/include/",
    "/wp-content/",
    "/wp-includes/",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".otf",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".map",
    ".xml",
    ".zip",
    ".pdf",
    ".css",
    ".js",
)

_PRODUCT_PATH_MARKERS = (
    "/catalog/",
    "/products/",
    "/product/",
    "/magazin/product/",
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


def _merged_competitor_sites() -> tuple[CompetitorSite, ...]:
    from src.services.competitor_site_manager import get_competitor_site_manager

    builtin_domains = {site.domain.lower() for site in COMPETITOR_SITES}
    custom = [
        site
        for site in get_competitor_site_manager().as_competitor_sites()
        if site.domain.lower() not in builtin_domains
    ]
    return COMPETITOR_SITES + tuple(custom)


def _merged_domain_labels() -> dict[str, str]:
    labels = dict(_DOMAIN_TO_LABEL)
    for site in _merged_competitor_sites():
        labels.setdefault(site.domain.lower(), site.label)
    return labels


def all_competitor_domains(*, include_custom: bool = True) -> list[str]:
    sites = _merged_competitor_sites() if include_custom else COMPETITOR_SITES
    return [site.domain for site in sites]


def competitor_sites_with_search() -> tuple[CompetitorSite, ...]:
    return tuple(site for site in _merged_competitor_sites() if site.search_url)


def get_builtin_competitor_site(domain: str) -> CompetitorSite | None:
    normalized = domain.lower().removeprefix("www.")
    for site in COMPETITOR_SITES:
        if site.domain.lower().removeprefix("www.") == normalized:
            return site
    return None


def is_builtin_competitor_domain(domain: str) -> bool:
    return get_builtin_competitor_site(domain) is not None


def competitor_search_profile(domain: str) -> CompetitorSearchProfile | None:
    key = domain.lower().removeprefix("www.")
    return _COMPETITOR_SEARCH_PROFILES.get(key)


def resolve_competitor_absolute_url(domain: str, href: str) -> str:
    """Собрать абсолютный URL; для путей с / игнорируем <base href> страницы."""
    href = unescape_href(href)
    if not href:
        return ""
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("http"):
        return href
    root = f"https://{domain.removeprefix('www.')}"
    if href.startswith("/"):
        return f"{root}{href}"
    return urljoin(f"{root}/", href)


def focus_competitor_search_html(page_text: str, domain: str) -> str:
    """Оставить фрагмент HTML с выдачей поиска, если он известен для домена."""
    if not page_text:
        return page_text

    profile = competitor_search_profile(domain)
    markers = profile.result_section_markers if profile else ()
    if not markers:
        return page_text

    start = -1
    for marker in markers:
        index = page_text.find(marker)
        if index >= 0 and (start < 0 or index < start):
            start = index

    if start < 0:
        return page_text

    window_start = max(0, start - 2_000)
    return page_text[window_start : window_start + 250_000]


def build_competitor_search_url(site: CompetitorSite, query: str) -> str:
    from urllib.parse import quote_plus

    profile = competitor_search_profile(site.domain)
    template = (
        profile.search_url
        if profile and profile.search_url
        else site.search_url
    )
    if not template:
        return ""
    return template.format(query=quote_plus(query))


def parse_competitor_search_results(
    page_text: str,
    site: CompetitorSite,
    *,
    limit: int = 5,
) -> list[CompetitorSearchHit]:
    if page_text and (
        'class="product-top"' in page_text
        or "preview_product" in page_text.lower()
        or "n72r-product-preview" in page_text
        or 'class="product-row"' in page_text
        or "price-products" in page_text
    ):
        from src.services.competitor_catalog_service import parse_catalog_html

        products = parse_catalog_html(
            page_text,
            domain=site.domain,
            site_label=site.label,
            page_url=build_competitor_search_url(site, "search"),
        )
        hits: list[CompetitorSearchHit] = []
        seen: set[str] = set()
        for product in products:
            if not product.url:
                continue
            if not is_competitor_product_page_url(product.url):
                continue
            if product.url in seen:
                continue
            seen.add(product.url)
            hits.append(
                CompetitorSearchHit(
                    url=product.url,
                    name=product.name,
                    price=product.price,
                    price_label=product.price_label,
                    wholesale_price=product.wholesale_price,
                )
            )
            if len(hits) >= limit:
                break
        if hits:
            return hits

    profile = competitor_search_profile(site.domain)
    if not profile or not profile.result_item_pattern or not page_text:
        return []

    pattern = re.compile(profile.result_item_pattern, re.I | re.S)
    hits: list[CompetitorSearchHit] = []
    seen: set[str] = set()

    focused = focus_competitor_search_html(page_text, site.domain)

    for match in pattern.finditer(focused):
        url = match.group("url")
        absolute = resolve_competitor_absolute_url(site.domain, url)
        if not absolute:
            continue
        if not is_competitor_product_page_url(absolute):
            continue

        if absolute in seen:
            continue
        seen.add(absolute)

        name = re.sub(r"\s+", " ", match.group("name")).strip()
        price_raw = match.groupdict().get("price")
        price = None
        if price_raw:
            from src.services.web_search_service import extract_prices_from_text

            prices = extract_prices_from_text(str(price_raw))
            price = prices[0] if prices else None
        chunk = focused[match.start() : match.start() + 2500]
        from src.services.web_search_service import price_on_request_label

        label = price_on_request_label(chunk) if price is None else None
        is_stronikum = site.domain.lower().removeprefix("www.") == "stronikum.ru"
        hits.append(
            CompetitorSearchHit(
                url=absolute,
                name=name,
                price=None if is_stronikum else price,
                wholesale_price=price if is_stronikum else None,
                price_label=label,
            )
        )
        if len(hits) >= limit:
            break

    return hits


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
    return any(host_matches_competitor(host, domain) for domain in _merged_domain_labels())


def competitor_label_for_url(url: str | None) -> str | None:
    if not url:
        return None
    host = _normalize_host(url)
    for domain, label in _merged_domain_labels().items():
        if host_matches_competitor(host, domain):
            return label
    return None


def competitor_site_for_url(url: str | None) -> CompetitorSite | None:
    if not url:
        return None
    host = _normalize_host(url)
    for site in _merged_competitor_sites():
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


def is_competitor_asset_url(url: str) -> bool:
    if not url:
        return True
    lower = url.lower()
    path = urlparse(lower).path
    if any(marker in lower for marker in _SKIP_PATH_MARKERS):
        return True
    return bool(
        re.search(
            r"\.(?:svg|ico|webp|gif|woff2?|ttf|eot|otf|map|xml|zip|pdf|css|js)(?:\?|$)",
            path,
            re.I,
        )
    )


def is_competitor_product_page_url(url: str) -> bool:
    if not url or is_competitor_asset_url(url):
        return False

    path = urlparse(url).path.lower().rstrip("/")
    if not path:
        return False

    if any(
        marker in path
        for marker in (
            "/catalog/product/",
            "/magazin/product/",
            "/product/",
            "/tovar/",
            "/goods/",
            "/item/",
            "/card/",
        )
    ):
        return True

    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return False
    last = segments[-1]

    if last.isdigit() and len(last) >= 4:
        return True

    if path.endswith(".html") and len(last) >= 10:
        return True

    slug_like = len(last) >= 12 and ("_" in last or "-" in last)
    if slug_like and "/products/" in path and len(segments) >= 4:
        return True
    if slug_like and "/catalog/" in path and len(segments) >= 3:
        return True

    # stronikum.ru: /1061_Fizika/15145_Komplekt_...
    if (
        len(segments) == 2
        and re.match(r"\d+_", segments[0])
        and re.match(r"\d+_", segments[1])
    ):
        return True

    return False


def _looks_like_product_path(path: str) -> bool:
    return is_competitor_product_page_url(f"https://example.com{path}")


def extract_competitor_product_urls(
    page_text: str,
    domain: str,
    *,
    limit: int = 5,
) -> list[str]:
    if not page_text:
        return []

    seen: set[str] = set()
    urls: list[str] = []

    focused = focus_competitor_search_html(page_text, domain)

    for href in _HREF_RE.findall(focused):
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        absolute = resolve_competitor_absolute_url(domain, href)
        if not absolute:
            continue

        parsed = urlparse(absolute)
        if not host_matches_competitor(parsed.netloc, domain):
            continue
        if is_competitor_asset_url(absolute):
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
