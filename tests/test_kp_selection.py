from __future__ import annotations

import copy

from src.services.kp_selection import (
    KpSelectionItem,
    apply_kp_selections,
    apply_web_addon_selection,
)
from src.services.models import MatchResult, MatchSource, MatchStatus, PriceQuote, TZItem


def _internet_only_result() -> MatchResult:
    return MatchResult(
        tz_item=TZItem(number=11, name="Комплект гипсовых моделей растений", unit="шт", quantity=1),
        status=MatchStatus.SIMILAR,
        source=MatchSource.WEB,
        matched_name="2.12.11. Комплект гипсовых моделей растений",
        match_score=100.0,
        unit_cost=5526.0,
        unit_base_price=5526.0,
        unit_price=5249.7,
        total_cost=5526.0,
        total_price=5249.7,
        internet_priced=True,
        comparison=[
            PriceQuote(
                source="web",
                label="EPP24",
                matched_name="2.12.11. Комплект гипсовых моделей растений",
                price=3526.0,
                cost=3526.0,
                match_score=100.0,
                url="https://epp24.ru/product/2-12-11-komplekt-gipsovyh-modelej-rastenij/",
            ),
        ],
    )


def test_internet_only_without_selection_has_no_price() -> None:
    result = _internet_only_result()
    selected = apply_kp_selections(
        [result],
        [KpSelectionItem(number=11, included=True, variant="primary", web_indices=())],
    )
    assert len(selected) == 1
    assert selected[0].unit_base_price is None
    assert selected[0].unit_price is None
    assert selected[0].total_price is None


def test_internet_only_uses_selected_web_price_not_auto_match() -> None:
    result = _internet_only_result()
    selected = apply_kp_selections(
        [result],
        [KpSelectionItem(number=11, included=True, variant="primary", web_indices=(0,))],
    )
    assert len(selected) == 1
    assert selected[0].unit_base_price == 3526.0
    assert selected[0].unit_price == 3349.7
    assert selected[0].total_price == 3349.7


def test_web_addon_replaces_auto_internet_price() -> None:
    result = _internet_only_result()
    applied = apply_web_addon_selection(copy.deepcopy(result), [0])
    assert applied.unit_base_price == 3526.0
    assert applied.unit_price == 3349.7


def test_manual_web_price_is_used() -> None:
    result = _internet_only_result()
    result.comparison[0].price = None
    result.comparison[0].cost = None
    selected = apply_kp_selections(
        [result],
        [
            KpSelectionItem(
                number=11,
                included=True,
                variant="primary",
                web_indices=(0,),
                web_manual_prices=((0, 4100.0),),
            ),
        ],
    )
    assert selected[0].unit_base_price == 4100.0
    assert selected[0].unit_price == 3895.0
