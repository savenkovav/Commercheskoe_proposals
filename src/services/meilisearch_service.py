from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from src.config import (
    MEILISEARCH_AUTO_SYNC,
    MEILISEARCH_ENABLED,
)

logger = logging.getLogger(__name__)

_MEILI_ROOT = Path(__file__).resolve().parents[2] / "kp_meilisearch"
if _MEILI_ROOT.exists() and str(_MEILI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEILI_ROOT))

_indexer = None
_search_engine = None
_import_error: str | None = None

try:
    from kp_search.config import MeiliSettings
    from kp_search.indexer import ProductIndexer
    from kp_search.search import MeiliSearchHit, ProductSearchEngine
except Exception as exc:  # pragma: no cover - optional dependency
    MeiliSettings = None  # type: ignore[assignment,misc]
    ProductIndexer = None  # type: ignore[assignment,misc]
    ProductSearchEngine = None  # type: ignore[assignment,misc]
    MeiliSearchHit = None  # type: ignore[assignment,misc]
    _import_error = str(exc)


def meilisearch_available() -> bool:
    return (
        MEILISEARCH_ENABLED
        and _import_error is None
        and ProductSearchEngine is not None
    )


def get_meilisearch_settings():
    if MeiliSettings is None:
        return None
    return MeiliSettings.from_env()


def get_product_search_engine():
    global _search_engine
    if not meilisearch_available():
        return None
    if _search_engine is None:
        _search_engine = ProductSearchEngine(get_meilisearch_settings())
    return _search_engine


def get_product_indexer():
    global _indexer
    if not meilisearch_available() or ProductIndexer is None:
        return None
    if _indexer is None:
        _indexer = ProductIndexer(get_meilisearch_settings())
    return _indexer


def meilisearch_health() -> dict[str, Any]:
    if not MEILISEARCH_ENABLED:
        return {"enabled": False, "available": False}
    if _import_error:
        return {"enabled": True, "available": False, "error": _import_error}
    indexer = get_product_indexer()
    if indexer is None:
        return {"enabled": True, "available": False}
    return indexer.health()


def sync_meilisearch_index(catalog: list, registry: list, price_lists: list) -> dict[str, Any]:
    if not meilisearch_available() or not MEILISEARCH_AUTO_SYNC:
        return {"skipped": True}
    indexer = get_product_indexer()
    if indexer is None:
        return {"skipped": True, "error": "indexer unavailable"}
    try:
        return indexer.sync_all(catalog, registry, price_lists)
    except Exception as exc:
        logger.warning("Meilisearch sync failed: %s", exc, exc_info=True)
        return {"skipped": False, "error": str(exc)}


def search_products(
    query: str,
    *,
    source: str | None = None,
    limit: int | None = None,
) -> list[Any]:
    engine = get_product_search_engine()
    if engine is None:
        return []
    return engine.search(query, source=source, limit=limit)
