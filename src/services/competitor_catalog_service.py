from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

from src.config import COMPETITOR_SEARCH_FALLBACK_THRESHOLD, WEB_SEARCH_TIMEOUT
from src.services.competitor_sites import (
    CompetitorSite,
    competitor_label_for_url,
    competitor_sites_with_search,
    is_competitor_product_page_url,
)
from src.services.data_loader import normalize_name
from src.services.fuzzy_scoring import name_match_score
from src.services.models import PriceQuote
from src.services.web_search_service import (
    PRICE_ON_REQUEST_LABEL,
    extract_prices_from_text,
    price_on_request_label,
)

logger = logging.getLogger(__name__)

_CATALOG_SEED_URLS: dict[str, list[str]] = {
    "skale.ru": [
        "https://skale.ru/magazin",
        "https://skale.ru/magazin/folder/uchebnoe-oborudovanie-po-astronomii-i-astrofizike",
        "https://skale.ru/prays-list",
    ],
    "xn----7sbbumkojddmeoc1a7r.xn--p1acf": [
        "https://xn----7sbbumkojddmeoc1a7r.xn--p1acf/products/",
    ],
    "n-72.ru": [
        "https://n-72.ru/catalog/",
    ],
    "stronikum.ru": [
        "https://stronikum.ru/prices",
        "https://stronikum.ru/sitemap.xml",
    ],
    "labkabinet.ru": [
        "https://labkabinet.ru/catalog/",
        "https://labkabinet.ru/sitemap.xml",
    ],
    "vrtorg.ru": [
        "https://vrtorg.ru/catalog/",
        "https://vrtorg.ru/sitemap.xml",
    ],
}

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_ARTICUL_RE = re.compile(r"Артикул:\s*(?:<span>)?([^\s<]+)", re.I)
_N72_PREVIEW_RE = re.compile(
    r'href="(?P<url>/catalog/product/[^"]+)".*?'
    r'class="n72r-product-preview__title">(?P<name>[^<]+)</a>.*?'
    r'class="price_value">(?P<price>[^<]+)</span>',
    re.I | re.S,
)
_PRODUCT_LINE_RE = re.compile(
    r"^\[product\]\s*domain=(?P<domain>[^|]+)\s*\|\s*site=(?P<site>[^|]+)\s*\|"
    r"\s*name=(?P<name>[^|]+)\s*\|\s*price=(?P<price>[^|]*)\s*\|"
    r"\s*url=(?P<url>[^|]*)\s*\|\s*articul=(?P<articul>[^|]*)(?:\s*\|\s*price_label=(?P<price_label>[^|]*))?"
    r"(?:\s*\|\s*wholesale_price=(?P<wholesale_price>[^|]*))?"
    r"(?:\s*\|\s*details=(?P<details>.*))?",
    re.I,
)


@dataclass
class CompetitorCatalogProduct:
    domain: str
    site_label: str
    name: str
    price: float | None
    url: str | None
    articul: str | None = None
    price_label: str | None = None
    details: str | None = None
    wholesale_price: float | None = None


_STRONIKUM_PRODUCT_PATH_RE = re.compile(r"^/\d+_[^/]+/\d+_", re.I)
_STRONIKUM_SITEMAP_PRODUCT_RE = re.compile(
    r"<loc>(https://stronikum\.ru/\d+_[^/]+/\d+_[^<]+)</loc>",
    re.I,
)
_STRONIKUM_CATEGORY_ROW_RE = re.compile(
    r'href="(?P<url>(?:/\d+_[^/]+/)?\d+_[^"]+)"[^>]*>(?P<name>[^<]+)</a>.*?'
    r'<td[^>]*(?:align="right")?[^>]*>(?P<price>\d[\d\s]*)\s*(?:р\.|</td>)',
    re.I | re.S,
)


def _strip_html_text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()


def _canonical_stronikum_product_url(url: str) -> str:
    clean = url.split("#")[0].split("?")[0]
    if clean.endswith(".modal"):
        clean = clean[:-6]
    return clean


def _is_stronikum_product_path(path: str) -> bool:
    return bool(_STRONIKUM_PRODUCT_PATH_RE.match(path.rstrip("/")))


def _parse_stronikum_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    canonical_url = _canonical_stronikum_product_url(page_url)
    path = urlparse(canonical_url).path
    if not _is_stronikum_product_path(path):
        return None

    name_match = re.search(
        r'<h1[^>]*itemprop="name"[^>]*>(?P<name>[^<]+)',
        html,
        re.I | re.S,
    )
    if not name_match:
        name_match = re.search(r'itemprop="name">(?P<name>[^<]+)', html, re.I)
    if not name_match:
        return None

    name = re.sub(r"\s+", " ", name_match.group("name")).strip()
    if len(name) < 4:
        return None

    articul = None
    articul_match = re.search(
        r'class="product-code"[^>]*>.*?Артикул:\s*(\d+)',
        html,
        re.I | re.S,
    )
    if articul_match:
        articul = articul_match.group(1)
    else:
        body_match = re.search(r'class="product-body"[^>]*id="(\d+)"', html, re.I)
        if body_match:
            articul = body_match.group(1)

    price = None
    retail_match = re.search(r"Цена:\s*(\d[\d\s]*)\s*RUB", html, re.I)
    if retail_match:
        price = _parse_price(retail_match.group(1))

    wholesale_price = None
    wholesale_match = re.search(
        r'itemprop="offers"[^>]*>.*?itemprop="price">(\d+)',
        html,
        re.I | re.S,
    )
    if wholesale_match:
        wholesale_price = float(wholesale_match.group(1))

    details = None
    desc_match = re.search(
        r'itemprop="description"[^>]*>(?P<body>.*?)</div>',
        html,
        re.I | re.S,
    )
    if desc_match:
        details = _strip_html_text(desc_match.group("body"))[:800] or None

    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=canonical_url,
        articul=articul,
        details=details,
        wholesale_price=wholesale_price,
    )


def _parse_stronikum_category_or_search_rows(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if 'class="product-row"' not in html and "price-products" not in html:
        return []

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    for match in _STRONIKUM_CATEGORY_ROW_RE.finditer(html):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        url = _absolute_url(domain, match.group("url"), page_url)
        if not url or not _is_stronikum_product_path(urlparse(url).path):
            continue
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=None,
                wholesale_price=_parse_price(match.group("price")),
                url=_canonical_stronikum_product_url(url),
                articul=None,
            )
        )
    return products


