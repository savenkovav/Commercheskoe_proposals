from __future__ import annotations

from src.services.models import PriceListItem, PriceQuote
from src.services.product_lookup import (
    ProductLookupService,
    is_pish_articul,
    is_pish_competitor_item,
    is_pish_price_item,
)


def test_is_pish_articul_detects_cyrillic_prefix() -> None:
    assert is_pish_articul("ПИШ01748")
    assert is_pish_articul("пиш 01748")
    assert not is_pish_articul("EPP2401748")
    assert not is_pish_articul("")


def test_is_pish_price_item_uses_code_or_supplier() -> None:
    assert is_pish_price_item(
        PriceListItem(code="ПИШ001", name="Модель", price=100.0, sheet="s", supplier="X")
    )
    assert is_pish_price_item(
        PriceListItem(code="001", name="Модель", price=100.0, sheet="s", supplier="ПИШ")
    )
    assert not is_pish_price_item(
        PriceListItem(code="ABC001", name="Модель", price=100.0, sheet="s", supplier="X")
    )


def test_is_pish_competitor_item_from_articul_or_notes() -> None:
    assert is_pish_competitor_item({"articul": "ПИШ01748", "notes": ""})
    assert is_pish_competitor_item(
        {
            "articul": None,
            "notes": "Индекс каталога | articul: ПИШ01748",
        }
    )
    assert not is_pish_competitor_item({"articul": "ABC123", "notes": "обычная заметка"})


def test_is_pish_quote_from_price_code() -> None:
    from src.services.product_lookup import is_pish_quote
    from src.services.models import PriceQuote

    assert is_pish_quote(
        PriceQuote(
            source="price_list",
            label="Прайс",
            matched_name="Модель",
            notes="код ПИШ001",
        )
    )
    assert not is_pish_quote(
        PriceQuote(
            source="price_list",
            label="Прайс",
            matched_name="Модель",
            notes="код ABC001",
        )
    )


def test_build_competitors_block_prioritizes_pish_items() -> None:
    class StubWebSearch:
        def search_competitor_offers(self, query: str, sort_by_match: bool = False) -> list[PriceQuote]:
            return [
                PriceQuote(
                    source="web",
                    label="EPP24",
                    matched_name="Обычный товар",
                    price=1000.0,
                    match_score=95.0,
                    articul="ABC001",
                    url="https://epp24.ru/product/1",
                ),
                PriceQuote(
                    source="web",
                    label="EPP24",
                    matched_name="Генератор звуковой частоты",
                    price=29979.0,
                    match_score=100.0,
                    articul="ПИШ01748",
                    notes="Индекс каталога | articul: ПИШ01748",
                    url="https://epp24.ru/product/2",
                ),
            ]

        def search_web_price_fallback(self, query: str) -> list[PriceQuote]:
            return []

    service = ProductLookupService.__new__(ProductLookupService)
    service.web_search = StubWebSearch()
    block = service._build_competitors_block("генератор", pish_only=False)
    items = block["items"]
    assert len(items) == 2
    assert items[0]["articul"] == "ПИШ01748"


def test_build_competitors_block_pish_only_filters_non_pish() -> None:
    class StubWebSearch:
        def search_competitor_offers(self, query: str, sort_by_match: bool = False) -> list[PriceQuote]:
            return [
                PriceQuote(
                    source="web",
                    label="EPP24",
                    matched_name="Обычный товар",
                    price=1000.0,
                    match_score=95.0,
                    articul="ABC001",
                    url="https://epp24.ru/product/1",
                ),
                PriceQuote(
                    source="web",
                    label="EPP24",
                    matched_name="Генератор звуковой частоты",
                    price=29979.0,
                    match_score=100.0,
                    articul="ПИШ01748",
                    url="https://epp24.ru/product/2",
                ),
            ]

        def search_web_price_fallback(self, query: str) -> list[PriceQuote]:
            return []

    service = ProductLookupService.__new__(ProductLookupService)
    service.web_search = StubWebSearch()
    block = service._build_competitors_block("генератор", pish_only=True)
    items = block["items"]
    assert len(items) == 1
    assert items[0]["articul"] == "ПИШ01748"
