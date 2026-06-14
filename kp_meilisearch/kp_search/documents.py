from __future__ import annotations

from typing import Any

from src.services.models import CatalogItem, PriceListItem, RegistryItem


def _catalog_doc(item: CatalogItem, index: int) -> dict[str, Any] | None:
    if item.entry_type not in {"item", "kit_total", "sub_kit"}:
        return None
    if not item.name.strip():
        return None
    return {
        "id": f"catalog_{index}",
        "name": item.name.strip(),
        "source": "catalog",
        "source_index": index,
        "cost": item.cost,
        "price": item.price,
        "unit": item.unit,
        "detail": item.source_file,
        "entry_type": item.entry_type,
    }


def _registry_doc(item: RegistryItem, index: int) -> dict[str, Any] | None:
    if not item.name.strip():
        return None
    return {
        "id": f"registry_{index}",
        "name": item.name.strip(),
        "source": "registry",
        "source_index": index,
        "quantity": item.quantity,
        "detail": f"остаток: {item.quantity} шт.",
    }


def _price_doc(item: PriceListItem, index: int) -> dict[str, Any] | None:
    if not item.name.strip():
        return None
    return {
        "id": f"price_{index}",
        "name": item.name.strip(),
        "source": "price_list",
        "source_index": index,
        "price": item.price,
        "supplier": item.supplier,
        "code": item.code,
        "sheet": item.sheet,
        "detail": f"{item.supplier} / {item.sheet} / код {item.code}",
    }


def build_documents(
    catalog: list[CatalogItem],
    registry: list[RegistryItem],
    price_lists: list[PriceListItem],
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []

    for index, item in enumerate(catalog):
        doc = _catalog_doc(item, index)
        if doc:
            documents.append(doc)

    for index, item in enumerate(registry):
        doc = _registry_doc(item, index)
        if doc:
            documents.append(doc)

    for index, item in enumerate(price_lists):
        doc = _price_doc(item, index)
        if doc:
            documents.append(doc)

    return documents
