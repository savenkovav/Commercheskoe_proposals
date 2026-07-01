"""Пороги совпадения: сначала 100%, при отсутствии — 95%, иначе интернет."""

from __future__ import annotations

from collections.abc import Iterable

from src.config import LOCAL_MATCH_THRESHOLD
from src.services.models import TZItem
from src.services.tz_search import combined_match_score

EXACT_SCORE = 100.0
FALLBACK_SCORE = float(LOCAL_MATCH_THRESHOLD)


def effective_score(tz_item: TZItem, matched_name: str, fuzzy_score: float) -> float:
    return max(float(fuzzy_score), combined_match_score(tz_item, matched_name))


def resolve_local_floor(scores: Iterable[float]) -> float | None:
    """100, если есть точное совпадение; иначе 95 при наличии; иначе None (интернет)."""
    values = [float(score) for score in scores]
    if not values:
        return None
    if max(values) >= EXACT_SCORE:
        return EXACT_SCORE
    if max(values) >= FALLBACK_SCORE:
        return FALLBACK_SCORE
    return None


def meets_floor(score: float, floor: float) -> bool:
    return float(score) >= float(floor)
