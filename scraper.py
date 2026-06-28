#!/usr/bin/env python3
"""
Обход интернет-магазинов конкурентов с помощью Crawlee (BeautifulSoupCrawler).

Извлекает название, цену и наличие, сохраняет результат в products.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext
from crawlee.storages import Dataset

from src.services.competitor_sites import COMPETITOR_SITES, competitor_label_for_url

OUTPUT_FILE = Path("products.json")

_PRICE_RE = re.compile(
    r"(?<!\d)(\d[\d\s\u00a0]{0,12}(?:[.,]\d{1,2})?)(?!\d)"
)
_SKIP_LINK_RE = re.compile(
    r"/(?:search|login|auth|register|cart|basket|compare|wishlist|"
    r"account|policy|privacy|contact|news|blog|upload)(?:/|$|\?)",
    re.I,
)
_PRODUCT_LINK_RE = re.compile(
    r"/(?:products?|catalog|katalog|tovar|goods|item|shop|card|prays)(?:/|$|\?)",
    re.I,
)

# Стартовые URL каталогов конкурентов
_CATALOG_PATHS: dict[str, str] = {
    "xn----7sbbumkojddmeoc1a7r.xn--p1acf": "/products/",
    "skale.ru": "/prays-list",
    "prioritet1.com": "/catalog/",
    "orionedu.ru": "/shop/",
    "rene-edu.ru": "/catalog/",
    "rostcom.com": "/catalog/",
    "zarnitza.ru": "/catalog/",
    "epp24.ru": "/catalog/",
    "vrtorg.ru": "/catalog/",
    "labkabinet.ru": "/catalog/",
    "stronikum.ru": "/prices",
    "n-72.ru": "/catalog/",
    "td-school.ru": "/",
    "xn--54-vlc3b6bza.xn--p1ai": "/",
    "music-expert.ru": "/catalog/",
}


def build_start_urls() -> list[str]:
    urls: list[str] = []
    for site in COMPETITOR_SITES:
        path = _CATALOG_PATHS.get(site.domain, "/")
        host = site.domain.removeprefix("www.")
        if host == "prioritet1.com" or host == "rostcom.com":
            urls.append(f"https://www.{host}{path}")
        else:
            urls.append(f"https://{host}{path}")
    return urls


_NAV_TITLES = {
    "главная",
    "каталог",
    "контакты",
    "новости",
    "о компании",
    "компания партнёр",
    "компания партнер",
}


def _is_valid_product_name(name: str) -> bool:
    cleaned = _clean_text(name)
    if len(cleaned) < 5:
        return False
    if cleaned.lower() in _NAV_TITLES:
        return False
    return True


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def _parse_price(text: str | None) -> float | None:
    raw = _clean_text(text)
    if not raw:
        return None
    match = _PRICE_RE.search(raw.replace("₽", "").replace("руб", ""))
    if not match:
        return None
    number = match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return None
    if value <= 0 or value > 50_000_000:
        return None
    return value


def _normalize_availability(raw: str | None) -> str | None:
    text = _clean_text(raw).lower()
    if not text:
        return None
    if any(marker in text for marker in ("instock", "in stock", "в наличии", "есть")):
        return "in_stock"
    if any(marker in text for marker in ("outofstock", "out of stock", "нет в наличии", "отсутств")):
        return "out_of_stock"
    if any(marker in text for marker in ("preorder", "pre-order", "под заказ", "ожида")):
        return "preorder"
    return text[:120] or None


def _availability_from_node(node) -> str | None:
    if node is None:
        return None
    if node.name == "link":
        return _normalize_availability(node.get("href", ""))
    if node.name == "meta":
        return _normalize_availability(node.get("content", ""))
    return _normalize_availability(node.get_text(" ", strip=True))


def _extract_name_price_availability(block) -> tuple[str, float | None, str | None]:
    name = ""
    price: float | None = None
    availability: str | None = None

    name_node = block.select_one('[itemprop="name"]')
    if name_node:
        name = _clean_text(name_node.get_text(" ", strip=True) or name_node.get("content", ""))
    if not name:
        title = block.select_one("h1, h2, h3, .product-title, .product-name, .title")
        if title:
            name = _clean_text(title.get_text(" ", strip=True))

    for selector in (
        '[itemprop="price"]',
        '[itemprop="lowPrice"]',
        '[itemprop="highPrice"]',
        ".price, .product-price, .catalog-item-price",
    ):
        node = block.select_one(selector)
        if not node:
            continue
        price = _parse_price(node.get("content") or node.get_text(" ", strip=True))
        if price is not None:
            break

    avail_node = block.select_one('[itemprop="availability"]')
    availability = _availability_from_node(avail_node)
    if availability is None:
        stock_node = block.select_one(".in-stock, .out-of-stock, .availability, .stock")
        availability = _availability_from_node(stock_node)

    return name, price, availability


def _product_blocks(soup) -> list:
    selectors = [
        '[itemtype*="Product"]',
        '[itemprop="itemListElement"]',
        ".preview_product",
        ".product-item",
        ".catalog-item",
        ".goods-item",
        ".product-card",
        "article.product",
    ]
    blocks: list = []
    seen_ids: set[int] = set()
    for selector in selectors:
        for block in soup.select(selector):
            block_id = id(block)
            if block_id in seen_ids:
                continue
            seen_ids.add(block_id)
            blocks.append(block)
    return blocks


def _looks_like_product_page(url: str) -> bool:
    path = urlparse(url).path.lower()
    segments = [segment for segment in path.split("/") if segment]
    if _PRODUCT_LINK_RE.search(path):
        return len(segments) >= 3
    return False


def extract_products(html_url: str, soup, *, site_label: str) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for block in _product_blocks(soup):
        name, price, availability = _extract_name_price_availability(block)
        if not _is_valid_product_name(name):
            continue

        link = block.select_one('a[href][itemprop="name"], a[href].product-title, a[href]')
        product_url = html_url
        if link and link.get("href"):
            product_url = urljoin(html_url, link["href"].split("#")[0])

        if price is None and availability is None and not _looks_like_product_page(product_url):
            continue

        key = (name.lower(), product_url)
        if key in seen:
            continue
        seen.add(key)

        products.append(
            {
                "name": name,
                "price": price,
                "availability": availability,
                "url": product_url,
                "source_page": html_url,
                "site": site_label,
            }
        )

    if products:
        return products

    if not _looks_like_product_page(html_url):
        return []

    # Страница карточки товара: один product на страницу
    name, price, availability = _extract_name_price_availability(soup)
    if not _is_valid_product_name(name):
        return []

    products.append(
        {
            "name": name,
            "price": price,
            "availability": availability,
            "url": html_url,
            "source_page": html_url,
            "site": site_label,
        }
    )

    return products


def _should_enqueue(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    path = parsed.path.lower()
    if _SKIP_LINK_RE.search(path):
        return False
    if "/products/" in path:
        return True
    if _PRODUCT_LINK_RE.search(path):
        return True
    # Каталог верхнего уровня — тоже обходим
    if path in {"/", "/products/", "/products", "/catalog/", "/catalog", "/prays-list"}:
        return True
    return False


async def run_crawler(
    start_urls: list[str],
    *,
    max_requests: int,
    output_file: Path,
) -> None:
    seen_products: set[tuple[str, str, str]] = set()

    crawler = BeautifulSoupCrawler(
        max_requests_per_crawl=max_requests,
        max_request_retries=2,
        request_handler_timeout=timedelta(seconds=45),
    )

    @crawler.router.default_handler
    async def request_handler(context: BeautifulSoupCrawlingContext) -> None:
        url = context.request.url
        site_label = competitor_label_for_url(url) or urlparse(url).netloc
        context.log.info("Processing %s (%s)", url, site_label)

        extracted = extract_products(url, context.soup, site_label=site_label)
        for item in extracted:
            dedupe_key = (item["site"], item["name"].lower(), item["url"])
            if dedupe_key in seen_products:
                continue
            seen_products.add(dedupe_key)
            await context.push_data(item)

        if extracted:
            context.log.info("Extracted %s products from %s", len(extracted), url)

        def transform_request(options: dict[str, Any]) -> str:
            if _should_enqueue(options["url"]):
                return "unchanged"
            return "skip"

        await context.enqueue_links(
            strategy="same-domain",
            exclude=[_SKIP_LINK_RE],
            transform_request_function=transform_request,
        )

    await crawler.run(start_urls)

    dataset = await Dataset.open()
    page = await dataset.get_data()
    items = list(page.items)

    payload = {
        "total": len(items),
        "products": items,
    }
    output_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Saved {len(items)} products to {output_file.resolve()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl competitor stores and save products to JSON",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=200,
        help="Maximum pages to crawl (default: 200)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_FILE,
        help="Output JSON file (default: products.json)",
    )
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        help="Additional start URL (can be repeated)",
    )
    parser.add_argument(
        "--only-urls",
        action="store_true",
        help="Crawl only URLs from --url (skip default competitor list)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.only_urls:
        if not args.urls:
            raise SystemExit("Pass at least one --url with --only-urls")
        start_urls = list(dict.fromkeys(args.urls))
    else:
        start_urls = build_start_urls()
        if args.urls:
            start_urls.extend(args.urls)
        start_urls = list(dict.fromkeys(start_urls))

    print(f"Start URLs: {len(start_urls)}")
    for url in start_urls:
        print(f"  - {url}")

    await run_crawler(
        start_urls,
        max_requests=args.max_requests,
        output_file=args.output,
    )


if __name__ == "__main__":
    asyncio.run(main())
