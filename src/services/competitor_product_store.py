from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from src.config import COMPETITOR_PRODUCTS_PATH
from src.services.competitor_catalog_service import CompetitorCatalogProduct
from src.services.data_loader import normalize_name

logger = logging.getLogger(__name__)


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
        added = self.merge_products(products, domain=domain, site_label=site_label)
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
    ) -> int:
        self.ensure_loaded()
        normalized_domain = domain.lower().removeprefix("www.")
        existing_keys = {
            normalize_name(product.name)
            for product in self._products
            if product.domain.lower().removeprefix("www.") == normalized_domain
        }
        added = 0
        label = site_label or self._site_labels.get(normalized_domain, domain)
        for product in products:
            if not product.name.strip():
                continue
            key = normalize_name(product.name)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            self._products.append(
                CompetitorCatalogProduct(
                    domain=normalized_domain,
                    site_label=product.site_label or label,
                    name=product.name,
                    price=product.price,
                    url=product.url,
                    articul=product.articul,
                    price_label=product.price_label,
                    details=product.details,
                    wholesale_price=product.wholesale_price,
                )
            )
            added += 1
        if label:
            self._site_labels[normalized_domain] = label
        return added

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

    def stats(self) -> dict[str, int]:
        self.ensure_loaded()
        by_domain: dict[str, int] = {}
        for product in self._products:
            domain = product.domain.lower().removeprefix("www.")
            if domain:
                by_domain[domain] = by_domain.get(domain, 0) + 1
        return {
            "products": len(self._products),
            "sites": len(self.site_domains()),
            "pages": len(self._pages),
            "by_domain": by_domain,
        }


_store: CompetitorProductStore | None = None


def get_competitor_product_store() -> CompetitorProductStore:
    global _store
    if _store is None:
        _store = CompetitorProductStore()
    return _store
