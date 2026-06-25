from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import xlrd
from docx import Document
from openpyxl import load_workbook

from src.config import TZ_OCR_LANG, TZ_PDF_OCR_ENABLED, TESSERACT_CMD
from src.services.models import TZItem

logger = logging.getLogger(__name__)

SUPPORTED_TZ_EXTENSIONS = {".doc", ".docx", ".pdf", ".xlsx", ".xls"}
SUPPORTED_TZ_LABEL = ".doc, .docx, .pdf, .xlsx, .xls"

_PRODUCT_UNITS = frozenset(
    {
        "шт",
        "шт.",
        "штука",
        "штук",
        "компл",
        "компл.",
        "комплект",
        "упак",
        "упак.",
        "упаковка",
        "м",
        "м.",
        "метр",
        "кг",
        "кг.",
        "л",
        "л.",
        "т",
        "т.",
        "пара",
        "пар",
        "набор",
    }
)


@dataclass
class _ColumnMap:
    number: int
    name: int
    unit: int
    qty: int
    specs: int | None = None
    country: int | None = None
    has_number: bool = True


def parse_tz(path: Path) -> list[TZItem]:
    suffix = path.suffix.lower()
    if suffix == ".doc":
        return _parse_tz_doc(path)
    if suffix == ".docx":
        return _parse_tz_docx(path)
    if suffix == ".pdf":
        return _parse_tz_pdf(path)
    if suffix in {".xlsx", ".xls"}:
        return _parse_tz_excel(path)
    raise ValueError(
        f"Неподдерживаемый формат ТЗ: {suffix}. "
        f"Допустимые форматы: {SUPPORTED_TZ_LABEL}"
    )


def extract_tz_document_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".doc":
        return _extract_doc_text(path)
    if suffix == ".docx":
        return _extract_docx_text(path)
    if suffix == ".pdf":
        return _extract_pdf_document_text(path)
    if suffix in {".xlsx", ".xls"}:
        return _extract_excel_text(path)
    raise ValueError(
        f"Неподдерживаемый формат ТЗ: {suffix}. "
        f"Допустимые форматы: {SUPPORTED_TZ_LABEL}"
    )


def detect_tz_suffix(content: bytes, content_type: str | None = None) -> str | None:
    if content.startswith(b"%PDF"):
        return ".pdf"
    if content.startswith(b"\xd0\xcf\x11\xe0"):
        return _detect_ole_suffix(content)
    if content.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                if any(name.startswith("word/") for name in names):
                    return ".docx"
                if any(name.startswith("xl/") for name in names):
                    return ".xlsx"
        except zipfile.BadZipFile:
            pass

    if content_type:
        lowered = content_type.lower()
        if "pdf" in lowered:
            return ".pdf"
        if "spreadsheet" in lowered or "excel" in lowered:
            return ".xlsx"
        if "word" in lowered or "document" in lowered:
            return ".docx"

    return None


def resolve_tz_upload_filename(
    filename: str | None,
    content: bytes,
    content_type: str | None = None,
) -> str:
    name = (filename or "tz").strip() or "tz"
    suffix = Path(name).suffix.lower()
    if suffix in SUPPORTED_TZ_EXTENSIONS:
        return name

    detected = detect_tz_suffix(content, content_type)
    if detected:
        stem = Path(name).stem or "tz"
        return f"{stem}{detected}"

    raise ValueError(f"Загрузите файл ТЗ: {SUPPORTED_TZ_LABEL}")


def _cell_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


_HEADER_NAME_TOKENS = frozenset(
    {
        "наименование",
        "товара",
        "характеристики",
        "характеристика",
        "характеристи",
        "характери",
        "единиц",
        "единица",
        "измерения",
        "измерение",
        "кол-во",
        "количество",
        "страны",
        "страна",
        "происхождения",
        "значение",
        "п/п",
        "итого",
        "стики",
        "ние",
    }
)


