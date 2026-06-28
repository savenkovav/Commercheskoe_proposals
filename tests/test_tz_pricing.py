from src.services.models import MatchResult, MatchSource, MatchStatus, TZItem
from src.services.pricing_rules import apply_kp_pricing, item_margin_percent
from src.services.tz_match_service import _missing_price_note
from src.services.tz_parser import _parse_tz_tables


def test_parse_table_with_sale_price_column() -> None:
    table = [
        ["№", "Наименование товара", "Ед. изм.", "Кол-во", "Цена"],
        ["1", "Кабель ВВГнг 3х2.5", "м", "10", "150,50"],
    ]
    items = _parse_tz_tables([table])
    assert len(items) == 1
    assert items[0].target_sale_price == 150.5


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


def test_missing_price_note_for_task1() -> None:
    assert (
        _missing_price_note(internet_allowed=False, use_ai=True)
        == "Не найдено в каталогах, прайсах и остатках"
    )
    assert _missing_price_note(internet_allowed=True, use_ai=True) == "Поиск цены в интернете"
