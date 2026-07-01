#!/usr/bin/env python3
"""Полная переиндексация каталогов всех сайтов конкурентов."""

from __future__ import annotations

import json
import logging
import sys

from src.logging_config import setup_logging
from src.services.app_state import get_processor
from src.services.competitor_catalog_service import reindex_all_competitor_sites
from src.services.competitor_catalog_urls import get_competitor_catalog_url_registry
from src.services.document_rag_index import get_document_rag_index
from src.services.tz_rag_service import TZRagService

logger = logging.getLogger(__name__)


def main() -> int:
    setup_logging()
    registry = get_competitor_catalog_url_registry()
    registry.add_page(
        "https://skale.ru/magazin/folder/uchebnoe-oborudovanie-po-astronomii-i-astrofizike",
        domain="skale.ru",
        label="Скале",
        source="seed",
    )

    rag_index = get_document_rag_index(TZRagService(get_processor().ai))
    summary = reindex_all_competitor_sites(rag_index, force=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