def _normalize_tz_product_name(name: str) -> str:
    cleaned = name.strip().strip("|").strip()
    cleaned = re.sub(r"^\d+\s*\|", "", cleaned).strip()
    cleaned = cleaned.rstrip("|").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _is_valid_tz_product_name(name: str) -> bool:
    cleaned = _normalize_tz_product_name(name)
    if len(cleaned) < 4:
        return False

    lower = cleaned.lower()
    if lower in _HEADER_NAME_TOKENS:
        return False

    words = [word for word in re.split(r"[\s|]+", lower) if word]
    if words and all(word in _HEADER_NAME_TOKENS for word in words):
        return False
    if len(words) == 1:
        if words[0] in _HEADER_NAME_TOKENS or len(cleaned) < 5:
            return False
    elif len(words) < 2 and len(cleaned) < 12:
        return False
    if re.search(r"(?:федерация|российск)", lower):
        return False
    if lower in {"мпиксель", "крат", "прямой", "верхнее", "нижнее", "монокулярный", "светодиод", "cmos", "usb"}:
        return False
    if cleaned.count("|") >= 2 and len(cleaned) < 40:
        return False
    if not re.search(r"[а-яa-zё]{3,}", lower):
        return False
    if any(
        token in lower
        for token in (
            "госпрограмма",
            "зарница",
            "купить учебное",
            "основной перечень",
        )
    ):
        return False

    return True


def _is_tz_table_header(header_cells: list[str]) -> bool:
    joined = " ".join(cell.strip().lower() for cell in header_cells if cell.strip())
    if "наимен" not in joined:
        return False
    if "товар" not in joined:
        return False
    return _build_column_map(header_cells) is not None


_EIS_KNOWN_CHARACTERISTICS: tuple[str, ...] = (
    "Конструкционные особенности",
    "Строение оптической схемы",
    "Максимальное увеличение",
    "Разрешение камеры",
    "Расположение осветителя",
    "Разъем входа/выхода",
    "Способ наблюдения",
    "Тип осветителя",
    "Тип матрицы",
    "Питание",
)


def _eis_characteristics_pattern() -> str:
    return "|".join(
        re.escape(header)
        for header in sorted(_EIS_KNOWN_CHARACTERISTICS, key=len, reverse=True)
    )


def _is_eis_tz_document(text: str) -> bool:
    lower = text.lower()
    if "наимен" not in lower and "техническое задание" not in lower:
        return False
    if "характер" not in lower and "осветител" not in lower and "микроскоп" not in lower:
        return False
    if "\x07" in text and re.search(r"\d+\x07", text):
        return True
    if re.search(r"\d+\s*\|", text):
        return True
    if "\t" in text and re.search(r"\d+\t", text):
        return True
    if re.search(r"\b1\s+[А-Яа-яA-Za-z«»]", text) and re.search(
        r"\bШТ\s*\d+", text, re.IGNORECASE
    ):
        return True
    if re.search(r"\b1[А-Яа-яA-Za-z«»]", text) and re.search(
        r"ШТ\s*\d+", text, re.IGNORECASE
    ):
        return True
    return False


