from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

from src.config import COMPETITOR_SEARCH_FALLBACK_THRESHOLD, WEB_SEARCH_TIMEOUT
from src.services.competitor_sites import (
    CompetitorSite,
    competitor_label_for_url,
    competitor_sites_with_search,
)
from src.services.data_loader import normalize_name
from src.services.fuzzy_scoring import name_match_score
from src.services.models import PriceQuote
from src.services.web_search_service import extract_prices_from_text

logger = logging.getLogger(__name__)

_CATALOG_SEED_URLS: dict[str, list[str]] = {
    "skale.ru": [
        "https://skale.ru/magazin",
        "https://skale.ru/magazin/folder/uchebnoe-oborudovanie-po-astronomii-i-astrofizike",
        "https://skale.ru/prays-list",
    ],
}

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_ARTICUL_RE = re.compile(r"Артикул:\s*(?:<span>)?([A-Za-z0-9\-_.]+)", re.I)
_PRODUCT_LINE_RE = re.compile(
    r"^\[product\]\s*domain=(?P<domain>[^|]+)\s*\|\s*site=(?P<site>[^|]+)\s*\|"
    r"\s*name=(?P<name>[^|]+)\s*\|\s*price=(?P<price>[^|]*)\s*\|"
    r"\s*url=(?P<url>[^|]*)\s*\|\s*articul=(?P<articul>[^|]*)",
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

    shop2_products = _parse_shop2_products(
        html,
        domain=domain,
        site_label=site_label,
        page_url=page_url,
    )
    if shop2_products:
        return shop2_products

    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()

    for match in _HREF_RE.finditer(html):
        href = match.group(1)
        lower = href.lower()
        if not any(token in lower for token in ("/product/", "/tovar/", "/goods/", "/item/", "/magazin/")):
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


def fetch_catalog_products(site: CompetitorSite, *, max_pages: int = 4) -> list[CompetitorCatalogProduct]:
    urls: list[str] = []
    if site.search_url:
        urls.append(site.search_url.format(query=quote_plus("")))
    urls.extend(_CATALOG_SEED_URLS.get(site.domain, []))
    urls.append(_site_root(site.domain))

    dedup_urls: list[str] = []
    seen_urls: set[str] = set()
    for url in urls:
        if url and url not in seen_urls:
            seen_urls.add(url)
            dedup_urls.append(url)

    products: list[CompetitorCatalogProduct] = []
    seen_names: set[str] = set()

    try:
        with httpx.Client(
            timeout=WEB_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KP-Assistant/1.0)"},
        ) as client:
            discovered: list[str] = []
            for page_url in list(dedup_urls[:max_pages]):
                try:
                    response = client.get(page_url)
                    response.raise_for_status()
                except Exception:
                    logger.debug("Catalog fetch failed %s", page_url, exc_info=True)
                    continue
                html = response.text[:700_000]
                discovered.extend(
                    _discover_folder_urls(
                        html,
                        domain=site.domain,
                        page_url=str(response.url),
                    )
                )
                for item in parse_catalog_html(
                    html,
                    domain=site.domain,
                    site_label=site.label,
                    page_url=str(response.url),
                ):
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


def products_to_rag_text(products: list[CompetitorCatalogProduct], *, site: CompetitorSite) -> str:
    lines = [
        f"Каталог конкурента: {site.label}",
        f"Домен: {site.domain}",
        f"Поиск: {site.search_url or '—'}",
        f"Позиций: {len(products)}",
        "",
    ]
    for product in products[:500]:
        lines.append(
            "[product] "
            f"domain={product.domain} | site={product.site_label} | "
            f"name={product.name} | price={product.price or ''} | "
            f"url={product.url or ''} | articul={product.articul or ''}"
        )
    return "\n".join(lines)


def index_competitor_site_catalog(
    site: CompetitorSite,
    doc_rag_index,
    *,
    force: bool = False,
) -> dict[str, int | bool]:
    doc_id = f"competitor-catalog:{site.domain}"
    doc_rag_index.ensure_loaded()
    if not force and doc_id in doc_rag_index._entries:
        entry = doc_rag_index._entries[doc_id]
        return {
            "indexed": True,
            "products": 0,
            "chunks": len(entry.chunks),
            "skipped": True,
        }

    products = fetch_catalog_products(site)
    text = products_to_rag_text(products, site=site)
    if not text.strip():
        return {"indexed": False, "products": 0, "chunks": 0}
    result = doc_rag_index.index_text(
        doc_id=doc_id,
        source_type="competitor",
        source_name=site.label,
        text=text,
        filename=f"{site.domain}-catalog",
        force=force,
    )
    result["products"] = len(products)
    return result


def bootstrap_competitor_catalogs(doc_rag_index, *, max_new_sites: int | None = None) -> None:
    sites = sorted(
        competitor_sites_with_search(),
        key=lambda site: 0 if site.domain in _CATALOG_SEED_URLS else 1,
    )
    indexed_new = 0
    for site in sites:
        doc_id = f"competitor-catalog:{site.domain}"
        doc_rag_index.ensure_loaded()
        if doc_id in doc_rag_index._entries:
            continue
        if max_new_sites is not None and indexed_new >= max_new_sites:
            break
        try:
            index_competitor_site_catalog(site, doc_rag_index)
            indexed_new += 1
        except Exception:
            logger.exception("Failed to index competitor catalog for %s", site.domain)


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
    return CompetitorCatalogProduct(
        domain=domain or "unknown",
        site_label=competitor_label_for_url(url) or domain or "Конкурент",
        name=name,
        price=price,
        url=url,
        articul=articul,
    )


def _iter_catalog_products(doc_rag_index) -> list[CompetitorCatalogProduct]:
    doc_rag_index.ensure_loaded()
    products: list[CompetitorCatalogProduct] = []
    seen: set[str] = set()
    for entry in doc_rag_index._entries.values():
        if not str(entry.doc_id).startswith("competitor-catalog:"):
            continue
        for chunk in entry.chunks:
            for line in str(chunk.get("text", "")).splitlines():
                if "[product]" not in line:
                    continue
                product = parse_product_from_chunk(line)
                if not product:
                    continue
                key = normalize_name(product.name)
                if key in seen:
                    continue
                seen.add(key)
                products.append(product)
    return products


def search_competitor_catalog_rag(
    query: str,
    doc_rag_index,
    *,
    limit: int = 10,
) -> list[PriceQuote]:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return []

    products = _iter_catalog_products(doc_rag_index)
    if not products:
        rows = doc_rag_index.query(query, source_type="competitor", top_k=max(limit * 4, 12))
        for row in rows:
            product = parse_product_from_chunk(str(row.get("text", "")))
            if product:
                products.append(product)

    scored: list[tuple[float, CompetitorCatalogProduct]] = []
    seen: set[str] = set()

    for product in products:
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
                match_score=round(score, 1),
                url=product.url,
                notes=(
                    f"RAG-каталог | articul: {product.articul}"
                    if product.articul
                    else "RAG-каталог конкурента"
                ),
            )
        )
    return quotes