def _is_labkabinet_product_path(path: str) -> bool:
    return bool(re.match(r"^/product/[^/]+/?$", path.rstrip("/"), re.I))


def _labkabinet_product_html_slice(html: str) -> str:
    """Labkabinet puts h1/price/articul late in HTML (~650k+); slice around product block."""
    for marker in ("<h1", 'itemprop="name"'):
        idx = html.find(marker)
        if idx >= 0:
            return html[max(0, idx - 2_000) : idx + 25_000]
    if len(html) > 100_000:
        return html[-100_000:]
    return html


def _parse_labkabinet_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    path = urlparse(page_url.split("#")[0]).path
    if not _is_labkabinet_product_path(path):
        return None

    title_match = re.search(r"<h1[^>]*>\s*(?P<name>[^<]+?)\s*</h1>", html, re.I | re.S)
    if not title_match:
        title_match = re.search(
            r'itemprop="name"[^>]*content="(?P<name>[^"]+)"',
            html,
            re.I,
        )
    if not title_match:
        title_match = re.search(r'itemprop="name"[^>]*>\s*(?P<name>[^<]+?)\s*</', html, re.I)
    if not title_match:
        return None

    name = unescape(re.sub(r"\s+", " ", title_match.group("name")).strip())
    if len(name) < 4:
        return None

    focused = _focus_product_price_html(html)
    price = None
    price_attr = re.search(
        r'class="price"[^>]*data-value="(\d[\d\s.]*)"',
        focused,
        re.I,
    )
    if price_attr:
        price = _parse_price(price_attr.group(1))
    if price is None:
        price = _extract_primary_product_price(focused)

    articul = None
    articul_match = re.search(
        r'class="block_articule"[^>]*>.*?Артикул:\s*([A-Za-z0-9\-_.]+)',
        html,
        re.I | re.S,
    )
    if not articul_match:
        articul_match = _ARTICUL_RE.search(html)

    details = None
    desc_match = re.search(
        r'itemprop="description"[^>]*content="(?P<body>[^"]+)"',
        html,
        re.I,
    )
    if desc_match:
        details = _strip_html_text(desc_match.group("body"))[:800] or None
    if not details:
        desc_match = re.search(
            r'itemprop="description"[^>]*>(?P<body>.*?)</div>',
            html,
            re.I | re.S,
        )
        if desc_match:
            details = _strip_html_text(desc_match.group("body"))[:800] or None

    price_label = price_on_request_label(focused) if price is None else None
    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=page_url.split("#")[0],
        articul=articul_match.group(1) if articul_match else None,
        price_label=price_label,
        details=details,
    )


def _parse_labkabinet_catalog_items(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if "item-title" not in html and "price_value" not in html:
        return []

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'href="(?P<url>/product/[^"]+)".*?'
        r'class="item-title"[^>]*>\s*(?P<name>[^<]+?)\s*</.*?'
        r'class="price_value">(?P<price>[^<]+)</span>',
        re.I | re.S,
    )
    for match in pattern.finditer(html):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        url = _absolute_url(domain, match.group("url"), page_url)
        if not url or not _is_labkabinet_product_path(urlparse(url).path):
            continue
        chunk = html[match.start() : match.start() + 2500]
        articul_match = _ARTICUL_RE.search(chunk)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=_parse_price(match.group("price")),
                url=url,
                articul=articul_match.group(1) if articul_match else None,
            )
        )
    return products


