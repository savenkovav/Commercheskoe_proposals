from __future__ import annotations

import re

import re

from rapidfuzz import fuzz

from src.services.data_loader import normalize_name

_SEARCH_STOP_WORDS = frozenset(
    """
    и в на для по из или том числе при без что как со кт с к о об от до
    """.split()
)


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


def extract_phrase_tokens(text: str) -> list[str]:
    normalized = normalize_name(text)
    tokens: list[str] = []
    seen: set[str] = set()
    for word in re.findall(r"[a-zа-яё0-9]+", normalized):
        if len(word) < 3 or word in _SEARCH_STOP_WORDS or word in seen:
            continue
        seen.add(word)
        tokens.append(word)
    return tokens


def _token_stem(token: str) -> str:
    if len(token) <= 4:
        return token
    if len(token) <= 6:
        return token[:4]
    return token[:6]


def token_present_in_text(token: str, text: str) -> bool:
    if not token or not text:
        return False
    if token in text:
        return True

    if token.isdigit():
        return bool(re.search(rf"(?<!\d){re.escape(token)}(?!\d)", text))

    stem = _token_stem(token)
    if len(stem) >= 5 and re.search(rf"(?<![a-zа-яё0-9]){re.escape(stem)}", text):
        return True

    if len(token) < 5:
        return False

    for word in text.split():
        if len(word) < 4:
            continue
        if fuzz.ratio(token, word) >= 92:
            return True
        if len(stem) >= 5 and len(word) >= max(5, len(stem) - 1):
            if fuzz.partial_ratio(stem, word) >= 94:
                return True
    return False


def phrase_token_coverage(query: str, choice: str) -> tuple[int, int, float]:
    tokens = extract_phrase_tokens(query)
    if not tokens:
        return 0, 0, 0.0
    normalized_choice = normalize_name(choice)
    hits = sum(1 for token in tokens if token_present_in_text(token, normalized_choice))
    return hits, len(tokens), hits / len(tokens)


def phrase_match_acceptable(query: str, choice: str) -> bool:
    hits, total, coverage = phrase_token_coverage(query, choice)
    if total == 0:
        return False
    if coverage >= 1.0:
        return True
    if total == 1:
        return hits >= 1
    if total == 2:
        return hits >= 2 or coverage >= 0.5
    required = max(2, (total + 1) // 2)
    return hits >= required or coverage >= 0.6


def catalog_phrase_match_score(query: str, choice: str, **_: object) -> float:
    """Скорер для длинных названий в индексе конкурентов (частичное совпадение фразы)."""
    if not query or not choice:
        return 0.0

    normalized_query = normalize_name(query)
    normalized_choice = normalize_name(choice)
    if normalized_query == normalized_choice:
        return 100.0

    fuzzy = name_match_score(normalized_query, normalized_choice)
    partial = float(fuzz.partial_ratio(normalized_query, normalized_choice))
    token_set = float(fuzz.token_set_ratio(normalized_query, normalized_choice))
    combined = max(fuzzy, partial, token_set)

    hits, total, coverage = phrase_token_coverage(query, choice)
    if total == 0:
        return combined

    if coverage >= 1.0:
        return max(combined, 97.0)
    if coverage >= 0.75 and hits >= 2:
        return max(combined, 93.0)
    if coverage >= 0.6 and hits >= 2:
        return max(combined, 90.0)
    if phrase_match_acceptable(query, choice):
        return max(combined, 86.0)
    return combined
