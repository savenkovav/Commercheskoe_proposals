from src.services.models import MatchResult, MatchSource, MatchStatus, TZItem
from src.services.pricing_rules import apply_kp_pricing, item_margin_percent
from src.services.tz_parser import _parse_tz_tables


def test_parse_table_without_price_column_does_not_use_row_number() -> None:
    table = [
        ["№ п/п", "Наименование товара", "Ед. изм.", "Кол-во"],
        ["1", "Товар А", "шт", "5"],
        ["2", "Товар Б", "шт", "3"],
    ]
    items = _parse_tz_tables([table])
    assert len(items) == 2
    assert items[0].target_sale_price is None
    assert items[1].target_sale_price is None


def test_parse_table_with_sale_price_column() -> None:
    table = [
        ["№", "Наименование товара", "Ед. изм.", "Кол-во", "Цена"],
        ["1", "Кабель ВВГнг 3х2.5", "м", "10", "150,50"],
    ]
    items = _parse_tz_tables([table])
    assert len(items) == 1
    assert items[0].target_sale_price == 150.5


def test_parse_equipment_request_table_without_row_numbers() -> None:
    table = [
        ["№ п/п", "Наименование", "Стоимость", "Количество"],
        ["", "Набор перкуссии модели FLT-PS5", "", "1"],
        ["", "Палочка эбонитовая", "", "1"],
        ["", "Демонстрационное оборудование «Сосуды сообщающиеся»", "", "1"],
        ["", "Набор муляжей овощей (большой)", "", "1"],
    ]
    items = _parse_tz_tables([table])
    assert len(items) == 4
    assert items[0].number == 1
    assert "перкуссии" in items[0].name.lower()
    assert items[0].quantity == 1.0
    assert items[2].number == 3
    assert "сосуды сообщающиеся" in items[2].name.lower()
    assert items[3].quantity == 1.0


def test_apply_kp_pricing_uses_tz_sale_price() -> None:
    tz_item = TZItem(
        number=1,
        name="Кабель",
        unit="м",
        quantity=2,
        target_sale_price=1000.0,
    )
    result = MatchResult(
        tz_item=tz_item,
        status=MatchStatus.EXACT,
        source=MatchSource.CATALOG,
        unit_cost=800.0,
        unit_base_price=900.0,
    )
    apply_kp_pricing(result)
    assert result.unit_price == 1000.0
    assert result.total_price == 2000.0
    assert result.applied_markup_pct == 25.0
    assert item_margin_percent(result.unit_cost, result.unit_price) == 25.0


def test_apply_kp_pricing_ignores_tz_sale_price_without_cost() -> None:
    tz_item = TZItem(
        number=1,
        name="Товар",
        unit="шт",
        quantity=1,
        target_sale_price=1.0,
    )
    result = MatchResult(
        tz_item=tz_item,
        status=MatchStatus.SIMILAR,
        source=MatchSource.NONE,
    )
    apply_kp_pricing(result)
    assert result.unit_price is None
    assert result.total_price is None


def test_missing_price_note_for_task1() -> None:
    from src.services.tz_match_service import _missing_price_note

    assert (
        _missing_price_note(internet_allowed=False, use_ai=True)
        == "Не найдено в каталогах, прайсах и остатках"
    )
    assert _missing_price_note(internet_allowed=True, use_ai=True) == "Поиск цены в интернете"
