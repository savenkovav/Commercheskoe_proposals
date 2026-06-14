from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import meilisearch

from kp_search.config import MeiliSettings

_SOURCE_FILTERS = {
    "catalog": 'source = "catalog"',
    "registry": 'source = "registry"',
    "price_list": 'source = "price_list"',
    "price": 'source = "price_list"',
}


@dataclass(frozen=True)
class MeiliSearchHit:
    document_id: str
    name: str
    source: str
    source_index: int
    score: float
    detail: str = ""


class ProductSearchEngine:
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

    def search(
        self,
        query: str,
        *,
        source: str | None = None,
        limit: int | None = None,
    ) -> list[MeiliSearchHit]:
        if not self.enabled:
            return []

        text = query.strip()
        if not text:
            return []

        index = self._client_or_raise().index(self.settings.index_name)
        params: dict[str, Any] = {
            "limit": limit or self.settings.search_limit,
            "showRankingScore": True,
            "attributesToRetrieve": [
                "id",
                "name",
                "source",
                "source_index",
                "detail",
            ],
        }
        source_filter = _SOURCE_FILTERS.get(source or "")
        if source_filter:
            params["filter"] = source_filter

        response = index.search(text, params)
        hits: list[MeiliSearchHit] = []
        for item in response.get("hits", []):
            source_index = item.get("source_index")
            if source_index is None:
                continue
            ranking_score = float(item.get("_rankingScore") or 0)
            hits.append(
                MeiliSearchHit(
                    document_id=str(item.get("id") or ""),
                    name=str(item.get("name") or "").strip(),
                    source=str(item.get("source") or ""),
                    source_index=int(source_index),
                    score=min(round(ranking_score * 100, 2), 100.0),
                    detail=str(item.get("detail") or ""),
                )
            )
        return hits
