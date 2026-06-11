from __future__ import annotations

import re

from src.config import EXACT_MATCH_THRESHOLD, LOCAL_MATCH_THRESHOLD
from src.services.data_loader import normalize_name
from src.services.fuzzy_scoring import name_match_score
from src.services.models import TZItem

_COMPLIANCE_SUFFIX = re.compile(r"\s*[–—-]\s*соответствие\s*$", re.IGNORECASE)
_CODE_PREFIX = re.compile(r"^\d+(?:\.\d+)+\.\s*")
_KIT_HEADER = re.compile(
    r"(?:включает|в\s+состав\s+вход|состоит\s+из|комплектация)\s*(?:следующ|перечень)?",
    re.IGNORECASE,
)

_GENERIC_WORDS = frozenset(
    """
    предназначена предназначен предназначено предназначены
    изготовлена изготовлен изготовлено изготовлены
    комплект набор модель система прибор приборы
    соответствие требованиям требование
    наличие наличии имеет иметь
    более менее размер размеры длина диаметр
    штука штук шт
    включает следующие следующий состоит входит перечень
    """.split()
)

_CATEGORY_CONFLICTS: tuple[tuple[str, str], ...] = (
    ("аудио", "dvd"),
    ("аудиосистем", "dvd"),
    ("аудиосистем", "астроном"),
    ("микрофон", "dvd"),
    ("микрофон", "астроном"),
    ("partybox", "dvd"),
    ("partybox", "астроном"),
    ("jbl", "dvd"),
    ("jbl", "астроном"),
    ("behringer", "dvd"),
    ("behringer", "астроном"),
    ("планетар", "dvd"),
    ("планетар", "микрофон"),
    ("гипсов", "dvd"),
    ("гипсов", "провод"),
    ("гипсов", "калориметр"),
    ("геометрич", "калориметр"),
    ("геометрич", "калориметрич"),
    ("мольберт", "dvd"),
    ("термометр", "dvd"),
)


def primary_spec_line(specifications: str) -> str:
    if not specifications or not specifications.strip():
        return ""
    line = specifications.replace("\r\n", "\n").replace("\r", "\n").split("\n")[0].strip()
    line = _COMPLIANCE_SUFFIX.sub("", line).strip()
    line = _CODE_PREFIX.sub("", line).strip()
    return line


def is_kit_composition_header(spec_line: str) -> bool:
    if not spec_line:
        return False
    lower = spec_line.lower()
    if _KIT_HEADER.search(lower):
        return True
    return spec_line.rstrip().endswith(":")


def build_search_queries(tz_item: TZItem) -> list[str]:
    name = tz_item.name.strip()
    spec_line = primary_spec_line(tz_item.specifications)
    queries: list[str] = []

    if name:
        queries.append(name)

    if spec_line and not is_kit_composition_header(spec_line):
        if normalize_name(spec_line) != normalize_name(name):
            queries.append(spec_line)
            queries.append(f"{name} {spec_line}")

    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        key = normalize_name(query)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(query.strip())
    return unique or ([name] if name else [])


def primary_search_text(tz_item: TZItem) -> str:
    name = tz_item.name.strip()
    spec_line = primary_spec_line(tz_item.specifications)
    if spec_line and not is_kit_composition_header(spec_line):
        if normalize_name(spec_line) != normalize_name(name):
            return spec_line
    return name


def spec_required_tokens(tz_item: TZItem) -> list[str]:
    spec_line = primary_spec_line(tz_item.specifications)
    if not spec_line or is_kit_composition_header(spec_line):
        return []

    name_tokens = set(normalize_name(tz_item.name).split())
    tokens: list[str] = []
    seen: set[str] = set()

    for raw in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", spec_line):
        token = normalize_name(raw)
        if len(token) < 3 or token in _GENERIC_WORDS or token in name_tokens:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)

    if not tokens:
        return []

    distinctive = [
        token
        for token in tokens
        if any(ch.isdigit() for ch in token) or len(token) >= 5
    ]
    if distinctive:
        return distinctive[:8]
    return []


def _category_conflict(tz_item: TZItem, matched_name: str) -> bool:
    haystack = normalize_name(
        f"{tz_item.name} {primary_spec_line(tz_item.specifications)}"
    )
    choice = normalize_name(matched_name)
    for query_marker, choice_marker in _CATEGORY_CONFLICTS:
        if query_marker in haystack and choice_marker in choice:
            return True
    return False


def _required_tokens_present(required: list[str], matched_name: str) -> bool:
    if not required:
        return True
    choice = normalize_name(matched_name)
    hits = sum(1 for token in required if token in choice)
    if len(required) == 1:
        return hits >= 1
    if len(required) == 2:
        return hits >= 1
    return hits >= max(2, len(required) // 2)


def relevance_score(tz_item: TZItem, matched_name: str) -> float:
    spec_line = primary_spec_line(tz_item.specifications)
    choice = normalize_name(matched_name)
    name_score = name_match_score(normalize_name(tz_item.name), choice)
    if spec_line and not is_kit_composition_header(spec_line):
        spec_score = name_match_score(normalize_name(spec_line), choice)
        return max(name_score, spec_score)
    return name_score


def is_relevant_match(
    tz_item: TZItem,
    matched_name: str,
    *,
    min_score: float = LOCAL_MATCH_THRESHOLD,
    score: float | None = None,
) -> bool:
    if not matched_name:
        return False
    if _category_conflict(tz_item, matched_name):
        return False

    name_score = name_match_score(
        normalize_name(tz_item.name),
        normalize_name(matched_name),
    )
    effective_score = relevance_score(tz_item, matched_name)
    if score is not None:
        effective_score = min(float(score), effective_score, name_score)

    if name_score >= min_score:
        return True

    required = spec_required_tokens(tz_item)
    if required and not _required_tokens_present(required, matched_name):
        return False

    spec_line = primary_spec_line(tz_item.specifications)
    if (
        spec_line
        and not is_kit_composition_header(spec_line)
        and normalize_name(spec_line) != normalize_name(tz_item.name)
    ):
        spec_score = name_match_score(
            normalize_name(spec_line),
            normalize_name(matched_name),
        )
        if spec_score >= min_score:
            return True
        if required:
            return False
        return effective_score >= EXACT_MATCH_THRESHOLD

    return effective_score >= min_score
