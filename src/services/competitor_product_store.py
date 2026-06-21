from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rapidfuzz import fuzz, process

from src.config import COMPETITOR_PRODUCTS_PATH, COMPETITOR_SEARCH_FALLBACK_THRESHOLD
from src.services.competitor_catalog_service import CompetitorCatalogProduct
from src.services.data_loader import normalize_name

logger = logging.getLogger(__name__)


def competitor_product_url_key(url: str) -> str:
    normalized = url.rstrip("/").split("#")[0]
    parsed = urlparse(normalized)
    query = parse_qs(parsed.query)
    if query.get("page") or parsed.path.lower().endswith("index.php"):
        return normalized
    return normalized.split("?")[0]


def merge_competitor_product_fields(
    existing: CompetitorCatalogProduct,
    incoming: CompetitorCatalogProduct,
) -> tuple[CompetitorCatalogProduct, bool]:
    merged = CompetitorCatalogProduct(
        domain=existing.domain,
        site_label=incoming.site_label or existing.site_label,
        name=incoming.name or existing.name,
        price=incoming.price if incoming.price is not None else existing.price,
        url=incoming.url or existing.url,
        articul=incoming.articul or existing.articul,
        price_label=incoming.price_label or existing.price_label,
        details=incoming.details or existing.details,
        wholesale_price=(
            incoming.wholesale_price
            if incoming.wholesale_price is not None
            else existing.wholesale_price
        ),
        image_url=incoming.image_url or existing.image_url,
    )
    return merged, merged != existing


