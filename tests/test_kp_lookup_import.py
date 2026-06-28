from src.services.kp_lookup_import import (
    build_match_result_from_lookup_competitor,
    find_result_by_lookup_key,
    lookup_competitor_key,
    parse_lookup_price_amount,
)


def test_parse_lookup_price_amount_from_number() -> None:
    assert parse_lookup_price_amount({"price_amount": 733.0}) == 733.0


def test_parse_lookup_price_amount_from_string() -> None:
    assert parse_lookup_price_amount({"price": "733.00 ₽"}) == 733.0


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
