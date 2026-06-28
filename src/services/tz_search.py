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
    перемешивания перемешивание различных различные
    химических химические веществ вещества
    должна должно должны может могут
    объем объема емкость емкости
    использования использование применения применение
    обеспечивает обеспечение соответствует соответствовать
    """.split()
)

_SPEC_FILLER = frozenset(
    """
    для при без над под из от до применения использования
    или либо также других другие различных различные
    """.split()
)

_NAME_GENERIC = frozenset(
    """
    портативная портативный портативное портативные
    лабораторный лабораторная лабораторное лабораторные
    комплект набор модель модели моделей система демонстрационный демонстрационная
    гипсовых гипсов гипсовые гипсовая
    цифровой цифровая цифровое цифровые
    стеклянная стеклянный стеклянное
    двухместная двухместный регулировкой регулировка
    виртуальный виртуальная кубический кубическая
    отечественных зарубежных художников
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
    ("аудио", "парта"),
    ("аудиосистем", "парта"),
    ("partybox", "парта"),
    ("jbl", "парта"),
    ("behringer", "парта"),
    ("колонк", "парта"),
    ("микрофон", "парта"),
    ("парта", "аудио"),
    ("парта", "аудиосистем"),
    ("парта", "partybox"),
    ("парта", "jbl"),
    ("парта", "behringer"),
    ("парта", "колонк"),
    ("парта", "микрофон"),
    ("микрофон", "микроскоп"),
    ("микроскоп", "микрофон"),
    ("портрет", "муляж"),
    ("портрет", "фрукт"),
    ("портрет", "овощ"),
    ("художник", "муляж"),
    ("художник", "фрукт"),
    ("художник", "овощ"),
    ("муляж", "портрет"),
    ("фрукт", "портрет"),
    ("овощ", "портрет"),
)

_PRODUCT_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("desk", ("парта", "парты", "ученическ", "стол школьн", "двухместн")),
    ("audio", ("колонк", "аудио", "аудиосистем", "partybox", "акустич", "динамик", "сабвуфер", "boombox")),
    ("microscope", ("микроскоп", "окуляр", "объективн")),
    ("microphone", ("микрофон", "радиосистем", "вокальн", "ulm300", "ulm")),
    ("easel", ("мольберт",)),
    ("thermometer", ("термометр",)),
    ("planetarium", ("планетар",)),
    ("gypsum", ("гипсов", "муляж", "натюрморт")),
    ("glassware", ("палочк", "стеклянн", "колб", "пробирк")),
    ("stationery", ("скетчбук", "блокнот", "тетрад", "канцтовар", "кожзам", "бумаг", "ручк", "карандаш")),
    ("molecular", ("молекул", "кристаллич", "решеток", "решёток", "органик", "неорганик")),
    ("still_life", ("муляж", "фрукт", "овощ", "натюрморт", "натюрмор")),
    ("portrait", ("портрет", "художник", "композитор")),
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


def has_meaningful_spec(tz_item: TZItem) -> bool:
    spec_line = primary_spec_line(tz_item.specifications)
    if not spec_line or is_kit_composition_header(spec_line):
        return False
    return normalize_name(spec_line) != normalize_name(tz_item.name)


def spec_line_is_characteristic_row(spec_line: str) -> bool:
    """Строка вида «параметр: значение» из вертикальной таблицы ТЗ."""
    if not spec_line or is_kit_composition_header(spec_line):
        return False
    if ":" not in spec_line:
        return False
    label, _, value = spec_line.partition(":")
    return bool(label.strip()) and bool(value.strip()) and len(label.strip()) <= 120


def tz_match_query(tz_item: TZItem) -> str:
    """Приоритетный текст сопоставления: наименование + первая характеристика."""
    name = tz_item.name.strip()
    spec_line = primary_spec_line(tz_item.specifications)
    if spec_line and spec_line_is_characteristic_row(spec_line):
        return name
    if spec_line and not is_kit_composition_header(spec_line):
        spec_norm = normalize_name(spec_line)
        name_norm = normalize_name(name)
        if spec_norm != name_norm:
            if spec_norm.startswith(name_norm):
                return spec_line.strip()
            return f"{name} {spec_line}".strip()
    return name


