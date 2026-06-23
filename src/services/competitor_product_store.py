from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz, process

from src.config import (
    COMPETITOR_PRODUCTS_JSON_EXPORT,
    COMPETITOR_PRODUCTS_PATH,
    COMPETITOR_SEARCH_FALLBACK_THRESHOLD,
)
from src.services.competitor_catalog_db import (
    CompetitorCatalogDatabase,
    competitor_product_url_key,
    get_competitor_catalog_db,
)
from src.services.competitor_catalog_service import CompetitorCatalogProduct
from src.services.data_loader import normalize_name

logger = logging.getLogger(__name__)

__all__ = [
    "CompetitorProductStore",
    "competitor_product_url_key",
    "get_competitor_product_store",
    "merge_competitor_product_fields",
]


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
        description=incoming.description or existing.description,
    )
    return merged, merged != existing


class CompetitorProductStore:
    """Фасад над SQLite-каталогом с fuzzy-поиском по товарам."""

    def __init__(
        self,
        db: CompetitorCatalogDatabase | None = None,
        *,
        json_path: Path = COMPETITOR_PRODUCTS_PATH,
    ) -> None:
        self._db = db or get_competitor_catalog_db()
        self.path = json_path
        self._products: list[CompetitorCatalogProduct] = []
        self._loaded = False

    def reload(self) -> None:
        self._loaded = False
        self._products = []
        self.ensure_loaded()

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._products = self._db.iter_products()
        self._loaded = True

    def _invalidate_cache(self) -> None:
        self._loaded = False
        self._products = []

    def save(self) -> None:
        """Опциональный JSON-снимок для резервного копирования и отладки."""
        if not COMPETITOR_PRODUCTS_JSON_EXPORT:
            return
        products = self._db.iter_products()
        sites = self._db.list_sites()
        pages_payload = self._db.list_indexed_pages()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "storage": "sqlite",
            "db_path": str(self._db.db_path),
            "sites": sites,
            "pages": pages_payload,
            "products": [asdict(product) for product in products],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def has_site(self, domain: str) -> bool:
        return self._db.has_site(domain)

    def site_domains(self) -> set[str]:
        return self._db.site_domains()

    def products_for_domain(self, domain: str) -> list[CompetitorCatalogProduct]:
        return self._db.products_for_domain(domain)

    def replace_site_products(
        self,
        domain: str,
        products: list[CompetitorCatalogProduct],
        *,
        site_label: str = "",
    ) -> int:
        inserted = self._db.replace_site_products(domain, products, site_label=site_label)
        self._invalidate_cache()
        if COMPETITOR_PRODUCTS_JSON_EXPORT:
            self.save()
        return inserted

    def merge_products(
        self,
        products: list[CompetitorCatalogProduct],
        *,
        domain: str,
        site_label: str = "",
    ) -> tuple[int, int]:
        added, updated = self._db.merge_products(
            products,
            domain=domain,
            site_label=site_label,
        )
        self._invalidate_cache()
        if added or updated:
            if COMPETITOR_PRODUCTS_JSON_EXPORT:
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
        self._db.record_indexed_page(
            page_url,
            domain=domain,
            site_label=site_label,
            products_count=products_count,
        )
        if COMPETITOR_PRODUCTS_JSON_EXPORT:
            self.save()

    def remove_domain(self, domain: str) -> None:
        self._db.remove_domain(domain)
        self._invalidate_cache()
        if COMPETITOR_PRODUCTS_JSON_EXPORT:
            self.save()

    def iter_products(self) -> list[CompetitorCatalogProduct]:
        return self._db.iter_products()

    def list_domains(self) -> list[str]:
        self.ensure_loaded()
        return sorted({product.domain for product in self._products if product.domain})

    def search_products(
        self,
        query: str,
        *,
        limit: int = 24,
        domain: str | None = None,
    ) -> list[CompetitorCatalogProduct]:
        """Fuzzy search по проиндексированным карточкам конкурентов."""
        self.ensure_loaded()
        normalized_query = normalize_name(query.strip())
        if not normalized_query or not self._products:
            return []

        pool = self._products
        if domain:
            normalized_domain = domain.lower().removeprefix("www.")
            pool = [product for product in pool if product.domain == normalized_domain]
            if not pool:
                return []

        query_words = [word for word in normalized_query.split() if len(word) >= 3]
        if query_words and len(pool) > 400:
            filtered: list[CompetitorCatalogProduct] = []
            for product in pool:
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
        return self._db.stats()

    def list_sites(self) -> list[dict[str, str | int | None]]:
        return self._db.list_sites()

    def catalog_db_report(self, *, domain: str | None = None) -> dict[str, object]:
        return self._db.catalog_db_report(domain=domain)


_store: CompetitorProductStore | None = None


def get_competitor_product_store() -> CompetitorProductStore:
    global _store
    if _store is None:
        _store = CompetitorProductStore()
    return _store
