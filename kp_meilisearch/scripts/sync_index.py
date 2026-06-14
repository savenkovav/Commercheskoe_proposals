#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEILI_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT_ROOT), str(MEILI_ROOT)]

from src.config import CATALOG_PATH, REGISTRY_PATH  # noqa: E402
from src.services.data_loader import load_catalog, load_registry  # noqa: E402
from src.services.price_list_manager import get_price_list_manager  # noqa: E402
from kp_search.config import MeiliSettings  # noqa: E402
from kp_search.indexer import ProductIndexer  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_index")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync catalog/registry/prices to Meilisearch")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if MEILISEARCH_ENABLED=false",
    )
    args = parser.parse_args()

    settings = MeiliSettings.from_env()
    if not settings.enabled and not args.force:
        logger.error("MEILISEARCH_ENABLED=false. Set it in .env or pass --force")
        return 1

    indexer = ProductIndexer(settings)
    health = indexer.health()
    if not health.get("available"):
        logger.error("Meilisearch unavailable at %s: %s", settings.host, health.get("error", health))
        return 2

    catalog = load_catalog(CATALOG_PATH)
    registry = load_registry(REGISTRY_PATH)
    price_lists = get_price_list_manager().load_all_items()

    stats = indexer.sync_all(catalog, registry, price_lists)
    logger.info(
        "Synced %s documents (catalog=%s, registry=%s, prices=%s)",
        stats.get("documents", 0),
        len(catalog),
        len(registry),
        len(price_lists),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
