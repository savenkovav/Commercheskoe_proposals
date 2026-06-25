from __future__ import annotations

from src.services.models import TZItem


def tz_item_rag_text(item: TZItem) -> str:
    """Структурированный текст позиции ТЗ для векторизации (RAG / embeddings)."""
    lines = [
        f"Позиция ТЗ №{item.number}",
        f"Наименование товара: {item.name.strip()}",
    ]
    specs = (item.specifications or "").strip()
    if specs:
        lines.append(f"Характеристики товара: {specs}")
    lines.append(f"Единица измерения товара: {item.unit.strip() or 'шт.'}")
    lines.append(f"Количество товара: {item.quantity:g}")
    country = (item.country_of_origin or "").strip()
    if country:
        lines.append(f"Наименование страны происхождения товара: {country}")
    return "\n".join(lines)


def build_tz_items_document(items: list[TZItem]) -> str:
    return "\n\n".join(tz_item_rag_text(item) for item in items if item.name.strip())