def _parse_labkabinet_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if domain.lower().removeprefix("www.") != "labkabinet.ru":
        return []

    product = _parse_labkabinet_product_html(
        _labkabinet_product_html_slice(html),
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if product:
        return [product]

    return _parse_labkabinet_catalog_items(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )


def _discover_labkabinet_product_urls() -> list[str]:
    index_url = "https://labkabinet.ru/sitemap.xml"
    product_re = re.compile(
        r"<loc>(https://labkabinet\.ru/product/[^<]+)</loc>",
        re.I,
    )
    sitemap_link_re = re.compile(
        r"<loc>(https://labkabinet\.ru/sitemap[^<]*\.xml)</loc>",
        re.I,
    )
    urls: list[str] = []
    seen: set[str] = set()

    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            index_response = client.get(index_url)
            index_response.raise_for_status()
            sitemap_urls = sitemap_link_re.findall(index_response.text)
            if not sitemap_urls:
                sitemap_urls = ["https://labkabinet.ru/sitemap-iblock-2.xml"]

            for sitemap_url in sitemap_urls:
                try:
                    response = client.get(sitemap_url)
                    response.raise_for_status()
                except Exception:
                    continue
                for url in product_re.findall(response.text):
                    clean = url.split("#")[0].split("?")[0]
                    if clean in seen:
                        continue
                    seen.add(clean)
                    urls.append(clean)
    except Exception:
        logger.exception("Failed to fetch labkabinet sitemap")

    return urls


def fetch_labkabinet_catalog(
    site: CompetitorSite,
    *,
    checkpoint_every: int = 500,
) -> list[CompetitorCatalogProduct]:
    from src.services.competitor_product_store import get_competitor_product_store

    product_urls = _discover_labkabinet_product_urls()
    if not product_urls:
        logger.warning("Labkabinet sitemap empty")
        return []

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()
    total = len(product_urls)
    store = get_competitor_product_store() if checkpoint_every > 0 else None
    logger.info("Labkabinet: indexing %s products from sitemap", total)

    with httpx.Client(
        timeout=WEB_SEARCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
    ) as client:
        for index, product_url in enumerate(product_urls, start=1):
            try:
                response = client.get(product_url)
                response.raise_for_status()
            except Exception:
                logger.debug("Labkabinet product fetch failed %s", product_url, exc_info=True)
                continue

            product = _parse_labkabinet_product_html(
                _labkabinet_product_html_slice(response.text),
                domain=site.domain,
                site_label=site.label,
                page_url=str(response.url),
            )
            if not product:
                continue
            key = normalize_name(product.name)
            if key in seen_names:
                continue
            seen_names.add(key)
            products.append(product)

            if store and checkpoint_every > 0 and products and (
                index % checkpoint_every == 0 or index == total
            ):
                saved = store.replace_site_products(
                    site.domain,
                    products,
                    site_label=site.label,
                )
                logger.info(
                    "Labkabinet checkpoint %s/%s urls, %s products saved",
                    index,
                    total,
                    saved,
                )
            elif index % 500 == 0 or index == total:
                logger.info("Labkabinet indexed %s/%s products", index, total)

    logger.info("Labkabinet catalog complete: %s products", len(products))
    return products


_VRTORG_PRODUCT_PATH_RE = re.compile(r"^/catalog/.+/\d+/?$", re.I)
_VRTORG_SITEMAP_PRODUCT_RE = re.compile(
    r"<loc>(https://vrtorg\.ru/catalog/[^<]+/\d+/)</loc>",
    re.I,
)
_VRTORG_SITEMAP_INDEX_RE = re.compile(
    r"<loc>(https://vrtorg\.ru/sitemap[^<]*\.xml)</loc>",
    re.I,
)


def _is_vrtorg_product_path(path: str) -> bool:
    return bool(_VRTORG_PRODUCT_PATH_RE.match(path.rstrip("/")))


def _vrtorg_product_html_slice(html: str) -> str:
    marker_positions: list[int] = []
    for marker in (
        "product-buy__price-current",
        'class="product__title"',
        'class="product__code"',
        "<h1",
    ):
        idx = html.find(marker)
        if idx >= 0:
            marker_positions.append(idx)
    if marker_positions:
        start = max(0, min(marker_positions) - 4_000)
        end = max(marker_positions) + 12_000
        return html[start:end]
    if len(html) > 120_000:
        return html[:120_000]
    return html


def _parse_vrtorg_product_html(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> CompetitorCatalogProduct | None:
    path = urlparse(page_url.split("#")[0]).path
    if not _is_vrtorg_product_path(path):
        return None

    title_match = re.search(
        r'<h1[^>]*class="product__title"[^>]*>\s*(?P<name>[^<]+?)\s*</h1>',
        html,
        re.I | re.S,
    )
    if not title_match:
        title_match = re.search(r"<h1[^>]*>\s*(?P<name>[^<]+?)\s*</h1>", html, re.I | re.S)
    if not title_match:
        return None

    name = unescape(re.sub(r"\s+", " ", title_match.group("name")).strip())
    if len(name) < 4:
        return None

    focused = _vrtorg_product_html_slice(html)
    price = None
    price_match = re.search(
        r'class="product-buy__price-current"[^>]*>(?P<price>[^<]+)',
        focused,
        re.I,
    )
    if price_match:
        price = _parse_price(unescape(price_match.group("price")))
    if price is None:
        price = _extract_primary_product_price(focused)

    articul = None
    articul_match = re.search(
        r'class="product__code"[^>]*>\s*Артикул:\s*(?P<articul>[^<]+)',
        html,
        re.I,
    )
    if not articul_match:
        articul_match = _ARTICUL_RE.search(html)

    price_label = price_on_request_label(focused) if price is None else None
    return CompetitorCatalogProduct(
        domain=domain,
        site_label=site_label,
        name=name[:300],
        price=price,
        url=page_url.split("#")[0],
        articul=articul_match.group("articul").strip() if articul_match else None,
        price_label=price_label,
    )


def _parse_vrtorg_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if domain.lower().removeprefix("www.") != "vrtorg.ru":
        return []

    product = _parse_vrtorg_product_html(
        _vrtorg_product_html_slice(html),
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    return [product] if product else []


def _discover_vrtorg_product_urls() -> list[str]:
    index_url = "https://vrtorg.ru/sitemap.xml"
    urls: list[str] = []
    seen: set[str] = set()

    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            index_response = client.get(index_url)
            index_response.raise_for_status()
            sitemap_urls = _VRTORG_SITEMAP_INDEX_RE.findall(index_response.text)
            if not sitemap_urls:
                sitemap_urls = ["https://vrtorg.ru/sitemap-iblock-9.xml"]

            for sitemap_url in sitemap_urls:
                if "iblock-9" not in sitemap_url.lower():
                    continue
                try:
                    response = client.get(sitemap_url)
                    response.raise_for_status()
                except Exception:
                    continue
                for url in _VRTORG_SITEMAP_PRODUCT_RE.findall(response.text):
                    clean = url.split("#")[0].split("?")[0]
                    if clean in seen:
                        continue
                    seen.add(clean)
                    urls.append(clean)
    except Exception:
        logger.exception("Failed to fetch vrtorg sitemap")

    return urls


def fetch_vrtorg_catalog(
    site: CompetitorSite,
    *,
    checkpoint_every: int = 500,
) -> list[CompetitorCatalogProduct]:
    from src.services.competitor_product_store import get_competitor_product_store

    product_urls = _discover_vrtorg_product_urls()
    if not product_urls:
        logger.warning("Vrtorg sitemap empty")
        return []

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()
    total = len(product_urls)
    store = get_competitor_product_store() if checkpoint_every > 0 else None
    logger.info("Vrtorg: indexing %s products from sitemap", total)

    with httpx.Client(
        timeout=WEB_SEARCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
    ) as client:
        for index, product_url in enumerate(product_urls, start=1):
            response = None
            for attempt in range(3):
                try:
                    response = client.get(product_url)
                    if response.status_code in {429, 502, 503, 504} and attempt < 2:
                        time.sleep(0.6 * (attempt + 1))
                        continue
                    response.raise_for_status()
                    break
                except Exception:
                    if attempt >= 2:
                        logger.debug(
                            "Vrtorg product fetch failed %s",
                            product_url,
                            exc_info=True,
                        )
                        response = None
                    else:
                        time.sleep(0.6 * (attempt + 1))
            if response is None:
                continue

            product = _parse_vrtorg_product_html(
                _vrtorg_product_html_slice(response.text),
                domain=site.domain,
                site_label=site.label,
                page_url=str(response.url),
            )
            if not product:
                continue
            key = normalize_name(product.name)
            if key in seen_names:
                continue
            seen_names.add(key)
            products.append(product)

            if store and checkpoint_every > 0 and products and (
                index % checkpoint_every == 0 or index == total
            ):
                saved = store.replace_site_products(
                    site.domain,
                    products,
                    site_label=site.label,
                )
                logger.info(
                    "Vrtorg checkpoint %s/%s urls, %s products saved",
                    index,
                    total,
                    saved,
                )
            elif index % 500 == 0 or index == total:
                logger.info("Vrtorg indexed %s/%s products", index, total)

    logger.info("Vrtorg catalog complete: %s products", len(products))
    return products


def _parse_stronikum_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if domain.lower().removeprefix("www.") != "stronikum.ru":
        return []

    product = _parse_stronikum_product_html(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if product:
        return [product]

    return _parse_stronikum_category_or_search_rows(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )


def _discover_stronikum_product_urls() -> list[str]:
    sitemap_url = "https://stronikum.ru/sitemap.xml"
    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            response = client.get(sitemap_url)
            response.raise_for_status()
            urls = _STRONIKUM_SITEMAP_PRODUCT_RE.findall(response.text)
            dedup: list[str] = []
            seen: set[str] = set()
            for url in urls:
                clean = _canonical_stronikum_product_url(url)
                if clean in seen:
                    continue
                seen.add(clean)
                dedup.append(clean)
            return dedup
    except Exception:
        logger.exception("Failed to fetch stronikum sitemap")
        return []


def _discover_stronikum_category_urls() -> list[str]:
    menu_url = "https://stronikum.ru/prices"
    category_re = re.compile(r'href="(/(\d+_[^"/]+))"', re.I)
    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            response = client.get(menu_url)
            response.raise_for_status()
            urls: list[str] = []
            seen: set[str] = set()
            for match in category_re.finditer(response.text):
                path = match.group(1)
                if "/" in path.strip("/"):
                    continue
                absolute = f"https://stronikum.ru{path}"
                if absolute in seen:
                    continue
                seen.add(absolute)
                urls.append(absolute)
            return urls
    except Exception:
        logger.exception("Failed to discover stronikum categories")
        return []


def fetch_stronikum_catalog(site: CompetitorSite) -> list[CompetitorCatalogProduct]:
    product_urls = _discover_stronikum_product_urls()
    if not product_urls:
        logger.warning("Stronikum sitemap empty, falling back to category crawl")
        category_urls = _discover_stronikum_category_urls()
        products: list[CompetitorCatalogProduct] = []
        seen_names: set[str] = set()
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            for category_url in category_urls:
                try:
                    response = client.get(category_url)
                    response.raise_for_status()
                except Exception:
                    continue
                for item in _parse_stronikum_category_or_search_rows(
                    response.text[:700_000],
                    domain=site.domain,
                    site_label=site.label,
                    page_url=str(response.url),
                ):
                    key = normalize_name(item.name)
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    products.append(item)
        return products

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()
    total = len(product_urls)
    logger.info("Stronikum: indexing %s products from sitemap", total)

    with httpx.Client(
        timeout=WEB_SEARCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
    ) as client:
        for index, product_url in enumerate(product_urls, start=1):
            modal_url = f"{product_url}.modal"
            html = ""
            final_url = product_url
            try:
                response = client.get(modal_url)
                if response.status_code >= 400:
                    response = client.get(product_url)
                response.raise_for_status()
                html = response.text[:200_000]
                final_url = str(response.url)
            except Exception:
                logger.debug("Stronikum product fetch failed %s", product_url, exc_info=True)
                continue

            product = _parse_stronikum_product_html(
                html,
                domain=site.domain,
                site_label=site.label,
                page_url=final_url,
            )
            if not product:
                continue
            key = normalize_name(product.name)
            if key in seen_names:
                continue
            seen_names.add(key)
            products.append(product)

            if index % 250 == 0 or index == total:
                logger.info("Stronikum indexed %s/%s products", index, total)

    logger.info("Stronikum catalog complete: %s products", len(products))
    return products


def _site_root(domain: str) -> str:
    return f"https://{domain.removeprefix('www.')}"


def _absolute_url(domain: str, href: str, base_url: str) -> str:
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("http"):
        return href.split("#")[0]
    if href.startswith("/"):
        return f"{_site_root(domain)}{href}"
    return urljoin(base_url.rstrip("/") + "/", href)


def _parse_price(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.replace("\xa0", " ").replace("&nbsp;", " ").strip()
    if not text or text in {"—", "-", "none", "null"}:
        return None
    prices = extract_prices_from_text(text)
    if prices:
        return max(prices)
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    try:
        value = float(cleaned.replace(",", "."))
    except ValueError:
        return None
    if 10 <= value <= 50_000_000:
        return round(value, 2)
    return None


def _extract_title_near(href_index: int, html: str) -> str:
    window = html[max(0, href_index - 400) : href_index + 400]
    plain = re.sub(r"\s+", " ", _TAG_RE.sub(" ", window)).strip()
    plain = re.sub(r"Артикул:\s*[A-Za-z0-9\-_.]+", "", plain, flags=re.I)
    plain = re.sub(r"Добавить к сравнению|Купить", "", plain, flags=re.I)
    return plain.strip(" |-")


_PREVIEW_PRODUCT_NAME_RE = re.compile(
    r'itemprop="name"\s+href="(?P<url>[^"]+)"[^>]*>\s*(?P<name>[^<]+?)\s*</a>',
    re.I | re.S,
)


def _focus_product_price_html(html: str) -> str:
    for marker in (
        "js_price_wrapper",
        "offers_price",
        "price_value_block",
        "product-buy__price-current",
        'itemprop="price"',
        "<h1",
    ):
        idx = html.find(marker)
        if idx >= 0:
            return html[idx : idx + 12_000]
    related_idx = html.find("Покупают вместе")
    if related_idx > 0:
        return html[:related_idx]
    return html[:120_000]


def _extract_primary_product_price(html: str) -> float | None:
    focused = _focus_product_price_html(html)
    prices = extract_prices_from_text(focused)
    if prices:
        return min(prices)
    return None


def _parse_n72_product_previews(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if "n72r-product-preview" not in html:
        return []

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    for match in _N72_PREVIEW_RE.finditer(html):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        url = _absolute_url(domain, match.group("url"), page_url)
        chunk = html[match.start() : match.start() + 3000]
        articul_match = re.search(
            r'data-value="([A-Za-z0-9\-_.]+)"[^>]*>\s*<span>\s*Артикул:',
            chunk,
            re.I | re.S,
        )
        if not articul_match:
            articul_match = _ARTICUL_RE.search(chunk)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=_parse_price(match.group("price")),
                url=url or None,
                articul=articul_match.group(1) if articul_match else None,
            )
        )
    return products


def _is_product_detail_path(path: str) -> bool:
    lower = path.lower().rstrip("/")
    if "/catalog/product/" in lower:
        return True
    if re.match(r"^/product/[^/]+$", lower):
        return True
    if _is_vrtorg_product_path(lower):
        return True
    return "/products/" in lower and lower.count("/") >= 3


def _parse_preview_product_blocks(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    if "preview_product" not in html.lower() and 'itemprop="name"' not in html.lower():
        return []

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    for match in _PREVIEW_PRODUCT_NAME_RE.finditer(html):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        url = _absolute_url(domain, match.group("url"), page_url)
        chunk = html[match.start() : match.start() + 2500]
        articul_match = _ARTICUL_RE.search(chunk)
        prices = extract_prices_from_text(chunk)
        label = price_on_request_label(chunk) if not prices else None
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=prices[0] if prices else None,
                url=url or None,
                articul=articul_match.group(1) if articul_match else None,
                price_label=label,
            )
        )
    return products


def _parse_product_detail_page(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    path = urlparse(page_url).path.lower()
    if not _is_product_detail_path(path):
        return []

    title_match = re.search(r"<h1[^>]*>\s*(?P<name>[^<]+?)\s*</h1>", html, re.I | re.S)
    if not title_match:
        title_match = re.search(
            r'itemprop="name"[^>]*>\s*(?P<name>[^<]+?)\s*</',
            html,
            re.I | re.S,
        )
    if not title_match:
        return []

    name = re.sub(r"\s+", " ", title_match.group("name")).strip()
    if len(name) < 4:
        return []

    focused = _focus_product_price_html(html)
    articul_match = re.search(
        r'data-value="([A-Za-z0-9\-_.]+)"[^>]*>\s*<span>\s*Артикул:',
        html[:120_000],
        re.I | re.S,
    )
    if not articul_match:
        articul_match = _ARTICUL_RE.search(html[:120_000])
    price = _extract_primary_product_price(focused)
    price_label = price_on_request_label(focused) if price is None else None
    return [
        CompetitorCatalogProduct(
            domain=domain,
            site_label=site_label,
            name=name[:300],
            price=price,
            url=page_url.split("#")[0],
            articul=articul_match.group(1) if articul_match else None,
            price_label=price_label,
        )
    ]


def _parse_shop2_products(
    html: str,
    *,
    domain: str,
    site_label: str,
    page_url: str,
) -> list[CompetitorCatalogProduct]:
    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    blocks = re.findall(
        r'class="product-top"(?P<body>.*?)class="price-current"><strong[^>]*>(?P<price>[^<]+)</strong>',
        html,
        re.I | re.S,
    )
    for body, price_raw in blocks:
        name_match = re.search(
            r'class="product-name"><a\s+href="(?P<url>[^"]+)">(?P<name>[^<]+)</a>',
            body,
            re.I | re.S,
        )
        if not name_match:
            continue
        name = re.sub(r"\s+", " ", name_match.group("name")).strip()
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        articul_match = re.search(r'class="article">Артикул:\s*<span>(?P<articul>[^<]+)</span>', body, re.I)
        url = _absolute_url(domain, name_match.group("url"), page_url)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=_parse_price(price_raw),
                url=url or None,
                articul=articul_match.group("articul").strip() if articul_match else None,
            )
        )
    return products


def parse_catalog_html(html: str, *, domain: str, site_label: str, page_url: str) -> list[CompetitorCatalogProduct]:
    if not html.strip():
        return []

    stronikum_products = _parse_stronikum_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if stronikum_products:
        return stronikum_products

    labkabinet_products = _parse_labkabinet_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if labkabinet_products:
        return labkabinet_products

    vrtorg_products = _parse_vrtorg_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if vrtorg_products:
        return vrtorg_products

    shop2_products = _parse_shop2_products(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if shop2_products:
        return shop2_products

    n72_products = _parse_n72_product_previews(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if n72_products:
        if _is_product_detail_path(urlparse(page_url).path.lower()):
            detail_products = _parse_product_detail_page(
                html,
                domain=domain,
                site_label=site_label,
                page_url=page_url,
            )
            if detail_products:
                return detail_products
        return n72_products

    preview_products = _parse_preview_product_blocks(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if preview_products and _is_product_detail_path(urlparse(page_url).path.lower()):
        path_parts = [part for part in urlparse(page_url).path.split("/") if part]
        if len(path_parts) >= 3:
            detail_products = _parse_product_detail_page(
                html,
                domain=domain,
                site_label=site_label,
                page_url=page_url,
            )
            if detail_products:
                return detail_products

    if preview_products:
        return preview_products

    detail_products = _parse_product_detail_page(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if detail_products:
        return detail_products

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()

    for match in _HREF_RE.finditer(html):
        href = match.group(1)
        lower = href.lower()
        if not any(
            token in lower
            for token in (
                "/products/",
                "/product/",
                "/catalog/product/",
                "/tovar/",
                "/goods/",
                "/item/",
                "/magazin/",
            )
        ):
            continue
        if "/folder/" in lower or "/search" in lower or "/cart" in lower:
            continue
        url = _absolute_url(domain, href, page_url)
        if not url or domain not in urlparse(url).netloc.lower():
            continue
        name = _extract_title_near(match.start(), html)
        if len(name) < 4:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        chunk = html[match.start() : match.start() + 2500]
        articul_match = _ARTICUL_RE.search(chunk)
        prices = extract_prices_from_text(chunk)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=prices[0] if prices else None,
                url=url,
                articul=articul_match.group(1) if articul_match else None,
            )
        )

    plain = re.sub(r"\s+", " ", _TAG_RE.sub("\n", html))
    blocks = re.split(r"(?=Артикул:\s*[A-Za-z0-9\-_.]+)", plain)
    for block in blocks:
        articul_match = _ARTICUL_RE.search(block)
        if not articul_match:
            continue
        prices = extract_prices_from_text(block)
        if not prices:
            continue
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        name = ""
        for line in lines:
            if "Артикул:" in line:
                continue
            if "руб" in line.lower():
                continue
            if len(line) >= 8:
                name = line
                break
        if not name:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        products.append(
            CompetitorCatalogProduct(
                domain=domain,
                site_label=site_label,
                name=name[:300],
                price=prices[0],
                url=None,
                articul=articul_match.group(1),
            )
        )

    return products


def _discover_folder_urls(html: str, *, domain: str, page_url: str, limit: int = 12) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for href in _HREF_RE.findall(html):
        lower = href.lower()
        if "/magazin/folder/" not in lower and "/catalog/" not in lower:
            continue
        absolute = _absolute_url(domain, href, page_url)
        if not absolute or absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
        if len(urls) >= limit:
            break
    return urls


def resolve_catalog_urls(
    site: CompetitorSite,
    *,
    extra_urls: list[str] | None = None,
) -> list[str]:
    from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry

    urls: list[str] = []
    if site.search_url:
        urls.append(site.search_url.format(query=quote_plus("")))
    urls.extend(_CATALOG_SEED_URLS.get(site.domain, []))
    urls.append(_site_root(site.domain))
    urls.extend(get_competitor_catalog_url_registry().urls_for_domain(site.domain))
    if extra_urls:
        urls.extend(extra_urls)

    dedup: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = url.strip().split("#")[0]
        if normalized and normalized not in seen:
            seen.add(normalized)
            dedup.append(normalized)
    return dedup


def fetch_catalog_page(
    page_url: str,
    *,
    domain: str,
    site_label: str,
) -> list[CompetitorCatalogProduct]:
    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            response = client.get(page_url)
            response.raise_for_status()
            return parse_catalog_html(
                response.text[:700_000],
                domain=domain,
                site_label=site_label,
                page_url=str(response.url),
            )
    except Exception:
        logger.debug("Catalog page fetch failed %s", page_url, exc_info=True)
        return []


def fetch_catalog_products(
    site: CompetitorSite,
    *,
    max_pages: int = 6,
    extra_urls: list[str] | None = None,
) -> list[CompetitorCatalogProduct]:
    normalized_domain = site.domain.lower().removeprefix("www.")
    if normalized_domain == "stronikum.ru":
        return fetch_stronikum_catalog(site)
    if normalized_domain == "labkabinet.ru":
        return fetch_labkabinet_catalog(site)
    if normalized_domain == "vrtorg.ru":
        return fetch_vrtorg_catalog(site)

    dedup_urls = resolve_catalog_urls(site, extra_urls=extra_urls)

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()
    seen_urls: set[str] = set()

    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            discovered: list[str] = []
            for page_url in list(dedup_urls[:max_pages]):
                if page_url in seen_urls:
                    continue
                seen_urls.add(page_url)
                try:
                    response = client.get(page_url)
                    response.raise_for_status()
                except Exception:
                    logger.debug("Catalog fetch failed %s", page_url, exc_info=True)
                    continue
                html = response.text[:700_000]
                final_url = str(response.url)
                discovered.extend(
                    _discover_folder_urls(
                        html,
                        domain=site.domain,
                        page_url=final_url,
                    )
                )
                page_products = parse_catalog_html(
                    html,
                    domain=site.domain,
                    site_label=site.label,
                    page_url=final_url,
                )
                for item in page_products:
                    key = normalize_name(item.name)
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    products.append(item)

            for page_url in discovered:
                if page_url in seen_urls:
                    continue
                seen_urls.add(page_url)
                try:
                    response = client.get(page_url)
                    response.raise_for_status()
                except Exception:
                    continue
                for item in parse_catalog_html(
                    response.text[:700_000],
                    domain=site.domain,
                    site_label=site.label,
                    page_url=str(response.url),
                ):
                    key = normalize_name(item.name)
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    products.append(item)
    except Exception:
        logger.exception("Catalog crawl failed for %s", site.domain)

    return products


def products_to_rag_text(
    products: list[CompetitorCatalogProduct],
    *,
    site: CompetitorSite | None = None,
    title: str = "",
) -> str:
    header_label = site.label if site else title or "Все конкуренты"
    header_domain = site.domain if site else "all"
    lines = [
        f"Каталог конкурента: {header_label}",
        f"Домен: {header_domain}",
        f"Поиск: {site.search_url if site else '—'}",
        f"Позиций: {len(products)}",
        "",
    ]
    for product in products:
        lines.append(
            "[product] "
            f"domain={product.domain} | site={product.site_label} | "
            f"name={product.name} | price={product.price or ''} | "
            f"url={product.url or ''} | articul={product.articul or ''} | "
            f"price_label={product.price_label or ''} | "
            f"wholesale_price={product.wholesale_price or ''} | "
            f"details={product.details or ''}"
        )
    return "\n".join(lines)


def index_competitor_page_url(
    page_url: str,
    *,
    domain: str,
    site_label: str,
    doc_rag_index,
) -> dict[str, int | bool | str]:
    from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry
    from src.services.competitor_product_store import get_competitor_product_store

    products = fetch_catalog_page(page_url, domain=domain, site_label=site_label)
    store = get_competitor_product_store()
    added = store.merge_products(products, domain=domain, site_label=site_label)
    store.record_indexed_page(
        page_url,
        domain=domain,
        site_label=site_label,
        products_count=len(products),
    )
    get_competitor_catalog_url_registry().add_page(
        page_url,
        domain=domain,
        label=site_label,
        source="page_index",
    )

    page_doc_id = f"competitor-page:{domain}:{abs(hash(page_url)) % 10_000_000}"
    rag_result: dict[str, int | bool | str] = {"indexed": False, "chunks": 0}
    if products:
        rag_result = doc_rag_index.index_text(
            doc_id=page_doc_id,
            source_type="competitor",
            source_name=f"{site_label} | {page_url}",
            text=(
                f"Страница каталога: {page_url}\n"
                f"Домен: {domain}\n"
                f"Позиций: {len(products)}\n\n"
                + products_to_rag_text(products, title=site_label)
            ),
            filename=page_url,
            force=True,
        )

    sync_unified_competitor_rag(doc_rag_index)
    return {
        "url": page_url,
        "domain": domain,
        "products_found": len(products),
        "products_added": added,
        "rag": rag_result,
    }


def sync_unified_competitor_rag(doc_rag_index) -> dict[str, int | bool]:
    from src.services.competitor_product_store import get_competitor_product_store

    products = get_competitor_product_store().iter_products()
    if not products:
        return {"indexed": False, "chunks": 0, "products": 0}

    text = products_to_rag_text(products, title="Каталог всех конкурентов")
    result = doc_rag_index.index_text(
        doc_id="competitor-catalog:all",
        source_type="competitor",
        source_name="Каталог конкурентов",
        text=text,
        filename="competitor-catalog-all",
        force=True,
    )
    result["products"] = len(products)
    return result


def index_competitor_site_catalog(
    site: CompetitorSite,
    doc_rag_index,
    *,
    force: bool = False,
    extra_urls: list[str] | None = None,
) -> dict[str, int | bool]:
    from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry
    from src.services.competitor_product_store import get_competitor_product_store

    doc_id = f"competitor-catalog:{site.domain}"
    doc_rag_index.ensure_loaded()
    store = get_competitor_product_store()
    if not force and store.has_site(site.domain) and doc_id in doc_rag_index._entries:
        entry = doc_rag_index._entries[doc_id]
        return {
            "indexed": True,
            "products": len(store.products_for_domain(site.domain)),
            "chunks": len(entry.chunks),
            "skipped": True,
        }

    for page_url in extra_urls or []:
        get_competitor_catalog_url_registry().add_page(
            page_url,
            domain=site.domain,
            label=site.label,
            source="site_index",
        )

    products = fetch_catalog_products(site, extra_urls=extra_urls)
    existing_products = store.products_for_domain(site.domain)
    if not products and existing_products:
        logger.warning(
            "Catalog fetch returned 0 products for %s — keeping %s existing entries",
            site.domain,
            len(existing_products),
        )
        products = existing_products
        store_count = len(existing_products)
    elif products:
        store_count = store.replace_site_products(
            site.domain,
            products,
            site_label=site.label,
        )
    else:
        store_count = 0
    for page_url in resolve_catalog_urls(site, extra_urls=extra_urls)[:12]:
        store.record_indexed_page(
            page_url,
            domain=site.domain,
            site_label=site.label,
            products_count=len(
                [p for p in products if p.url and page_url.split("#")[0] in (p.url or "")]
            )
            or len(products),
        )
    store.save()
    products = store.products_for_domain(site.domain)

    if not products:
        return {"indexed": False, "products": 0, "chunks": 0, "store_products": store_count}

    text = products_to_rag_text(products, site=site)
    result = doc_rag_index.index_text(
        doc_id=doc_id,
        source_type="competitor",
        source_name=site.label,
        text=text,
        filename=f"{site.domain}-catalog",
        force=True,
    )
    result["products"] = len(products)
    result["store_products"] = store_count
    sync_unified_competitor_rag(doc_rag_index)
    return result


def reindex_all_competitor_sites(
    doc_rag_index,
    *,
    force: bool = True,
) -> dict[str, object]:
    from src.services.competitor_site_manager import get_competitor_site_manager
    from src.services.competitor_product_store import get_competitor_product_store

    results: list[dict[str, object]] = []
    manager = get_competitor_site_manager()

    for site in competitor_sites_with_search():
        try:
            result = index_competitor_site_catalog(site, doc_rag_index, force=force)
            results.append({"domain": site.domain, "label": site.label, **result})
        except Exception:
            logger.exception("Failed to reindex competitor site %s", site.domain)
            results.append({"domain": site.domain, "label": site.label, "indexed": False, "error": True})

    for entry in manager.list_custom():
        site = CompetitorSite(
            domain=entry.domain,
            label=entry.label or entry.domain,
            search_url=entry.search_url,
        )
        try:
            result = index_competitor_site_catalog(
                site,
                doc_rag_index,
                force=force,
                extra_urls=entry.catalog_urls,
            )
            for page_url in entry.catalog_urls:
                index_competitor_page_url(
                    page_url,
                    domain=entry.domain,
                    site_label=entry.label,
                    doc_rag_index=doc_rag_index,
                )
            results.append({"domain": entry.domain, "label": entry.label, "custom": True, **result})
        except Exception:
            logger.exception("Failed to reindex custom competitor site %s", entry.domain)
            results.append(
                {"domain": entry.domain, "label": entry.label, "custom": True, "indexed": False, "error": True}
            )

    unified = sync_unified_competitor_rag(doc_rag_index)
    store_stats = get_competitor_product_store().stats()
    rag_stats = doc_rag_index.stats()
    return {
        "sites": results,
        "unified_rag": unified,
        "catalog_products": store_stats,
        "rag_docs": rag_stats,
    }


def bootstrap_competitor_catalogs(doc_rag_index, *, max_new_sites: int | None = None) -> None:
    from src.services.competitor_product_store import get_competitor_product_store

    store = get_competitor_product_store()
    sites = sorted(
        competitor_sites_with_search(),
        key=lambda site: 0 if site.domain in _CATALOG_SEED_URLS else 1,
    )
    indexed_new = 0
    for site in sites:
        doc_id = f"competitor-catalog:{site.domain}"
        doc_rag_index.ensure_loaded()
        if store.has_site(site.domain) and doc_id in doc_rag_index._entries:
            continue
        if max_new_sites is not None and indexed_new >= max_new_sites:
            break
        try:
            index_competitor_site_catalog(site, doc_rag_index)
            indexed_new += 1
        except Exception:
            logger.exception("Failed to index competitor catalog for %s", site.domain)


def bootstrap_competitor_catalogs_priority(
    doc_rag_index,
    *,
    domains: list[str] | None = None,
) -> None:
    if not domains:
        bootstrap_competitor_catalogs(doc_rag_index, max_new_sites=1)
        return
    domain_set = {domain.lower() for domain in domains}
    for site in competitor_sites_with_search():
        if site.domain.lower() not in domain_set:
            continue
        try:
            index_competitor_site_catalog(site, doc_rag_index, force=False)
        except Exception:
            logger.exception("Failed to index priority competitor catalog for %s", site.domain)


def parse_product_from_chunk(text: str) -> CompetitorCatalogProduct | None:
    match = _PRODUCT_LINE_RE.search(text.strip())
    if match:
        return CompetitorCatalogProduct(
            domain=match.group("domain").strip(),
            site_label=match.group("site").strip(),
            name=match.group("name").strip(),
            price=_parse_price(match.group("price")),
            url=(match.group("url").strip() or None),
            articul=(match.group("articul").strip() or None),
            price_label=(match.group("price_label") or "").strip() or None,
            details=(match.group("details") or "").strip() or None,
            wholesale_price=_parse_price(match.group("wholesale_price")),
        )

    domain = ""
    for token in text.split():
        if token.startswith("domain="):
            domain = token.split("=", 1)[1]
            break
    if "name=" not in text:
        return None
    name_part = text.split("name=", 1)[1]
    name = name_part.split("|", 1)[0].strip()
    if len(name) < 4:
        return None
    price = None
    if "price=" in text:
        price_raw = text.split("price=", 1)[1].split("|", 1)[0].strip()
        price = _parse_price(price_raw)
    url = None
    if "url=" in text:
        url_raw = text.split("url=", 1)[1].split("|", 1)[0].strip()
        url = url_raw or None
    articul = None
    if "articul=" in text:
        articul_raw = text.split("articul=", 1)[1].split("|", 1)[0].strip()
        articul = articul_raw or None
    price_label = None
    if "price_label=" in text:
        price_label_raw = text.split("price_label=", 1)[1].split("|", 1)[0].strip()
        price_label = price_label_raw or None
    return CompetitorCatalogProduct(
        domain=domain or "unknown",
        site_label=competitor_label_for_url(url) or domain or "Конкурент",
        name=name,
        price=price,
        url=url,
        articul=articul,
        price_label=price_label,
    )


def _iter_catalog_products(doc_rag_index) -> list[CompetitorCatalogProduct]:
    from src.services.competitor_product_store import get_competitor_product_store

    store_products = get_competitor_product_store().iter_products()
    if store_products:
        return store_products

    doc_rag_index.ensure_loaded()
    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    product_line_re = re.compile(r"\[product\][^\n]+", re.I)
    for entry in doc_rag_index._entries.values():
        if not str(entry.doc_id).startswith("competitor-catalog:"):
            continue
        full_text = "\n".join(str(chunk.get("text", "")) for chunk in entry.chunks)
        for line in product_line_re.findall(full_text):
            product = parse_product_from_chunk(line)
            if not product:
                continue
            key = normalize_name(product.name)
            if key in seen:
                continue
            seen.add(key)
            products.append(product)
    return products


def enrich_catalog_product_price(
    product: CompetitorCatalogProduct,
) -> CompetitorCatalogProduct:
    if product.price is not None or product.price_label:
        return product
    if not product.url or not is_competitor_product_page_url(product.url):
        return product

    fetched = fetch_catalog_page(
        product.url,
        domain=product.domain,
        site_label=product.site_label,
    )
    if not fetched:
        return product

    for item in fetched:
        if normalize_name(item.name) == normalize_name(product.name):
            return CompetitorCatalogProduct(
                domain=product.domain,
                site_label=product.site_label,
                name=product.name,
                price=item.price if item.price is not None else product.price,
                url=product.url,
                articul=item.articul or product.articul,
                price_label=item.price_label or product.price_label,
                details=item.details or product.details,
                wholesale_price=item.wholesale_price or product.wholesale_price,
            )
    if fetched[0].price is not None or fetched[0].price_label:
        item = fetched[0]
        return CompetitorCatalogProduct(
            domain=product.domain,
            site_label=product.site_label,
            name=product.name,
            price=item.price,
            url=product.url,
            articul=item.articul or product.articul,
            price_label=item.price_label,
            details=item.details or product.details,
            wholesale_price=item.wholesale_price or product.wholesale_price,
        )
    return product


def _score_catalog_products(
    query: str,
    products: list[CompetitorCatalogProduct],
    *,
    limit: int,
) -> list[PriceQuote]:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return []

    scored: list[tuple[float, CompetitorCatalogProduct]] = []
    seen: set[str] = set()

    for product in products:
        if product.url and not is_competitor_product_page_url(product.url):
            continue
        key = normalize_name(product.name)
        if key in seen:
            continue
        score = float(name_match_score(normalized_query, key))
        query_words = normalized_query.split()
        token_match = bool(
            query_words and all(word in key for word in query_words if len(word) >= 3)
        )
        if score < COMPETITOR_SEARCH_FALLBACK_THRESHOLD and not token_match:
            continue
        seen.add(key)
        if product.price is None and not product.price_label and product.url:
            product = enrich_catalog_product_price(product)
        scored.append((max(score, 96.0 if token_match else score), product))

    scored.sort(key=lambda item: item[0], reverse=True)
    quotes: list[PriceQuote] = []
    for score, product in scored[:limit]:
        label = product.site_label or competitor_label_for_url(product.url) or product.domain
        quotes.append(
            PriceQuote(
                source="web",
                label=label,
                matched_name=product.name,
                price=product.price,
                cost=product.price,
                price_label=product.price_label,
                wholesale_price=product.wholesale_price,
                articul=product.articul,
                match_score=round(score, 1),
                url=product.url,
                notes=(
                    f"Индекс каталога | articul: {product.articul}"
                    if product.articul
                    else "Индекс каталога конкурента"
                ),
            )
        )
    return quotes


def search_competitor_catalog_rag(
    query: str,
    doc_rag_index,
    *,
    limit: int = 10,
) -> list[PriceQuote]:
    from src.services.competitor_product_store import get_competitor_product_store

    store = get_competitor_product_store()
    candidates: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()

    def _add(product: CompetitorCatalogProduct | None) -> None:
        if not product or not product.name.strip():
            return
        key = normalize_name(product.name)
        if not key or key in seen:
            return
        seen.add(key)
        candidates.append(product)

    for product in store.search_products(query, limit=max(limit * 3, 24)):
        _add(product)

    if doc_rag_index is not None:
        rows = doc_rag_index.query(query, source_type="competitor", top_k=max(limit * 4, 16))
        for row in rows:
            text = str(row.get("text", ""))
            for line in re.findall(r"\[product\][^\n]+", text, re.I):
                _add(parse_product_from_chunk(line))
            if "[product]" not in text.lower():
                _add(parse_product_from_chunk(text))

    if not candidates:
        for product in _iter_catalog_products(doc_rag_index):
            _add(product)

    return _score_catalog_products(query, candidates, limit=limit)