def build_search_queries(tz_item: TZItem) -> list[str]:
    name = tz_item.name.strip()
    combined = tz_match_query(tz_item)
    spec_line = primary_spec_line(tz_item.specifications)
    queries: list[str] = []

    if combined:
        queries.append(combined)
    if name and normalize_name(name) != normalize_name(combined):
        queries.append(name)
    if spec_line and not is_kit_composition_header(spec_line):
        if normalize_name(spec_line) != normalize_name(name):
            queries.append(spec_line)

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
    return tz_match_query(tz_item)


def internet_search_text(tz_item: TZItem) -> str:
    """Короткий запрос для интернета и конкурентов — только наименование."""
    name = tz_item.name.strip()
    if name:
        return name
    return primary_search_text(tz_item)


def tz_item_search_text(tz_item: TZItem) -> str:
    """Текст для индексации/RAG: наименование, характеристика, страна."""
    name = tz_item.name.strip()
    spec_line = primary_spec_line(tz_item.specifications)
    parts = [name] if name else []

    if spec_line and not is_kit_composition_header(spec_line):
        if normalize_name(spec_line) != normalize_name(name):
            parts.append(spec_line)

    country = (tz_item.country_of_origin or "").strip()
    if country:
        parts.append(country)

    if not parts:
        return name
    if len(parts) == 1:
        return parts[0]
    return " ".join(parts)


def name_anchor_tokens(tz_item: TZItem) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", tz_item.name):
        token = normalize_name(raw)
        if len(token) < 4 or token in _GENERIC_WORDS or token in _NAME_GENERIC:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)

    if tokens:
        return tokens[:4]

    normalized = normalize_name(tz_item.name)
    fallback = [word for word in normalized.split() if len(word) >= 4]
    return fallback[:2]


def _tz_name_is_category_only(tz_item: TZItem) -> bool:
    """Наименование — только категория без марки, модели или артикула."""
    words = [w for w in normalize_name(tz_item.name).split() if w]
    if not words or len(words) > 2:
        return False
    for raw in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", tz_item.name):
        token = normalize_name(raw)
        if any(ch.isdigit() for ch in token):
            return False
        if len(token) >= 4 and re.search(r"[a-z]", token):
            return False
    return True


def spec_identity_tokens(tz_item: TZItem) -> list[str]:
    """Марка, модель и артикул из характеристик — обязательны при общем наименовании."""
    return [
        token
        for token in spec_required_tokens(tz_item)
        if any(ch.isdigit() for ch in token)
        or (len(token) >= 5 and re.search(r"[a-z]", token))
    ]


def _identity_tokens_present(identity: list[str], matched_name: str) -> bool:
    if not identity:
        return True
    choice = normalize_name(matched_name)
    return all(token in choice for token in identity)


def spec_required_tokens(tz_item: TZItem) -> list[str]:
    from src.services.matcher import ItemMatcher

    spec_line = primary_spec_line(tz_item.specifications)
    if not spec_line or is_kit_composition_header(spec_line):
        return []

    name_tokens = set(normalize_name(tz_item.name).split())
    tokens: list[str] = []
    seen: set[str] = set()

    for raw in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", spec_line):
        token = normalize_name(raw)
        if (
            len(token) < 3
            or token in _GENERIC_WORDS
            or token in _SPEC_FILLER
            or token in name_tokens
        ):
            continue
        if ItemMatcher.is_distinctive_mismatch(token, tz_item.name):
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
        if any(ch.isdigit() for ch in token)
        or len(token) >= 5
        or (len(token) >= 3 and re.search(r"[a-z]", token))
    ]
    if distinctive:
        return distinctive[:8]
    return tokens[:4]


def detect_product_types(text: str) -> set[str]:
    normalized = normalize_name(text)
    if not normalized:
        return set()
    found: set[str] = set()
    for type_id, markers in _PRODUCT_TYPES:
        if any(marker in normalized for marker in markers):
            found.add(type_id)
    return found