class CompetitorProductStore:
    def __init__(self, path: Path = COMPETITOR_PRODUCTS_PATH) -> None:
        self.path = path
        self._products: list[CompetitorCatalogProduct] = []
        self._pages: list[dict[str, str | int]] = []
        self._site_labels: dict[str, str] = {}
        self._loaded = False
        self._file_mtime: float = 0.0

    def _disk_mtime(self) -> float:
        if not self.path.exists():
            return 0.0
        return self.path.stat().st_mtime

    def reload(self) -> None:
        self._loaded = False
        self._products = []
        self._pages = []
        self._site_labels = {}
        self._file_mtime = 0.0
        self.ensure_loaded()

    def ensure_loaded(self) -> None:
        mtime = self._disk_mtime()
        if self._loaded and mtime == self._file_mtime:
            return
        self._loaded = True
        self._file_mtime = mtime
        self._products = []
        self._pages = []
        self._site_labels = {}
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = payload.get("products", [])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                product = CompetitorCatalogProduct(
                    domain=str(row.get("domain", "")),
                    site_label=str(row.get("site_label", "")),
                    name=str(row.get("name", "")),
                    price=row.get("price"),
                    url=row.get("url") or None,
                    articul=row.get("articul") or None,
                    price_label=row.get("price_label") or None,
                    details=row.get("details") or None,
                    wholesale_price=row.get("wholesale_price"),
                    image_url=row.get("image_url") or None,
                )
                if product.name.strip():
                    self._products.append(product)
                    if product.domain and product.site_label:
                        self._site_labels[product.domain] = product.site_label
            page_rows = payload.get("pages", [])
            if isinstance(page_rows, list):
                self._pages = [row for row in page_rows if isinstance(row, dict)]
        except Exception:
            logger.exception("Failed to load competitor product store")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pages": self._pages,
            "products": [asdict(product) for product in self._products],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._file_mtime = self._disk_mtime()

    def has_site(self, domain: str) -> bool:
        self.ensure_loaded()
        return any(product.domain == domain for product in self._products)

    def site_domains(self) -> set[str]:
        self.ensure_loaded()
        return {product.domain for product in self._products if product.domain}

    def products_for_domain(self, domain: str) -> list[CompetitorCatalogProduct]:
        self.ensure_loaded()
        normalized = domain.lower().removeprefix("www.")
        return [
            product
            for product in self._products
            if product.domain.lower().removeprefix("www.") == normalized
        ]

    def replace_site_products(
        self,
        domain: str,
        products: list[CompetitorCatalogProduct],
        *,
        site_label: str = "",
    ) -> int:
        self.ensure_loaded()
        normalized = domain.lower().removeprefix("www.")
        self._products = [
            product
            for product in self._products
            if product.domain.lower().removeprefix("www.") != normalized
        ]
        added, _updated = self.merge_products(products, domain=domain, site_label=site_label)
        if site_label:
            self._site_labels[normalized] = site_label
        self.save()
        return added

    def merge_products(
        self,
        products: list[CompetitorCatalogProduct],
        *,
        domain: str,
        site_label: str = "",
    ) -> tuple[int, int]:
        self.ensure_loaded()
        normalized_domain = domain.lower().removeprefix("www.")
        label = site_label or self._site_labels.get(normalized_domain, domain)
        index_by_key: dict[str, int] = {}
        url_by_key: dict[str, int] = {}
        for index, product in enumerate(self._products):
            if product.domain.lower().removeprefix("www.") != normalized_domain:
                continue
            name_key = normalize_name(product.name)
            if name_key:
                index_by_key[name_key] = index
            if product.url:
                url_by_key[competitor_product_url_key(product.url)] = index

        added = 0
        updated = 0
        for product in products:
            if not product.name.strip():
                continue
            name_key = normalize_name(product.name)
            url_key = ""
            if product.url:
                url_key = competitor_product_url_key(product.url)

            existing_index = index_by_key.get(name_key)
            if existing_index is None and url_key:
                existing_index = url_by_key.get(url_key)

            if existing_index is not None:
                existing = self._products[existing_index]
                merged, changed = merge_competitor_product_fields(existing, product)
                if changed:
                    self._products[existing_index] = CompetitorCatalogProduct(
                        domain=normalized_domain,
                        site_label=merged.site_label or label,
                        name=merged.name,
                        price=merged.price,
                        url=merged.url,
                        articul=merged.articul,
                        price_label=merged.price_label,
                        details=merged.details,
                        wholesale_price=merged.wholesale_price,
                        image_url=merged.image_url,
                    )
                    updated += 1
                    if name_key:
                        index_by_key[name_key] = existing_index
                    if merged.url:
                        url_by_key[competitor_product_url_key(merged.url)] = existing_index
                continue

            new_product = CompetitorCatalogProduct(
                domain=normalized_domain,
                site_label=product.site_label or label,
                name=product.name,
                price=product.price,
                url=product.url,
                articul=product.articul,
                price_label=product.price_label,
                details=product.details,
                wholesale_price=product.wholesale_price,
                image_url=product.image_url,
            )
            self._products.append(new_product)
            new_index = len(self._products) - 1
            if name_key:
                index_by_key[name_key] = new_index
            if url_key:
                url_by_key[url_key] = new_index
            added += 1

        if label:
            self._site_labels[normalized_domain] = label
        if added or updated:
            self.save()
        return added, updated

    def record_indexed_page(
        self,
        page_url: str,
        *,
        domain: str,
        site_label: str,
        products_count: int,
    ) -> None:
        self.ensure_loaded()
        normalized_url = page_url.strip().split("#")[0]
        row = {
            "url": normalized_url,
            "domain": domain,
            "label": site_label,
            "products_count": products_count,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._pages = [item for item in self._pages if item.get("url") != normalized_url]
        self._pages.append(row)
        self.save()

    def remove_domain(self, domain: str) -> None:
        self.ensure_loaded()
        normalized = domain.lower().removeprefix("www.")
        self._products = [
            product
            for product in self._products
            if product.domain.lower().removeprefix("www.") != normalized
        ]
        self._pages = [
            page for page in self._pages if str(page.get("domain", "")).lower() != normalized
        ]
        self._site_labels.pop(normalized, None)
        self.save()

    def iter_products(self) -> list[CompetitorCatalogProduct]:
        self.ensure_loaded()
        return list(self._products)

    def search_products(
        self,
        query: str,
        *,
        limit: int = 24,
    ) -> list[CompetitorCatalogProduct]:
        """Fuzzy search по проиндексированным карточкам конкурентов."""
        self.ensure_loaded()
        normalized_query = normalize_name(query.strip())
        if not normalized_query or not self._products:
            return []

        query_words = [word for word in normalized_query.split() if len(word) >= 3]
        pool = self._products
        if query_words and len(self._products) > 400:
            filtered: list[CompetitorCatalogProduct] = []
            for product in self._products:
                normalized_name = normalize_name(product.name)
                if all(word in normalized_name for word in query_words):
                    filtered.append(product)
            if filtered:
                pool = filtered

        by_name: dict[str, CompetitorCatalogProduct] = {}
        for product in pool:
            key = normalize_name(product.name)
            if key and key not in by_name:
                by_name[key] = product
        if not by_name:
            return []

        min_score = max(70, COMPETITOR_SEARCH_FALLBACK_THRESHOLD - 10)
        matches = process.extract(
            normalized_query,
            by_name.keys(),
            scorer=fuzz.WRatio,
            limit=max(limit, 8),
        )
        results: list[CompetitorCatalogProduct] = []
        for name_key, score, _ in matches:
            if score < min_score:
                continue
            results.append(by_name[name_key])
        return results[:limit]

    def stats(self) -> dict[str, int | dict[str, int]]:
        self.ensure_loaded()
        by_domain: dict[str, int] = {}
        images_by_domain: dict[str, int] = {}
        for product in self._products:
            domain = product.domain.lower().removeprefix("www.")
            if domain:
                by_domain[domain] = by_domain.get(domain, 0) + 1
                if product.image_url:
                    images_by_domain[domain] = images_by_domain.get(domain, 0) + 1
        return {
            "products": len(self._products),
            "sites": len(self.site_domains()),
            "pages": len(self._pages),
            "by_domain": by_domain,
            "images_by_domain": images_by_domain,
        }


_store: CompetitorProductStore | None = None


def get_competitor_product_store() -> CompetitorProductStore:
    global _store
    if _store is None:
        _store = CompetitorProductStore()
    return _store
