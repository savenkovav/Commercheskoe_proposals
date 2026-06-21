#!/usr/bin/env python3
"""Обновление каталога, реестра остатков и перезагрузка прайсов."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from src.config import CATALOG_PATH, REGISTRY_PATH, REGISTRY_PHOTOS_DIR
from src.logging_config import setup_logging
from src.services.app_state import get_processor, reload_processor
from src.services.data_loader import load_catalog, load_registry
from src.services.document_rag_index import get_document_rag_index
from src.services.price_list_manager import get_price_list_manager
from src.services.tz_rag_service import TZRagService

logger = logging.getLogger(__name__)


def _copy_source(source: Path, destination: Path) -> int:
    if not source.exists():
        raise FileNotFoundError(f"Файл не найден: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description="Update catalog, registry and reload prices")
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Путь к .xlsx каталога (скопируется в data/catalog.xlsx)",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        help="Путь к .xlsx реестра остатков (скопируется в data/registry.xlsx)",
    )
    parser.add_argument(
        "--reload-only",
        action="store_true",
        help="Только перезагрузить уже сохранённые источники и прайсы",
    )
    args = parser.parse_args()

    setup_logging()
    report: dict[str, object] = {"copied": {}, "counts": {}}

    if args.catalog:
        _copy_source(args.catalog, CATALOG_PATH)
        report["copied"]["catalog"] = str(CATALOG_PATH)
        items = load_catalog(CATALOG_PATH)
        report["counts"]["catalog"] = len(items)
        products = [i for i in items if i.entry_type in {"item", "kit_total", "sub_kit"}]
        with_supplier = [i for i in products if i.supplier]
        logger.info(
            "Catalog loaded: %s rows, %s products, %s with supplier",
            len(items),
            len(products),
            len(with_supplier),
        )

    if args.registry:
        _copy_source(args.registry, REGISTRY_PATH)
        report["copied"]["registry"] = str(REGISTRY_PATH)
        registry_items = load_registry(REGISTRY_PATH, REGISTRY_PHOTOS_DIR)
        report["counts"]["registry"] = len(registry_items)

    if not args.reload_only and not args.catalog and not args.registry:
        parser.error("Укажите --catalog и/или --registry, либо --reload-only")

    price_items = reload_processor()
    report["counts"]["price_list_items"] = price_items

    processor = get_processor()
    rag_index = get_document_rag_index(TZRagService(processor.ai))
    rag_report: dict[str, object] = {}
    if args.catalog or args.reload_only:
        rag_report["catalog"] = rag_index.index_document(
            doc_id="catalog:main",
            source_type="catalog",
            source_name=CATALOG_PATH.stem,
            file_path=CATALOG_PATH,
            force=True,
        )
    if args.registry or args.reload_only:
        rag_report["registry"] = rag_index.index_document(
            doc_id="registry:main",
            source_type="registry",
            source_name=REGISTRY_PATH.stem,
            file_path=REGISTRY_PATH,
            force=True,
        )
    report["rag"] = rag_report
    report["price_lists"] = [
        {"id": entry.id, "name": entry.name, "supplier": entry.supplier, "items": entry.items_count}
        for entry in get_price_list_manager().list_entries()
    ]

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