def _clean_eis_char_value(raw: str) -> str:
    cleaned = re.sub(
        r"\b(ШТ|ШТ\.|компл|упак)\s*\d+\s*(?:Российская Федерация)?",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_readable_spec_fragment(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    if "\x00" in cleaned:
        return False
    if re.search(r"[\u0900-\u0dff\u0e00-\u0eff\uf000-\uffff]", cleaned):
        return False
    if re.fullmatch(r"[\d.,]+", cleaned):
        return True
    letters = len(re.findall(r"[А-Яа-яA-Za-zЁё]", cleaned))
    return letters >= max(2, len(cleaned) // 4)


def _is_eis_characteristic_header(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    lower = cleaned.lower()
    for header in _EIS_KNOWN_CHARACTERISTICS:
        if lower == header.lower():
            return True
    return _looks_like_characteristic_name(cleaned)


def _sanitize_eis_delimited_text(text: str) -> str:
    if "\x07" not in text:
        return text
    text = text.replace("\x00", "")
    text = re.sub(
        r"[^\w\s«»\-.,/():;№%\"'А-Яа-яЁё\x07]+",
        "\x07",
        text,
    )
    return re.sub(r"\x07{4,}", "\x07\x07", text)


def _extract_eis_specifications(segment: str) -> list[str]:
    pattern = re.compile(
        rf"({_eis_characteristics_pattern()})",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(segment))
    spec_lines: list[str] = []
    for index, match in enumerate(matches):
        header = match.group(1).strip()
        value_start = match.end()
        value_end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(segment)
        )
        value = _clean_eis_char_value(segment[value_start:value_end])
        if value and _is_readable_spec_fragment(value):
            spec_lines.append(f"{header}: {value}")
    return spec_lines


def _eis_data_section(text: str) -> str:
    normalized = _normalize_eis_doc_text(text)
    lower = normalized.lower()
    data_start = 0
    for marker in (
        "единица измерения характеристики",
        "наименование характеристики",
        "кол-во товара",
        "наименование страны происхождения товара",
    ):
        idx = lower.find(marker)
        if idx >= 0:
            data_start = max(data_start, idx + len(marker))
    return normalized[data_start:].strip() or normalized


def _parse_zakupki_concatenated_text(text: str) -> list[TZItem]:
    if not _is_eis_tz_document(text):
        return []

    scan = _eis_data_section(text)
    if not scan:
        return []

    headers_alt = _eis_characteristics_pattern()
    items: list[TZItem] = []
    item_pattern = re.compile(
        rf"\b(\d+)\s+([А-Яа-яA-Za-z«»\"][^|]+?)(?=\s+(?:{headers_alt}))",
        re.IGNORECASE,
    )
    for match in item_pattern.finditer(scan):
        number = int(match.group(1))
        if number <= 0 or number > 200:
            continue

        name = _normalize_tz_product_name(match.group(2))
        if not _is_valid_tz_product_name(name):
            continue

        tail = scan[match.start() :]
        segment_end = len(tail)
        itogo = re.search(r"\bИТОГО\b", tail, re.IGNORECASE)
        if itogo:
            segment_end = itogo.start()

        for later in item_pattern.finditer(tail[match.end() - match.start() :]):
            if later.start() <= 0:
                continue
            later_number = int(later.group(1))
            later_name = _normalize_tz_product_name(later.group(2))
            if later_number <= 0 or later_number > 200:
                continue
            if not _is_valid_tz_product_name(later_name):
                continue
            later_tail = tail[match.end() - match.start() + later.start() :]
            if re.search(r"\bШТ\s*\d+", later_tail[:400], re.IGNORECASE):
                segment_end = min(segment_end, match.end() - match.start() + later.start())
                break

        segment = tail[:segment_end]

        if not re.search(r"\bШТ\s*\d+", segment, re.IGNORECASE):
            continue

        unit = "шт."
        quantity = 1.0
        country = ""
        meta = re.search(
            r"\b(ШТ|ШТ\.|компл|упак)\s*(\d+(?:[.,]\d+)?)\s*(Российская Федерация)?",
            segment,
            re.IGNORECASE,
        )
        if meta:
            unit = meta.group(1)
            quantity = float(meta.group(2).replace(",", "."))
            if meta.group(3):
                country = meta.group(3).strip()

        spec_lines = _extract_eis_specifications(segment)
        if not spec_lines:
            continue

        items.append(
            TZItem(
                number=number,
                name=name,
                unit=unit,
                quantity=quantity,
                specifications="; ".join(spec_lines),
                country_of_origin=country,
            )
        )

    return items


def _build_column_map(header_cells: list[str]) -> _ColumnMap | None:
    lower = [c.strip().lower() for c in header_cells]
    if not any("наимен" in cell for cell in lower):
        return None

    country_idx: int | None = None
    for i, cell in enumerate(lower):
        if "стран" in cell or "происхожд" in cell:
            country_idx = i
            break

    name_idx: int | None = None
    for i, cell in enumerate(lower):
        if "наимен" not in cell:
            continue
        if "стран" in cell or "происхожд" in cell:
            continue
        name_idx = i
        break
    if name_idx is None:
        return None

    number_idx = 0
    has_number = False
    for i, cell in enumerate(lower):
        if cell in {"№", "п/п", "пп", "n", "no"} or cell.startswith("№"):
            number_idx = i
            has_number = True
            break
        if "номер" in cell and "товар" not in cell and "стран" not in cell:
            number_idx = i
            has_number = True
            break

    unit_idx = name_idx + 1
    for i, cell in enumerate(lower):
        if ("ед" in cell and ("изм" in cell or cell.startswith("ед"))) or "единиц" in cell:
            unit_idx = i
            break

    qty_idx = name_idx + 2
    for i, cell in enumerate(lower):
        if "кол" in cell or "колич" in cell:
            qty_idx = i
            break

    specs_idx: int | None = None
    for i, cell in enumerate(lower):
        if any(key in cell for key in ("характер", "описан", "функцион", "техн")):
            specs_idx = i
            break

    return _ColumnMap(
        number_idx,
        name_idx,
        unit_idx,
        qty_idx,
        specs_idx,
        country_idx,
        has_number,
    )


def _parse_row(
    cells: list[str],
    col: _ColumnMap,
    *,
    fallback_number: int | None = None,
) -> TZItem | None:
    indices = [col.name, col.unit, col.qty]
    if col.specs is not None:
        indices.append(col.specs)
    if col.country is not None:
        indices.append(col.country)
    max_idx = max(indices)
    if len(cells) <= max_idx:
        return None

    number: int | None = None
    if col.has_number:
        number_raw = cells[col.number].rstrip(".")
        if number_raw.isdigit():
            number = int(number_raw)
        elif fallback_number is not None:
            number = fallback_number
        else:
            return None
    elif fallback_number is not None:
        number = fallback_number
    else:
        return None

    name = _normalize_tz_product_name(cells[col.name])
    if not _is_valid_tz_product_name(name):
        return None

    unit = cells[col.unit] or "шт."
    qty_raw = cells[col.qty].replace(",", ".")
    specs = cells[col.specs] if col.specs is not None and len(cells) > col.specs else ""
    country = (
        cells[col.country]
        if col.country is not None and len(cells) > col.country
        else ""
    )

    try:
        quantity = float(qty_raw)
    except ValueError:
        quantity = 1.0

    return TZItem(
        number=number,
        name=name,
        unit=unit,
        quantity=quantity,
        specifications=specs,
        country_of_origin=country,
    )


def _parse_tz_tables(tables: Iterable[list[list[str]]]) -> list[TZItem]:
    items: list[TZItem] = []

    for table in tables:
        if len(table) < 2:
            continue

        header_cells = [_cell_str(cell) for cell in table[0]]
        if not _is_tz_table_header(header_cells):
            continue
        col = _build_column_map(header_cells)
        if not col:
            continue

        auto_number = 0
        for row in table[1:]:
            cells = [_cell_str(cell) for cell in row]
            if not any(cells):
                continue
            item: TZItem | None = None
            if col.has_number:
                item = _parse_row(cells, col)
            else:
                auto_number += 1
                item = _parse_row(cells, col, fallback_number=auto_number)
            if item:
                items.append(item)

    return items


def _detect_ole_suffix(content: bytes) -> str:
    if b"Workbook" in content and b"MSWord" not in content and b"WordDocument" not in content:
        return ".xls"
    if b"MSWord" in content or b"WordDocument" in content:
        return ".doc"
    if b"Word" in content and b"Excel" not in content[:8192]:
        return ".doc"
    return ".xls"


def _extract_doc_text_from_ole_utf16(path: Path) -> str:
    """Извлекает текст из старого .doc напрямую из UTF-16LE-потока (без textutil/antiword)."""
    data = path.read_bytes()
    if not data.startswith(b"\xd0\xcf\x11\xe0"):
        return ""

    start = -1
    for label in ("Техническое задание", "Наименование", "наименование"):
        idx = data.find(label.encode("utf-16le"))
        if idx >= 0:
            start = idx if start < 0 else min(start, idx)
    if start < 0:
        return ""
    if start % 2 == 1:
        start += 1

    end = -1
    for end_label in ("ИТОГО",):
        idx = data.find(end_label.encode("utf-16le"), start)
        if idx >= 0:
            end = idx + len(end_label.encode("utf-16le")) + 400
            break
    if end < 0:
        end = min(len(data), start + 80000)

    blob = data[start:end]
    if len(blob) % 2 == 1:
        blob = blob[:-1]
    return blob.decode("utf-16le", errors="ignore").strip()


def _score_doc_extraction_text(text: str) -> int:
    if not text.strip():
        return 0
    lower = text.lower()
    score = len(re.findall(r"[а-яё]", lower))
    if "\x07" in text:
        score += 50
    if "наимен" in lower:
        score += 20
    if re.search(r"шт\s*\d+", lower):
        score += 20
    if "характер" in lower:
        score += 10
    return score


def _normalize_eis_doc_text(text: str) -> str:
    normalized = text.replace("\f", " ")
    normalized = re.sub(r"[\r\n\t]+", " ", normalized)
    normalized = re.sub(r"\s*\|\s*", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _parse_zakupki_loose_text(text: str) -> list[TZItem]:
    normalized = _normalize_eis_doc_text(text)
    if not normalized:
        return []

    spec_lines = _extract_eis_specifications(normalized)
    if len(spec_lines) < 1:
        return []

    char_pattern = re.compile(rf"({_eis_characteristics_pattern()})", re.IGNORECASE)
    first_char = char_pattern.search(normalized)
    if not first_char:
        return []

    prefix = normalized[: first_char.start()]
    name: str | None = None
    number = 1
    for match in reversed(
        list(
            re.finditer(
                r"(?:^|[\s\x07])(\d+)\s*[\.\):\-–—]?\s*"
                r"([А-Яа-яA-Za-z«»\"][\w\s«»\-]{4,})",
                prefix,
                re.IGNORECASE,
            )
        )
    ):
        candidate_number = int(match.group(1))
        if candidate_number <= 0 or candidate_number > 200:
            continue
        candidate_name = _normalize_tz_product_name(match.group(2))
        if not _is_valid_tz_product_name(candidate_name):
            continue
        number = candidate_number
        name = candidate_name
        break

    if not name:
        return []

    unit = "шт."
    quantity = 1.0
    country = ""
    meta = re.search(
        r"\b(ШТ|ШТ\.|компл|упак)\s*(\d+(?:[.,]\d+)?)\s*(Российская Федерация)?",
        normalized,
        re.IGNORECASE,
    )
    if meta:
        unit = meta.group(1)
        quantity = float(meta.group(2).replace(",", "."))
        if meta.group(3):
            country = meta.group(3).strip()

    return [
        TZItem(
            number=number,
            name=name,
            unit=unit,
            quantity=quantity,
            specifications="; ".join(spec_lines),
            country_of_origin=country,
        )
    ]


def _extract_doc_text(path: Path) -> str:
    errors: list[str] = []
    candidates: list[str] = []

    ole_text = _extract_doc_text_from_ole_utf16(path)
    if ole_text:
        candidates.append(ole_text)

    if shutil.which("textutil"):
        try:
            candidates.append(
                subprocess.check_output(
                    ["textutil", "-convert", "txt", "-stdout", str(path)],
                    stderr=subprocess.STDOUT,
                ).decode("utf-8", errors="replace")
            )
        except subprocess.CalledProcessError as exc:
            errors.append(f"textutil: {exc}")

    if shutil.which("antiword"):
        for args in (
            ["-m", "UTF-8.txt"],
            ["-m", "UTF-8.txt", "-w", "0"],
            [],
        ):
            try:
                raw = subprocess.check_output(
                    ["antiword", *args, str(path)],
                    stderr=subprocess.STDOUT,
                )
                candidates.append(raw.decode("utf-8", errors="replace"))
                for encoding in ("cp1251", "cp866"):
                    candidates.append(raw.decode(encoding, errors="replace"))
            except subprocess.CalledProcessError as exc:
                errors.append(f"antiword{' '.join(args)}: {exc}")

    if shutil.which("soffice"):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(
                    [
                        "soffice",
                        "--headless",
                        "--convert-to",
                        "txt:Text",
                        "--outdir",
                        tmp,
                        str(path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    errors.append(f"soffice: {result.stderr.strip() or result.stdout.strip()}")
                else:
                    txt_path = Path(tmp) / f"{path.stem}.txt"
                    if txt_path.exists():
                        candidates.append(
                            txt_path.read_text(encoding="utf-8", errors="replace")
                        )
        except OSError as exc:
            errors.append(f"soffice: {exc}")

    candidates = [text for text in candidates if text and text.strip()]
    if candidates:
        return max(candidates, key=_score_doc_extraction_text)

    details = f" ({'; '.join(errors)})" if errors else ""
    raise ValueError(
        "Не удалось прочитать .doc. Сохраните файл как .docx "
        f"или установите LibreOffice/antiword.{details}"
    )


def _parse_tz_doc(path: Path) -> list[TZItem]:
    text = _extract_doc_text(path)
    items = _parse_zakupki_doc_stream(text)
    if items:
        return items

    items = _parse_zakupki_loose_text(text)
    if items:
        return items

    items = _parse_tz_tables(_tables_from_text(text))
    if items:
        return items

    raise ValueError(
        "Не удалось извлечь позиции из .doc. "
        "Убедитесь, что в файле есть таблица с колонкой «Наименование товара»."
    )


def _is_product_unit(value: str) -> bool:
    normalized = value.strip().lower().rstrip(".")
    if normalized in _PRODUCT_UNITS:
        return True
    upper = value.strip().upper()
    return upper in {"ШТ", "КОМПЛ", "УПАК"}


def _parse_quantity(value: str) -> float | None:
    raw = value.strip().replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _looks_like_characteristic_name(value: str) -> bool:
    lower = value.lower()
    keywords = (
        "располож",
        "тип",
        "способ",
        "размер",
        "максим",
        "миним",
        "конструк",
        "разреш",
        "разъем",
        "питание",
        "строение",
        "матриц",
        "характер",
        "назнач",
        "мощност",
        "диаметр",
        "длина",
        "ширин",
        "высот",
        "вес",
        "объем",
        "емкост",
        "частот",
        "напряж",
    )
    return any(keyword in lower for keyword in keywords)


def _parse_zakupki_item_parts(parts: list[str]) -> TZItem | None:
    if len(parts) < 2:
        return None

    number_raw = parts[0].rstrip(".")
    if not number_raw.isdigit():
        return None

    name = _normalize_tz_product_name(parts[1])
    if not _is_valid_tz_product_name(name):
        return None

    unit = "шт."
    quantity = 1.0
    country = ""
    spec_lines: list[str] = []
    product_meta_found = False

    idx = 2
    while idx < len(parts):
        part = parts[idx].strip()
        if not part:
            idx += 1
            continue
        if part.upper().startswith("ИТОГО"):
            break

        if not product_meta_found and _is_product_unit(part):
            qty = _parse_quantity(parts[idx + 1]) if idx + 1 < len(parts) else None
            if qty is not None:
                unit = part.strip() or "шт."
                quantity = qty
                idx += 2
                if idx < len(parts):
                    candidate = parts[idx].strip()
                    if (
                        candidate
                        and not _is_product_unit(candidate)
                        and _parse_quantity(candidate) is None
                        and (
                            not _looks_like_characteristic_name(candidate)
                            or len(candidate) > 15
                        )
                    ):
                        country = candidate
                        idx += 1
                product_meta_found = True
                continue

        if (
            idx + 1 < len(parts)
            and _is_eis_characteristic_header(part)
            and _is_readable_spec_fragment(part)
        ):
            value = parts[idx + 1].strip()
            if (
                value
                and not value.upper().startswith("ИТОГО")
                and _is_readable_spec_fragment(value)
            ):
                spec_value = value
                step = 2
                if idx + 2 < len(parts):
                    char_unit = parts[idx + 2].strip()
                    if (
                        char_unit
                        and not _is_product_unit(char_unit)
                        and _parse_quantity(char_unit) is None
                        and len(char_unit) <= 20
                        and not _looks_like_characteristic_name(char_unit)
                        and _is_readable_spec_fragment(char_unit)
                    ):
                        spec_value = f"{value} {char_unit}"
                        step = 3
                spec_lines.append(f"{part}: {spec_value}")
                idx += step
                continue

        idx += 1

    if not spec_lines and not product_meta_found:
        return None

    return TZItem(
        number=int(number_raw),
        name=name,
        unit=unit,
        quantity=quantity,
        specifications="; ".join(spec_lines),
        country_of_origin=country,
    )


def _iter_zakupki_product_matches(
    text: str,
    delimiter: str,
) -> list[re.Match[str]]:
    if delimiter == "\x07":
        if "\x07" not in text:
            return []
        item_pattern = re.compile(
            r"(?:^|[\n\r]|\x07)(\d+)\x07"
            r"([А-Яа-яA-Za-z«»\"][^\x07\n\r]{2,})",
            re.IGNORECASE,
        )
    elif delimiter == "|":
        if not re.search(r"\d+\s*\|", text) and not re.search(r"\|\s*\d+\s*\|", text):
            return []
        item_pattern = re.compile(
            r"(?:^|[\n\r]|\|)\s*(\d+)\s*\|"
            r"([А-Яа-яA-Za-z«»\"][^|\n\r]{2,})",
            re.IGNORECASE,
        )
    else:
        if delimiter not in text:
            return []
        item_pattern = re.compile(
            rf"(?:^|[\n\r]|\|)\s*(\d+)\s*{re.escape(delimiter)}"
            rf"([А-Яа-яA-Za-z«»\"][^{re.escape(delimiter)}\n\r]{{2,}})",
            re.IGNORECASE,
        )

    matches: list[re.Match[str]] = []
    for match in item_pattern.finditer(text):
        number = int(match.group(1))
        if number <= 0 or number > 200:
            continue
        name = _normalize_tz_product_name(match.group(2))
        if not _is_valid_tz_product_name(name):
            continue
        matches.append(match)
    return matches


def _parse_zakupki_delimited_stream(text: str, delimiter: str) -> list[TZItem]:
    if delimiter == "\x07":
        split_delimiter = "\x07"
    elif delimiter == "|":
        split_delimiter = "|"
    else:
        split_delimiter = delimiter

    row_matches = _iter_zakupki_product_matches(text, delimiter)
    if not row_matches:
        return []

    items: list[TZItem] = []
    for index, match in enumerate(row_matches):
        tail = text[match.start(1) :]
        segment_end = len(tail)
        itogo = re.search(r"\bИТОГО\b", tail, re.IGNORECASE)
        if itogo:
            segment_end = itogo.start()
        if index + 1 < len(row_matches):
            next_start = row_matches[index + 1].start(1) - match.start(1)
            if next_start > 0:
                segment_end = min(segment_end, next_start)

        parts = [part.strip() for part in tail[:segment_end].split(split_delimiter)]
        item = _parse_zakupki_item_parts(parts)
        if item:
            items.append(item)

    return items


def _zakupki_items_complete(items: list[TZItem], source_text: str) -> bool:
    if not items:
        return False
    for item in items:
        if not item.specifications or len(item.specifications) < 40:
            return False
        if item.quantity <= 1 and re.search(r"\bШТ\s+[2-9]\d", source_text, re.IGNORECASE):
            return False
    return True


def _parse_zakupki_doc_stream(text: str) -> list[TZItem]:
    if not _is_eis_tz_document(text):
        return []

    if "\x07" in text:
        text = _sanitize_eis_delimited_text(text)

    best_delimited: list[TZItem] = []
    for delimiter in ("\x07", "|", "\t"):
        items = _parse_zakupki_delimited_stream(text, delimiter)
        if items:
            if _zakupki_items_complete(items, text):
                return items
            best_delimited = items

    items = _parse_zakupki_concatenated_text(text)
    if items:
        return items

    items = _parse_zakupki_loose_text(text)
    if items:
        return items

    return best_delimited


def _parse_zakupki_doc_cell_stream(text: str) -> list[TZItem]:
    return _parse_zakupki_doc_stream(text)


def _parse_tz_docx(path: Path) -> list[TZItem]:
    doc = Document(str(path))
    tables: list[list[list[str]]] = []

    for table in doc.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        tables.append(rows)

    return _parse_tz_tables(tables)


def _extract_docx_text(path: Path) -> str:
    doc = Document(str(path))
    chunks: list[str] = []
    chunks.extend(p.text.strip() for p in doc.paragraphs if p.text.strip())
    for table in doc.tables:
        for row in table.rows:
            values = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if values:
                chunks.append(" | ".join(values))
    return "\n".join(chunks)


def _parse_tz_excel(path: Path) -> list[TZItem]:
    suffix = path.suffix.lower()
    sheets: list[list[list[str]]] = []

    if suffix == ".xlsx":
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            sheet_rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                sheet_rows.append([_cell_str(cell) for cell in row])
            if sheet_rows:
                sheets.append(sheet_rows)
        wb.close()
    else:
        wb = xlrd.open_workbook(str(path))
        for sheet_name in wb.sheet_names():
            ws = wb.sheet_by_name(sheet_name)
            sheet_rows = [
                [_cell_str(cell) for cell in ws.row_values(row_idx)]
                for row_idx in range(ws.nrows)
            ]
            if sheet_rows:
                sheets.append(sheet_rows)

    tables = [_table_from_sheet_rows(sheet) for sheet in sheets]
    tables = [table for table in tables if table]
    return _parse_tz_tables(tables)


def _extract_excel_text(path: Path) -> str:
    suffix = path.suffix.lower()
    lines: list[str] = []
    if suffix == ".xlsx":
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            lines.append(f"# Лист: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                values = [_cell_str(cell) for cell in row if _cell_str(cell)]
                if values:
                    lines.append(" | ".join(values))
        wb.close()
    else:
        wb = xlrd.open_workbook(str(path))
        for sheet_name in wb.sheet_names():
            ws = wb.sheet_by_name(sheet_name)
            lines.append(f"# Лист: {sheet_name}")
            for row_idx in range(ws.nrows):
                values = [_cell_str(cell) for cell in ws.row_values(row_idx)]
                values = [value for value in values if value]
                if values:
                    lines.append(" | ".join(values))
    return "\n".join(lines)


def _table_from_sheet_rows(sheet_rows: list[list[str]]) -> list[list[str]] | None:
    for idx, row in enumerate(sheet_rows):
        if _build_column_map(row):
            return sheet_rows[idx:]
    return None


def _parse_tz_pdf(path: Path) -> list[TZItem]:
    items = _parse_pdf_with_pdfplumber(path)
    if items:
        return items

    text = _extract_pdf_text(path)
    items = _parse_tz_tables(_tables_from_text(text))
    if items:
        return items

    if TZ_PDF_OCR_ENABLED:
        ocr_text = _ocr_pdf(path)
        items = _parse_tz_tables(_tables_from_text(ocr_text))
        if items:
            return items

    raise ValueError(
        "Не удалось извлечь таблицу позиций из PDF. "
        "Убедитесь, что есть колонка «Наименование». "
        "Для сканов установите Tesseract и включите TZ_PDF_OCR_ENABLED=true."
    )


def _extract_pdf_document_text(path: Path) -> str:
    text = _extract_pdf_text(path)
    if text.strip():
        return text
    if TZ_PDF_OCR_ENABLED:
        return _ocr_pdf(path)
    return ""


def _parse_pdf_with_pdfplumber(path: Path) -> list[TZItem]:
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber не установлен — пропускаю извлечение таблиц из PDF")
        return []

    tables: list[list[list[str]]] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                cleaned = [[_cell_str(cell) for cell in row] for row in table if row]
                if cleaned:
                    tables.append(cleaned)

            page_text = page.extract_text() or ""
            if page_text:
                tables.extend(_tables_from_text(page_text))

    return _parse_tz_tables(tables)


def _extract_pdf_text(path: Path) -> str:
    try:
        import fitz
    except ImportError:
        logger.warning("pymupdf не установлен — пропускаю извлечение текста из PDF")
        return ""

    chunks: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            chunks.append(page.get_text("text"))
    return "\n".join(chunks)


def _configure_tesseract() -> None:
    if not TESSERACT_CMD:
        return
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def _ocr_pdf(path: Path) -> str:
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise ValueError(
            "Для OCR PDF установите зависимости: pymupdf, pytesseract, Pillow"
        ) from exc

    _configure_tesseract()
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        raise ValueError(
            "Tesseract OCR не найден. Установите: brew install tesseract tesseract-lang "
            "(macOS) или apt install tesseract-ocr tesseract-ocr-rus (Linux)."
        ) from exc

    texts: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            texts.append(pytesseract.image_to_string(img, lang=TZ_OCR_LANG))

    return "\n".join(texts)


def _tables_from_text(text: str) -> list[list[list[str]]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tables: list[list[list[str]]] = []
    idx = 0

    while idx < len(lines):
        if "наимен" not in lines[idx].lower():
            idx += 1
            continue

        header = _split_table_line(lines[idx])
        if not _is_tz_table_header(header):
            idx += 1
            continue
        col = _build_column_map(header)
        rows = [header]
        idx += 1

        while idx < len(lines):
            line = lines[idx]
            if "наимен" in line.lower() and rows:
                break
            parts = _split_table_line(line)
            has_number = col.has_number if col else True
            if _looks_like_data_row(parts, has_number=has_number):
                rows.append(parts)
                idx += 1
                continue
            if rows and len(rows) > 1:
                break
            idx += 1

        if len(rows) > 1:
            tables.append(rows)

    return tables


def _split_table_line(line: str) -> list[str]:
    if "\t" in line:
        return [part.strip() for part in line.split("\t") if part.strip()]
    parts = re.split(r"\s{2,}", line)
    if len(parts) >= 4:
        return [part.strip() for part in parts]
    return [part.strip() for part in line.split() if part.strip()]


def _looks_like_data_row(parts: list[str], *, has_number: bool = True) -> bool:
    if len(parts) < 2:
        return False
    if not has_number:
        return bool(parts[0].strip())
    number = parts[0].rstrip(".")
    return number.isdigit() and bool(parts[1])
