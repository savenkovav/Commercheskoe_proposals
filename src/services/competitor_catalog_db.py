"""SQLite-хранилище проиндексированных каталогов товаров сайтов конкурентов."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.config import COMPETITOR_CATALOG_DB_PATH, COMPETITOR_PRODUCTS_PATH
from src.services.competitor_catalog_service import CompetitorCatalogProduct
from src.services.data_loader import normalize_name

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


def competitor_product_url_key(url: str) -> str:
    normalized = url.rstrip("/").split("#")[0]
    parsed = urlparse(normalized)
    query = parse_qs(parsed.query)
    if query.get("page") or parsed.path.lower().endswith("index.php"):
        return normalized
    return normalized.split("?")[0]


def product_dedup_key(product: CompetitorCatalogProduct) -> str:
    if product.url:
        return competitor_product_url_key(product.url)
    if product.articul:
        return f"articul:{product.articul.strip()}"
    return f"name:{normalize_name(product.name)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_product(row: sqlite3.Row) -> CompetitorCatalogProduct:
    return CompetitorCatalogProduct(
        domain=row["domain"],
        site_label=row["site_label"] or "",
        name=row["name"],
        price=row["price"],
        url=row["url"],
        articul=row["articul"],
        price_label=row["price_label"],
        details=row["details"],
        wholesale_price=row["wholesale_price"],
        image_url=row["image_url"],
    )


class CompetitorCatalogDatabase:
    def __init__(self, db_path: Path = COMPETITOR_CATALOG_DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate_json_if_needed()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            with self._connection() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS competitor_sites (
                        domain TEXT PRIMARY KEY,
                        label TEXT NOT NULL DEFAULT '',
                        product_count INTEGER NOT NULL DEFAULT 0,
                        last_indexed_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS competitor_products (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        domain TEXT NOT NULL,
                        site_label TEXT NOT NULL DEFAULT '',
                        name TEXT NOT NULL,
                        name_key TEXT NOT NULL,
                        url TEXT,
                        url_key TEXT NOT NULL,
                        articul TEXT,
                        price REAL,
                        price_label TEXT,
                        details TEXT,
                        wholesale_price REAL,
                        image_url TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (domain) REFERENCES competitor_sites(domain) ON DELETE CASCADE,
                        UNIQUE(domain, url_key)
                    );

                    CREATE INDEX IF NOT EXISTS idx_competitor_products_domain
                        ON competitor_products(domain);
                    CREATE INDEX IF NOT EXISTS idx_competitor_products_name_key
                        ON competitor_products(domain, name_key);
                    CREATE INDEX IF NOT EXISTS idx_competitor_products_articul
                        ON competitor_products(domain, articul);

                    CREATE TABLE IF NOT EXISTS competitor_indexed_pages (
                        url TEXT PRIMARY KEY,
                        domain TEXT NOT NULL,
                        label TEXT NOT NULL DEFAULT '',
                        products_count INTEGER NOT NULL DEFAULT 0,
                        indexed_at TEXT NOT NULL,
                        FOREIGN KEY (domain) REFERENCES competitor_sites(domain) ON DELETE CASCADE
                    );
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
                    ("version", str(_SCHEMA_VERSION)),
                )

    def _migrate_json_if_needed(self) -> None:
        if not COMPETITOR_PRODUCTS_PATH.exists():
            return
        with self._lock:
            with self._connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM competitor_products").fetchone()[0]
                if count > 0:
                    return
        try:
            payload = json.loads(COMPETITOR_PRODUCTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read legacy competitor_products.json")
            return

        rows = payload.get("products", [])
        if not isinstance(rows, list) or not rows:
            return

        products: list[CompetitorCatalogProduct] = []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("name", "")).strip():
                continue
            products.append(
                CompetitorCatalogProduct(
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
            )

        by_domain: dict[str, list[CompetitorCatalogProduct]] = {}
        labels: dict[str, str] = {}
        for product in products:
            domain = product.domain.lower().removeprefix("www.")
            if not domain:
                continue
            by_domain.setdefault(domain, []).append(product)
            if product.site_label:
                labels[domain] = product.site_label

        imported = 0
        for domain, domain_products in by_domain.items():
            imported += self.replace_site_products(
                domain,
                domain_products,
                site_label=labels.get(domain, domain),
            )

        page_rows = payload.get("pages", [])
        if isinstance(page_rows, list):
            for page in page_rows:
                if not isinstance(page, dict) or not page.get("url"):
                    continue
                self.record_indexed_page(
                    str(page["url"]),
                    domain=str(page.get("domain", "")),
                    site_label=str(page.get("label", "")),
                    products_count=int(page.get("products_count") or 0),
                )

        logger.info(
            "Migrated %s products from %s into SQLite %s",
            imported,
            COMPETITOR_PRODUCTS_PATH,
            self.db_path,
        )

    def _upsert_site(self, conn: sqlite3.Connection, domain: str, *, label: str) -> None:
        normalized = domain.lower().removeprefix("www.")
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO competitor_sites(domain, label, product_count, last_indexed_at, created_at, updated_at)
            VALUES (?, ?, 0, NULL, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                label = excluded.label,
                updated_at = excluded.updated_at
            """,
            (normalized, label or normalized, now, now),
        )

    def _refresh_site_stats(self, conn: sqlite3.Connection, domain: str) -> None:
        normalized = domain.lower().removeprefix("www.")
        count = conn.execute(
            "SELECT COUNT(*) FROM competitor_products WHERE domain = ?",
            (normalized,),
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE competitor_sites
            SET product_count = ?, last_indexed_at = ?, updated_at = ?
            WHERE domain = ?
            """,
            (count, _utc_now(), _utc_now(), normalized),
        )

    def replace_site_products(
        self,
        domain: str,
        products: list[CompetitorCatalogProduct],
        *,
        site_label: str = "",
    ) -> int:
        normalized = domain.lower().removeprefix("www.")
        label = site_label or normalized
        now = _utc_now()

        with self._lock:
            with self._connection() as conn:
                self._upsert_site(conn, normalized, label=label)
                conn.execute(
                    "DELETE FROM competitor_products WHERE domain = ?",
                    (normalized,),
                )
                inserted = 0
                for product in products:
                    if not product.name.strip():
                        continue
                    url_key = product_dedup_key(product)
                    conn.execute(
                        """
                        INSERT INTO competitor_products (
                            domain, site_label, name, name_key, url, url_key,
                            articul, price, price_label, details,
                            wholesale_price, image_url, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            normalized,
                            product.site_label or label,
                            product.name[:300],
                            normalize_name(product.name),
                            product.url,
                            url_key,
                            product.articul,
                            product.price,
                            product.price_label,
                            product.details,
                            product.wholesale_price,
                            product.image_url,
                            now,
                            now,
                        ),
                    )
                    inserted += 1
                self._refresh_site_stats(conn, normalized)
                return inserted

    def merge_products(
        self,
        products: list[CompetitorCatalogProduct],
        *,
        domain: str,
        site_label: str = "",
    ) -> tuple[int, int]:
        normalized = domain.lower().removeprefix("www.")
        label = site_label or normalized
        now = _utc_now()
        added = 0
        updated = 0

        with self._lock:
            with self._connection() as conn:
                self._upsert_site(conn, normalized, label=label)
                for product in products:
                    if not product.name.strip():
                        continue
                    url_key = product_dedup_key(product)
                    existing = conn.execute(
                        """
                        SELECT id, domain, site_label, name, name_key, url, url_key,
                               articul, price, price_label, details, wholesale_price, image_url
                        FROM competitor_products
                        WHERE domain = ? AND url_key = ?
                        """,
                        (normalized, url_key),
                    ).fetchone()

                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO competitor_products (
                                domain, site_label, name, name_key, url, url_key,
                                articul, price, price_label, details,
                                wholesale_price, image_url, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                normalized,
                                product.site_label or label,
                                product.name[:300],
                                normalize_name(product.name),
                                product.url,
                                url_key,
                                product.articul,
                                product.price,
                                product.price_label,
                                product.details,
                                product.wholesale_price,
                                product.image_url,
                                now,
                                now,
                            ),
                        )
                        added += 1
                        continue

                    merged = CompetitorCatalogProduct(
                        domain=normalized,
                        site_label=product.site_label or existing["site_label"] or label,
                        name=product.name or existing["name"],
                        price=product.price if product.price is not None else existing["price"],
                        url=product.url or existing["url"],
                        articul=product.articul or existing["articul"],
                        price_label=product.price_label or existing["price_label"],
                        details=product.details or existing["details"],
                        wholesale_price=(
                            product.wholesale_price
                            if product.wholesale_price is not None
                            else existing["wholesale_price"]
                        ),
                        image_url=product.image_url or existing["image_url"],
                    )
                    conn.execute(
                        """
                        UPDATE competitor_products SET
                            site_label = ?, name = ?, name_key = ?, url = ?,
                            articul = ?, price = ?, price_label = ?, details = ?,
                            wholesale_price = ?, image_url = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            merged.site_label,
                            merged.name[:300],
                            normalize_name(merged.name),
                            merged.url,
                            merged.articul,
                            merged.price,
                            merged.price_label,
                            merged.details,
                            merged.wholesale_price,
                            merged.image_url,
                            now,
                            existing["id"],
                        ),
                    )
                    updated += 1

                self._refresh_site_stats(conn, normalized)
                return added, updated

    def products_for_domain(self, domain: str) -> list[CompetitorCatalogProduct]:
        normalized = domain.lower().removeprefix("www.")
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT domain, site_label, name, url, articul, price,
                           price_label, details, wholesale_price, image_url
                    FROM competitor_products
                    WHERE domain = ?
                    ORDER BY name COLLATE NOCASE
                    """,
                    (normalized,),
                ).fetchall()
                return [_row_to_product(row) for row in rows]

    def iter_products(self) -> list[CompetitorCatalogProduct]:
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT domain, site_label, name, url, articul, price,
                           price_label, details, wholesale_price, image_url
                    FROM competitor_products
                    ORDER BY domain, name COLLATE NOCASE
                    """
                ).fetchall()
                return [_row_to_product(row) for row in rows]

    def has_site(self, domain: str) -> bool:
        normalized = domain.lower().removeprefix("www.")
        with self._lock:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM competitor_products WHERE domain = ? LIMIT 1",
                    (normalized,),
                ).fetchone()
                return row is not None

    def site_domains(self) -> set[str]:
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT domain FROM competitor_products WHERE domain != ''"
                ).fetchall()
                return {row[0] for row in rows}

    def remove_domain(self, domain: str) -> None:
        normalized = domain.lower().removeprefix("www.")
        with self._lock:
            with self._connection() as conn:
                conn.execute("DELETE FROM competitor_indexed_pages WHERE domain = ?", (normalized,))
                conn.execute("DELETE FROM competitor_products WHERE domain = ?", (normalized,))
                conn.execute("DELETE FROM competitor_sites WHERE domain = ?", (normalized,))

    def record_indexed_page(
        self,
        page_url: str,
        *,
        domain: str,
        site_label: str,
        products_count: int,
    ) -> None:
        normalized = domain.lower().removeprefix("www.")
        normalized_url = page_url.strip().split("#")[0]
        now = _utc_now()
        with self._lock:
            with self._connection() as conn:
                self._upsert_site(conn, normalized, label=site_label or normalized)
                conn.execute(
                    """
                    INSERT INTO competitor_indexed_pages(url, domain, label, products_count, indexed_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        domain = excluded.domain,
                        label = excluded.label,
                        products_count = excluded.products_count,
                        indexed_at = excluded.indexed_at
                    """,
                    (normalized_url, normalized, site_label or normalized, products_count, now),
                )

    def stats(self) -> dict[str, int | dict[str, int]]:
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT domain, COUNT(*) AS cnt,
                           SUM(CASE WHEN image_url IS NOT NULL AND image_url != '' THEN 1 ELSE 0 END) AS images
                    FROM competitor_products
                    GROUP BY domain
                    """
                ).fetchall()
                pages_count = conn.execute(
                    "SELECT COUNT(*) FROM competitor_indexed_pages"
                ).fetchone()[0]

        by_domain: dict[str, int] = {}
        images_by_domain: dict[str, int] = {}
        total = 0
        for row in rows:
            domain = row["domain"]
            count = int(row["cnt"])
            by_domain[domain] = count
            images_by_domain[domain] = int(row["images"] or 0)
            total += count

        return {
            "products": total,
            "sites": len(by_domain),
            "pages": pages_count,
            "by_domain": by_domain,
            "images_by_domain": images_by_domain,
        }

    def list_indexed_pages(self) -> list[dict[str, str | int]]:
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT url, domain, label, products_count, indexed_at
                    FROM competitor_indexed_pages
                    ORDER BY indexed_at DESC
                    """
                ).fetchall()
                return [
                    {
                        "url": row["url"],
                        "domain": row["domain"],
                        "label": row["label"],
                        "products_count": row["products_count"],
                        "indexed_at": row["indexed_at"],
                    }
                    for row in rows
                ]

    def list_sites(self) -> list[dict[str, str | int | None]]:
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT domain, label, product_count, last_indexed_at, updated_at
                    FROM competitor_sites
                    ORDER BY domain
                    """
                ).fetchall()
                return [dict(row) for row in rows]

    def catalog_db_report(self, *, domain: str | None = None) -> dict[str, object]:
        normalized = domain.lower().removeprefix("www.").strip() if domain else ""
        with self._lock:
            with self._connection() as conn:
                version_row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'version'"
                ).fetchone()
                schema_version = int(version_row["value"]) if version_row else _SCHEMA_VERSION

                domain_filter = ""
                params: tuple[str, ...] = ()
                if normalized:
                    domain_filter = "WHERE COALESCE(s.domain, d.domain) = ?"
                    params = (normalized,)

                rows = conn.execute(
                    f"""
                    WITH domain_stats AS (
                        SELECT
                            domain,
                            COUNT(*) AS product_count,
                            SUM(
                                CASE
                                    WHEN image_url IS NOT NULL AND image_url != '' THEN 1
                                    ELSE 0
                                END
                            ) AS products_with_images,
                            MAX(updated_at) AS last_product_updated_at
                        FROM competitor_products
                        GROUP BY domain
                    ),
                    page_stats AS (
                        SELECT
                            domain,
                            COUNT(*) AS indexed_pages,
                            MAX(indexed_at) AS last_page_indexed_at
                        FROM competitor_indexed_pages
                        GROUP BY domain
                    )
                    SELECT
                        COALESCE(s.domain, d.domain) AS domain,
                        COALESCE(NULLIF(s.label, ''), d.domain) AS label,
                        COALESCE(d.product_count, 0) AS product_count,
                        COALESCE(d.products_with_images, 0) AS products_with_images,
                        COALESCE(p.indexed_pages, 0) AS indexed_pages,
                        s.last_indexed_at,
                        d.last_product_updated_at,
                        p.last_page_indexed_at,
                        s.created_at,
                        s.updated_at
                    FROM domain_stats d
                    LEFT JOIN competitor_sites s ON s.domain = d.domain
                    LEFT JOIN page_stats p ON p.domain = d.domain
                    {domain_filter}
                    ORDER BY domain
                    """,
                    params,
                ).fetchall()

                totals = conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM competitor_products) AS products,
                        (SELECT COUNT(DISTINCT domain) FROM competitor_products) AS sites,
                        (SELECT COUNT(*) FROM competitor_indexed_pages) AS pages
                    """
                ).fetchone()

        sites: list[dict[str, object]] = []
        for row in rows:
            last_indexed_at = row["last_indexed_at"] or row["last_page_indexed_at"] or row["last_product_updated_at"]
            sites.append(
                {
                    "domain": row["domain"],
                    "label": row["label"],
                    "product_count": int(row["product_count"] or 0),
                    "products_with_images": int(row["products_with_images"] or 0),
                    "indexed_pages": int(row["indexed_pages"] or 0),
                    "last_indexed_at": last_indexed_at,
                    "last_page_indexed_at": row["last_page_indexed_at"],
                    "last_product_updated_at": row["last_product_updated_at"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )

        return {
            "db_path": str(self.db_path),
            "schema_version": schema_version,
            "totals": {
                "products": int(totals["products"] or 0),
                "sites": int(totals["sites"] or 0),
                "indexed_pages": int(totals["pages"] or 0),
            },
            "sites": sites,
        }


_db: CompetitorCatalogDatabase | None = None


def get_competitor_catalog_db() -> CompetitorCatalogDatabase:
    global _db
    if _db is None:
        _db = CompetitorCatalogDatabase()
    return _db
