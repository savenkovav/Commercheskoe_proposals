from __future__ import annotations

from src.services.kp_manual_entry import allows_custom_manual_entry
from src.services.kp_selection import KpSelectionItem, apply_kp_selections
from src.services.models import MatchResult, MatchSource, MatchStatus, PriceQuote, TZItem


def _not_found_result() -> MatchResult:
    return MatchResult(
        tz_item=TZItem(number=3, name="LER2938 Робот Ботли", unit="шт.", quantity=2),
        status=MatchStatus.NOT_FOUND,
        source=MatchSource.NONE,
    )


def _marketplace_only_result() -> MatchResult:
    return MatchResult(
        tz_item=TZItem(number=4, name="Редкая игрушка", unit="шт.", quantity=1),
        status=MatchStatus.NOT_FOUND,
        source=MatchSource.WEB,
        comparison=[
            PriceQuote(
                source="web",
                label="Ozon",
                matched_name="Поиск на Ozon",
                url="https://www.ozon.ru/search/?text=toy",
                match_score=100.0,
            ),
        ],
    )


def _catalog_result() -> MatchResult:
    return MatchResult(
        tz_item=TZItem(number=5, name="Краски", unit="шт.", quantity=1),
        status=MatchStatus.EXACT,
        source=MatchSource.CATALOG,
        matched_name="Краски акриловые",
        match_score=95.0,
        unit_cost=100.0,
        unit_base_price=120.0,
        unit_price=156.0,
        comparison=[
            PriceQuote(
                source="catalog",
                label="Каталог",
                matched_name="Краски акриловые",
                cost=100.0,
                price=120.0,
                match_score=95.0,
            ),
        ],
    )


def test_allows_custom_for_not_found() -> None:
    assert allows_custom_manual_entry(_not_found_result()) is True


def test_allows_custom_for_marketplace_only() -> None:
    assert allows_custom_manual_entry(_marketplace_only_result()) is True


def test_disallows_custom_for_catalog_match() -> None:
    assert allows_custom_manual_entry(_catalog_result()) is False


def test_custom_manual_price_applied_to_kp() -> None:
    result = _not_found_result()
    selected = apply_kp_selections(
        [result],
        [
            KpSelectionItem(
                number=3,
                included=True,
                custom_enabled=True,
                custom_unit_price=15000.0,
                custom_quantity=3.0,
            ),
        ],
    )
    assert len(selected) == 1
    item = selected[0]
    assert item.tz_item.quantity == 3.0
    assert item.unit_base_price == 15000.0
    assert item.unit_price == 19500.0
    assert item.total_price == 58500.0
    assert item.notes == "Ручной ввод цены"
