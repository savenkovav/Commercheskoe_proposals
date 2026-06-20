from __future__ import annotations

import io
import logging
import re
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

SUPPORTED_TZ_EXTENSIONS = {".docx", ".pdf", ".xlsx", ".xls"}
SUPPORTED_TZ_LABEL = ".docx, .pdf, .xlsx, .xls"


@dataclass
class _ColumnMap:
    number: int
    name: int
    unit: int
    qty: int
    specs: int | None = None


def parse_tz(path: Path) -> list[TZItem]:
    suffix = path.suffix.lower()
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
        return ".xls"
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


def _build_column_map(header_cells: list[str]) -> _ColumnMap | None:
    lower = [c.strip().lower() for c in header_cells]
    if not any("наимен" in cell for cell in lower):
        return None

    name_idx = next(i for i, cell in enumerate(lower) if "наимен" in cell)

    number_idx = 0
    for i, cell in enumerate(lower):
        if cell in {"№", "п/п", "пп", "n", "no"} or cell.startswith("№") or "номер" in cell:
            number_idx = i
            break

    unit_idx = name_idx + 1
    for i, cell in enumerate(lower):
        if "ед" in cell and ("изм" in cell or cell.startswith("ед")):
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

    return _ColumnMap(number_idx, name_idx, unit_idx, qty_idx, specs_idx)


def _parse_row(cells: list[str], col: _ColumnMap) -> TZItem | None:
    max_idx = max(col.number, col.name, col.unit, col.qty)
    if len(cells) <= max_idx:
        return None

    number_raw = cells[col.number].rstrip(".")
    if not number_raw.isdigit():
        return None

    name = cells[col.name]
    if not name:
        return None

    unit = cells[col.unit] or "шт."
    qty_raw = cells[col.qty].replace(",", ".")
    specs = cells[col.specs] if col.specs is not None and len(cells) > col.specs else ""

    try:
        quantity = float(qty_raw)
    except ValueError:
        quantity = 1.0

    return TZItem(
        number=int(number_raw),
        name=name,
        unit=unit,
        quantity=quantity,
        specifications=specs,
    )


def _parse_tz_tables(tables: Iterable[list[list[str]]]) -> list[TZItem]:
    items: list[TZItem] = []

    for table in tables:
        if len(table) < 2:
            continue

        header_cells = [_cell_str(cell) for cell in table[0]]
        col = _build_column_map(header_cells)
        if not col:
            continue

        for row in table[1:]:
            cells = [_cell_str(cell) for cell in row]
            item = _parse_row(cells, col)
            if item:
                items.append(item)

    return items


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
        rows = [header]
        idx += 1

        while idx < len(lines):
            line = lines[idx]
            if "наимен" in line.lower() and rows:
                break
            parts = _split_table_line(line)
            if _looks_like_data_row(parts):
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


def _looks_like_data_row(parts: list[str]) -> bool:
    if len(parts) < 3:
        return False
    number = parts[0].rstrip(".")
    return number.isdigit() and bool(parts[1])
