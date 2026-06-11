from __future__ import annotations

from rapidfuzz import fuzz


def name_match_score(query: str, choice: str, **_: object) -> float:
    """Составной скорер для сопоставления наименований товаров."""
    if not query or not choice:
        return 0.0
    if query == choice:
        return 100.0
    token_score = float(fuzz.token_set_ratio(query, choice))
    weighted_score = float(fuzz.WRatio(query, choice))
    sort_score = float(fuzz.token_sort_ratio(query, choice))
    return max(token_score, weighted_score, sort_score)