def product_type_conflict(tz_item: TZItem, matched_name: str) -> bool:
    from src.services.matcher import ItemMatcher

    if ItemMatcher.is_distinctive_mismatch(tz_item.name, matched_name):
        return True

    name_distinctive_agreement = ItemMatcher.distinctive_markers_agree(
        tz_item.name, matched_name
    )

    spec_line = primary_spec_line(tz_item.specifications)
    if spec_line and not is_kit_composition_header(spec_line):
        if not name_distinctive_agreement and ItemMatcher.is_distinctive_mismatch(
            spec_line, matched_name
        ):
            return True

    tz_types = detect_product_types(tz_item.name)
    if spec_line and not is_kit_composition_header(spec_line) and not name_distinctive_agreement:
        tz_types |= detect_product_types(spec_line)
    if not tz_types:
        tz_types = detect_product_types(tz_match_query(tz_item))

    match_types = detect_product_types(matched_name)
    if not tz_types or not match_types:
        return False
    if tz_types & match_types:
        return False
    return True


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


def _token_matches_anchor(anchor: str, text: str) -> bool:
    if anchor in text:
        return True
    if len(anchor) >= 5 and anchor[:5] in text:
        return True
    return False


def _name_anchors_satisfied(tz_item: TZItem, matched_name: str) -> bool:
    anchors = name_anchor_tokens(tz_item)
    if not anchors:
        return True
    choice = normalize_name(matched_name)
    hits = sum(1 for anchor in anchors if _token_matches_anchor(anchor, choice))
    if len(anchors) == 1:
        return hits >= 1
    return hits >= len(anchors)


def combined_match_score(tz_item: TZItem, matched_name: str) -> float:
    choice = normalize_name(matched_name)
    if not choice:
        return 0.0

    name_score = name_match_score(normalize_name(tz_item.name), choice)
    if not has_meaningful_spec(tz_item):
        return name_score

    spec_line = primary_spec_line(tz_item.specifications)
    spec_norm = normalize_name(spec_line)
    combined_query = normalize_name(tz_match_query(tz_item))
    combined_score = name_match_score(combined_query, choice)
    spec_score = name_match_score(spec_norm, choice)
    if _tz_name_is_category_only(tz_item) and spec_required_tokens(tz_item):
        return max(combined_score, spec_score)
    return max(combined_score, name_score, spec_score)


def relevance_score(tz_item: TZItem, matched_name: str) -> float:
    return combined_match_score(tz_item, matched_name)


def is_relevant_match(
    tz_item: TZItem,
    matched_name: str,
    *,
    min_score: float = LOCAL_MATCH_THRESHOLD,
    score: float | None = None,
) -> bool:
    if not matched_name:
        return False
    if product_type_conflict(tz_item, matched_name):
        return False
    if _category_conflict(tz_item, matched_name):
        return False

    spec_line = primary_spec_line(tz_item.specifications)
    has_spec = has_meaningful_spec(tz_item)
    choice = normalize_name(matched_name)
    name_score = name_match_score(normalize_name(tz_item.name), choice)
    combined_score = combined_match_score(tz_item, matched_name)
    if score is not None:
        combined_score = min(float(score), combined_score)

    required = spec_required_tokens(tz_item)
    identity = spec_identity_tokens(tz_item)
    spec_bound_match = _tz_name_is_category_only(tz_item) and bool(identity)
    strong_spec_match = bool(
        required and len(required) >= 2 and _required_tokens_present(required, matched_name)
    )
    strong_name_match = name_score >= LOCAL_MATCH_THRESHOLD and not spec_bound_match

    if spec_bound_match and not _identity_tokens_present(identity, matched_name):
        spec_score = name_match_score(normalize_name(spec_line), choice)
        if spec_score < min_score:
            return False

    if (
        has_spec
        and not strong_name_match
        and not _name_anchors_satisfied(tz_item, matched_name)
        and not strong_spec_match
    ):
        return False

    if required and not strong_name_match and not _required_tokens_present(
        required, matched_name
    ):
        return False

    if has_spec:
        if combined_score >= min_score:
            return True
        spec_score = name_match_score(normalize_name(spec_line), choice)
        if spec_score >= min_score:
            return True
        if required:
            return False
        return combined_score >= EXACT_MATCH_THRESHOLD

    if name_score >= min_score:
        return True
    return combined_score >= min_score
