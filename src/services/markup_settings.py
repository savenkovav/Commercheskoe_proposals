from __future__ import annotations

from src.config import MARKUP_PERCENT

_current_markup_percent: float = MARKUP_PERCENT


def get_markup_percent() -> float:
    return _current_markup_percent


def set_markup_percent(value: float) -> float:
    global _current_markup_percent
    numeric = float(value)
    if numeric < 0 or numeric > 1000:
        raise ValueError("Наценка должна быть от 0 до 1000%")
    _current_markup_percent = round(numeric, 2)
    return _current_markup_percent
