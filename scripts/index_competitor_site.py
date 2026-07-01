#!/usr/bin/env python3
"""Индексация каталога одного встроенного или добавленного сайта конкурента."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from src.logging_config import setup_logging
from src.services.app_state import get_processor
from src.services.competitor_catalog_service import (
    index_competitor_site_catalog,
    sync_unified_competitor_rag,
)
from src.services.competitor_product_store import get_competitor_product_store
from src.services.competitor_sites import CompetitorSite, get_builtin_competitor_site
from src.services.document_rag_index import get_document_rag_index
from src.services.tz_rag_service import TZRagService

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Index competitor site catalog")
    parser.add_argument("domain", help="Domain, e.g. td-school.ru")
    parser.add_argument("--force", action="store_true", help="Force reindex")
    args = parser.parse_args()

    setup_logging()
    domain = args.domain.lower().removeprefix("www.")
    builtin = get_builtin_competitor_site(domain)
    if not builtin:
        print(f"Unknown builtin site: {domain}", file=sys.stderr)
        return 1

    rag_index = get_document_rag_index(TZRagService(get_processor().ai))
    logger.info("Indexing %s ...", domain)
    result = index_competitor_site_catalog(builtin, rag_index, force=args.force or True)
    unified = sync_unified_competitor_rag(rag_index)
    store = get_competitor_product_store()
    payload = {
        "domain": domain,
        "catalog": result,
        "unified_rag": unified,
        "store": store.stats(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
