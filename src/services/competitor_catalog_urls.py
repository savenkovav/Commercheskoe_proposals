from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from src.config import COMPETITOR_CATALOG_URLS_PATH

logger = logging.getLogger(__name__)


class CompetitorCatalogUrlRegistry:
    """Реестр страниц каталогов конкурентов для индексации."""

    def __init__(self, path: Path = COMPETITOR_CATALOG_URLS_PATH) -> None:
        self.path = path
        self._pages: list[dict[str, str]] = []
        self._loaded = False

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = payload.get("pages", [])
            self._pages = [row for row in rows if isinstance(row, dict) and row.get("url")]
        except Exception:
            logger.exception("Failed to load competitor catalog URL registry")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "pages": self._pages,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _domain_from_url(url: str) -> str:
        return urlparse(url).netloc.lower().removeprefix("www.")

    def add_page(
        self,
        url: str,
        *,
        domain: str = "",
        label: str = "",
        source: str = "manual",
    ) -> bool:
        self.ensure_loaded()
        normalized = url.strip().split("#")[0]
        if not normalized:
            return False
        page_domain = domain or self._domain_from_url(normalized)
        for row in self._pages:
            if row.get("url") == normalized:
                row["domain"] = page_domain
                if label:
                    row["label"] = label
                row["source"] = source
                self.save()
                return False
        self._pages.append(
            {
                "url": normalized,
                "domain": page_domain,
                "label": label or page_domain,
                "source": source,
                "added_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.save()
        return True

    def remove_domain(self, domain: str) -> None:
        self.ensure_loaded()
        normalized_domain = domain.lower().removeprefix("www.")
        self._pages = [
            row for row in self._pages if row.get("domain", "").lower() != normalized_domain
        ]
        self.save()

    def urls_for_domain(self, domain: str) -> list[str]:
        self.ensure_loaded()
        normalized_domain = domain.lower().removeprefix("www.")
        return [
            str(row["url"])
            for row in self._pages
            if str(row.get("domain", "")).lower() == normalized_domain
        ]

    def all_urls(self) -> list[str]:
        self.ensure_loaded()
        return [str(row["url"]) for row in self._pages]


_registry: CompetitorCatalogUrlRegistry | None = None


def get_competitor_catalog_url_registry() -> CompetitorCatalogUrlRegistry:
    global _registry
    if _registry is None:
        _registry = CompetitorCatalogUrlRegistry()
    return _registry
