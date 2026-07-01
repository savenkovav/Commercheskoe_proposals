from __future__ import annotations

import re


_COMPOSITION_MARKERS = (
    r"состав\s+комплекта",
    r"в\s+состав\s+комплекта\s+вход",
    r"комплект\s+включает",
    r"в\s+комплект\s+вход",
)

_NUMBERED_LINE = re.compile(
    r"^\s*\d+[\).\:\-–—]\s*(.+?)\s*$",
    re.IGNORECASE,
)

_INLINE_NUMBERED = re.compile(
    r"\d+[\).\:\-–—]\s*([^;\d]+?)(?=\s*\d+[\).\:\-–—]|$)",
    re.IGNORECASE,
)


def parse_kit_components_from_specs(specifications: str) -> list[str]:
    """Извлекает перечень составляющих из текста характеристик ТЗ."""
    if not specifications or not specifications.strip():
        return []

    text = specifications.replace("\r\n", "\n").replace("\r", "\n")
    lower = text.lower()

    components: list[str] = []

    comp_section = re.search(r"состав\s+комплекта\s*:\s*", lower)
    if comp_section:
        tail = text[comp_section.end() :]
        for part in re.split(r",(?=\s*гипсовая\s+)", tail, flags=re.IGNORECASE):
            for chunk in re.split(r"[;\n]+", part):
                name = _clean_component_name(chunk)
                if _is_valid_component_name(name):
                    components.append(name)
        if components:
            return _dedupe(components)

    section = text
    for pattern in _COMPOSITION_MARKERS:
        match = re.search(pattern, lower)
        if match:
            section = text[match.end() :]
            break
    else:
        if "комплект" not in lower:
            return []

    for line in section.split("\n"):
        cleaned = line.strip()
        if not cleaned:
            continue
        if cleaned.endswith(";"):
            name = _clean_component_name(cleaned[:-1])
            if _is_valid_component_name(name):
                components.append(name)
                continue
        numbered = _NUMBERED_LINE.match(cleaned)
        if numbered:
            name = _clean_component_name(numbered.group(1))
            if _is_valid_component_name(name):
                components.append(name)

    if components:
        return _dedupe(components)

    inline = _INLINE_NUMBERED.findall(section.replace("\n", " "))
    components = [
        _clean_component_name(name)
        for name in inline
        if _is_valid_component_name(_clean_component_name(name))
    ]
    return _dedupe(components)


def is_kit_tz_item(name: str, specifications: str) -> bool:
    lower_name = name.lower()
    lower_specs = (specifications or "").lower()
    if "комплект" in lower_name:
        return True
    if any(re.search(marker, lower_specs) for marker in _COMPOSITION_MARKERS):
        return True
    return bool(parse_kit_components_from_specs(specifications))


def _clean_component_name(value: str) -> str:
    name = value.strip().strip(";.,")
    name = re.sub(r"\s+", " ", name)
    return name


_SKIP_COMPONENT_FRAGMENTS = (
    "комплект",
    "модели изготов",
    "технические характеристики",
    "окпд",
    "предназначен",
    "включает не менее",
    "включает следующие",
    "изготовлены из гипса",
)


def _is_valid_component_name(name: str) -> bool:
    if len(name) < 3:
        return False
    lower = name.lower()
    if re.match(r"^\d+[\.\)]", name):
        return False
    if any(fragment in lower for fragment in _SKIP_COMPONENT_FRAGMENTS):
        return False
    return True


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
