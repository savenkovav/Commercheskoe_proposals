from src.services.kp_lookup_import import (
    build_match_result_from_lookup_catalog,
    build_match_result_from_lookup_competitor,
    build_match_result_from_lookup_item,
    build_match_result_from_lookup_price_list,
    build_match_result_from_lookup_registry,
    find_result_by_lookup_key,
    lookup_competitor_key,
    lookup_item_key,
    parse_lookup_price_amount,
)
from src.services.models import MatchSource


def test_parse_lookup_price_amount_from_number() -> None:
    assert parse_lookup_price_amount({"price_amount": 733.0}) == 733.0


def test_parse_lookup_price_amount_from_string() -> None:
    assert parse_lookup_price_amount({"price": "733.00 ₽"}) == 733.0


def test_lookup_item_key_catalog() -> None:
    item = {"source_type": "catalog", "name": "Товар А", "row_index": 42}
    assert lookup_item_key(item) == "catalog|Товар А|row:42"


def test_lookup_item_key_price_list() -> None:
    item = {
        "source_type": "price_list",
        "name": "Товар Б",
        "code": "X-1",
        "supplier": "Поставщик",
    }
    assert lookup_item_key(item) == "price_list|Товар Б|code:X-1|supplier:Поставщик"


def test_lookup_item_key_registry() -> None:
    item = {
        "source_type": "registry",
        "name": "Товар В",
        "link": "https://example.com/item",
    }
    assert lookup_item_key(item) == "registry|Товар В|link:https://example.com/item"


def test_build_match_result_from_lookup_competitor() -> None:
    item = {
        "label": "Rostcom",
        "name": "Рабочий словарик. 1 класс",
        "match_score": 100.0,
        "price": "733.00 ₽",
        "price_amount": 733.0,
        "url": "https://rostcom.com/catalog/element/rabochiy_slovarik_1_klass_/",
        "has_price": True,
    }
    result = build_match_result_from_lookup_competitor("рабочий словарик", 1, item)
    assert result.tz_item.name == "рабочий словарик"
    assert result.matched_name == "Рабочий словарик. 1 класс"
    assert result.unit_base_price == 733.0
    assert result.unit_price == 696.35
    assert result.internet_priced is True
    assert result.competitors[0].url == item["url"]
    assert result.lookup_kp_key == item["url"]


def test_build_match_result_from_lookup_catalog() -> None:
    item = {
        "source_type": "catalog",
        "name": "Кабель HDMI",
        "match_score": 92.0,
        "cost": "100.00 ₽",
        "price": "150.00 ₽",
        "row_index": 7,
        "supplier": "Склад",
    }
    result = build_match_result_from_lookup_catalog("кабель hdmi", 2, item)
    assert result.source == MatchSource.CATALOG
    assert result.unit_cost == 100.0
    assert result.unit_base_price == 150.0
    assert result.lookup_kp_key == "catalog|Кабель HDMI|row:7"


def test_build_match_result_from_lookup_price_list() -> None:
    item = {
        "source_type": "price_list",
        "name": "Мышь USB",
        "match_score": 88.0,
        "price": "450.00 ₽",
        "code": "M-01",
        "supplier": "Опт",
    }
    result = build_match_result_from_lookup_price_list("мышь", 3, item)
    assert result.source == MatchSource.PRICE_LIST
    assert result.unit_cost == 450.0
    assert result.supplier == "Опт"
    assert result.lookup_kp_key == "price_list|Мышь USB|code:M-01|supplier:Опт"


def test_build_match_result_from_lookup_registry() -> None:
    item = {
        "source_type": "registry",
        "name": "Монитор 24",
        "match_score": 90.0,
        "link": "https://stock.local/monitor",
    }
    result = build_match_result_from_lookup_registry("монитор", 4, item)
    assert result.source == MatchSource.REGISTRY
    assert result.lookup_kp_key == "registry|Монитор 24|link:https://stock.local/monitor"


def test_build_match_result_from_lookup_item_dispatches_by_source() -> None:
    catalog = build_match_result_from_lookup_item(
        "q",
        1,
        {"source_type": "catalog", "name": "A", "cost": "10 ₽"},
    )
    assert catalog.source == MatchSource.CATALOG


def test_find_result_by_lookup_key() -> None:
    item = {
        "label": "Rostcom",
        "name": "Рабочий словарик. 1 класс",
        "url": "https://rostcom.com/catalog/element/rabochiy_slovarik_1_klass_/",
    }
    result = build_match_result_from_lookup_competitor("рабочий словарик", 1, item)
    found = find_result_by_lookup_key([result], item)
    assert found is result
    assert lookup_competitor_key(item) == item["url"]


def test_find_result_by_lookup_key_catalog() -> None:
    item = {
        "source_type": "catalog",
        "name": "Кабель HDMI",
        "row_index": 7,
        "cost": "100.00 ₽",
    }
    result = build_match_result_from_lookup_catalog("кабель", 1, item)
    found = find_result_by_lookup_key([result], item)
    assert found is result
