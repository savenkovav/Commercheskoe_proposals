#!/usr/bin/env python3
"""Сгенерировать xlsx-шаблоны КП по образцам (задача 1 / 1+2, с маржой и без)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import KP_TEMPLATES_DIR
from src.services.excel_generator import ExcelGenerator
from src.services.models import MatchResult, MatchSource, MatchStatus, ProposalSummary, TZItem


def _demo_results(*, with_link: bool) -> list[MatchResult]:
    return [
        MatchResult(
            tz_item=TZItem(
                number=1,
                name="Модель почки (анатомическая демонстрационная)",
                unit="шт.",
                quantity=1,
                specifications="32.99.53.190",
            ),
            status=MatchStatus.EXACT,
            source=MatchSource.WEB,
            matched_name="Модель почки (анатомическая демонстрационная)",
            match_score=98.0,
            unit_cost=2297.0,
            unit_base_price=2297.0,
            unit_price=3100.95,
            total_cost=2297.0,
            total_price=3100.95,
            supplier="ozon Retorsa",
            notes="Конкурент",
            comparison=[
                __import__("src.services.models", fromlist=["PriceQuote"]).PriceQuote(
                    source="web",
                    label="ozon",
                    matched_name="Модель почки",
                    price=2297.0,
                    cost=2297.0,
                    url=(
                        "https://www.ozon.ru/product/anatomiya-model-123456789/"
                        if with_link
                        else None
                    ),
                    match_score=98.0,
                )
            ],
        ),
        MatchResult(
            tz_item=TZItem(
                number=2,
                name="Микроскоп бинокулярный",
                unit="шт.",
                quantity=13,
                specifications="",
            ),
            status=MatchStatus.SIMILAR,
            source=MatchSource.CATALOG,
            matched_name="Микроскоп бинокулярный учебный",
            match_score=92.0,
            unit_cost=5859.0,
            unit_base_price=5859.0,
            unit_price=7909.65,
            total_cost=76167.0,
            total_price=102825.45,
            supplier="ozon",
        ),
    ]


def _demo_summary(results: list[MatchResult]) -> ProposalSummary:
    return ProposalSummary(
        total_items=len(results),
        exact_count=1,
        similar_count=1,
        not_found_count=0,
        total_cost=sum(r.total_cost or 0 for r in results),
        total_base_price=sum(r.total_cost or 0 for r in results),
        total_price=sum(r.total_price or 0 for r in results),
        processing_seconds=1.2,
    )


def main() -> None:
    KP_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    gen = ExcelGenerator()
    variants = [
        ("KP_template_task1.xlsx", "task1", False),
        ("KP_template_task1_margin.xlsx", "task1", True),
        ("KP_template_task12.xlsx", "task1_task2", False),
        ("KP_template_task12_margin.xlsx", "task1_task2", True),
    ]
    for filename, task_mode, with_margin in variants:
        results = _demo_results(with_link=task_mode == "task1_task2")
        path = KP_TEMPLATES_DIR / filename
        gen.generate(
            results,
            _demo_summary(results),
            path,
            request_number="б/н",
            task_mode=task_mode,
            with_margin=with_margin,
            template_mode=True,
        )
        print("written", path)


if __name__ == "__main__":
    main()
