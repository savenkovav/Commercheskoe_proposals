from __future__ import annotations

import logging
from typing import Any

import meilisearch
from meilisearch.errors import MeilisearchApiError

from kp_search.config import MeiliSettings
from kp_search.documents import build_documents

logger = logging.getLogger(__name__)

_INDEX_SETTINGS: dict[str, Any] = {
    "searchableAttributes": ["name", "supplier", "code", "detail"],
    "filterableAttributes": ["source", "entry_type", "supplier"],
    "sortableAttributes": ["name"],
    "displayedAttributes": [
        "id",
        "name",
        "source",
        "source_index",
        "cost",
        "price",
        "quantity",
        "unit",
        "supplier",
        "code",
        "sheet",
        "detail",
        "entry_type",
    ],
    "typoTolerance": {
        "enabled": True,
        "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 8},
    },
}


class ProductIndexer:
    def __init__(self, settings: MeiliSettings | None = None) -> None:
        self.settings = settings or MeiliSettings.from_env()
        self._client: meilisearch.Client | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def _client_or_raise(self) -> meilisearch.Client:
        if self._client is None:
            self._client = meilisearch.Client(
                self.settings.host,
                self.settings.api_key or None,
            )
        return self._client

    def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "available": False}
        try:
            status = self._client_or_raise().health()
            return {
                "enabled": True,
                "available": status.get("status") == "available",
                "host": self.settings.host,
                "index": self.settings.index_name,
            }
        except Exception as exc:
            logger.warning("Meilisearch health check failed: %s", exc)
            return {
                "enabled": True,
                "available": False,
                "host": self.settings.host,
                "error": str(exc),
            }

    def ensure_index(self) -> meilisearch.index.Index:
        client = self._client_or_raise()
        try:
            index = client.get_index(self.settings.index_name)
        except MeilisearchApiError:
            task = client.create_index(self.settings.index_name, {"primaryKey": "id"})
            client.wait_for_task(task.task_uid)
            index = client.get_index(self.settings.index_name)

        task = index.update_settings(_INDEX_SETTINGS)
        client.wait_for_task(task.task_uid)
        return index

    def sync_all(
        self,
        catalog: list,
        registry: list,
        price_lists: list,
        *,
        batch_size: int = 1000,
    ) -> dict[str, int]:
        if not self.enabled:
            return {"documents": 0, "skipped": True}

        documents = build_documents(catalog, registry, price_lists)
        index = self.ensure_index()
        client = self._client_or_raise()

        delete_task = index.delete_all_documents()
        client.wait_for_task(delete_task.task_uid)

        indexed = 0
        for start in range(0, len(documents), batch_size):
            chunk = documents[start : start + batch_size]
            if not chunk:
                continue
            task = index.add_documents(chunk)
            client.wait_for_task(task.task_uid)
            indexed += len(chunk)

        logger.info("Meilisearch index %s synced: %s documents", self.settings.index_name, indexed)
        return {
            "documents": indexed,
            "catalog": len(catalog),
            "registry": len(registry),
            "price_lists": len(price_lists),
        }
