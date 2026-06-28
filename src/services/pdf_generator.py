from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import fitz

from src.config import (
    COMPANY_ADDRESS,
    COMPANY_DIRECTOR,
    COMPANY_INN,
    COMPANY_KPP,
    COMPANY_NAME,
    COMPANY_OGRN,
    DELIVERY_DAYS,
    DELIVERY_TERMS,
    KP_PDF_FONT_BOLD_PATH,
    KP_PDF_FONT_PATH,
    KP_STAMP_PATH,
    KP_TEMPLATES_DIR,
    KP_VAT_LABEL,
    PAYMENT_TERMS,
)
from src.services.excel_generator import _money
from src.services.markup_settings import get_markup_percent
from src.services.models import MatchResult, MatchStatus, ProposalSummary

logger = logging.getLogger(__name__)

PDF_FONT_REGULAR_NAME = "KpPdfRegular"
PDF_FONT_BOLD_NAME = "KpPdfBold"

PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN_X = 42
MARGIN_RIGHT = 553
TABLE_BOTTOM_Y = 760
PAGE_BOTTOM_MARGIN = 36
PAGE_MAX_Y = PAGE_HEIGHT - PAGE_BOTTOM_MARGIN
STAMP_DISPLAY_WIDTH = 150
SINGLE_PAGE_MAX_ITEMS = 20
SINGLE_PAGE_RELAXED_MAX_ITEMS = 16
SINGLE_PAGE_HEADER_RESERVE = 240

ROW_FONT_SIZE = 9
HEADER_FONT_SIZE = 9
LINE_HEIGHT = ROW_FONT_SIZE + 3
ROW_GAP = 5
HEADER_GAP_AFTER_LINE = 10
TOTAL_SEPARATOR_GAP_BEFORE = 4
TOTAL_SEPARATOR_GAP_AFTER_COMPACT = 16
TOTAL_SEPARATOR_GAP_AFTER = 18

# x0, x1 для колонок таблицы
TABLE_COLS = {
    "num": (MARGIN_X, MARGIN_X + 20),
    "name": (MARGIN_X + 22, MARGIN_X + 246),
    "unit": (MARGIN_X + 248, MARGIN_X + 276),
    "qty": (MARGIN_X + 278, MARGIN_X + 316),
    "price": (MARGIN_X + 318, MARGIN_X + 406),
    "sum": (MARGIN_X + 408, MARGIN_RIGHT),
}


def resolve_kp_stamp_image() -> Path | None:
    stamp_path = KP_STAMP_PATH
    if not stamp_path.exists():
        logger.warning("KP stamp image not found: %s", stamp_path)
        return None
    return stamp_path


def _stamp_display_size(stamp_path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(stamp_path) as stamp_image:
        stamp_w, stamp_h = stamp_image.size
    display_width = STAMP_DISPLAY_WIDTH
    display_height = max(80, int(display_width * stamp_h / max(stamp_w, 1)))
    return display_width, display_height


def _footer_block_height(
    *,
    compact: bool = False,
    stamp_height: float = 0,
) -> float:
    if compact:
        height = float(TOTAL_SEPARATOR_GAP_BEFORE) + float(TOTAL_SEPARATOR_GAP_AFTER_COMPACT)
        height += 11.0 + 4.0
        height += 4.0
        height += (10.0 + 4.0) * 3
        height += 6.0
        height += 9.0 + 4.0
        height += 8.0
        height += 11.0 + 4.0
        if stamp_height:
            height += 4.0 + stamp_height
        return height

    height = float(TOTAL_SEPARATOR_GAP_BEFORE) + float(TOTAL_SEPARATOR_GAP_AFTER)
    height += 11.0 + 6.0
    height += 8.0
    height += (10.0 + 6.0) * 3
    height += 10.0
    height += 9.0 + 6.0
    height += 18.0
    height += 11.0 + 6.0
    if stamp_height:
        height += 4.0 + stamp_height
    return height


def _estimate_table_body_height(
    results: list[MatchResult],
    font_regular: fitz.Font,
    *,
    row_font_size: float,
    line_height: float,
    row_gap: float,
) -> float:
    total = 0.0
    for result in results:
        name = result.tz_item.name
        if result.status == MatchStatus.SIMILAR:
            name = f"{name} *"
        elif result.status == MatchStatus.NOT_FOUND:
            name = f"{name} (не подобрано)"
        name_lines = _wrap_text(
            name,
            font_regular,
            row_font_size,
            _col_width("name"),
        )
        row_height = max(row_font_size + 2, len(name_lines) * line_height + 2)
        total += row_height + row_gap
    return total


def _tighten_single_page_layout(layout: dict[str, float | bool]) -> bool:
    row_font_size = float(layout["row_font_size"])
    row_gap = float(layout["row_gap"])
    if row_font_size <= 7.5 and row_gap <= 1.0:
        return False
    if row_font_size > 7.5:
        layout["row_font_size"] = max(7.5, row_font_size - 0.5)
        layout["line_height"] = float(layout["row_font_size"]) + 1.5
    if row_gap > 1.0:
        layout["row_gap"] = max(1.0, row_gap - 0.5)
    return True


def resolve_stamp_y(
    content_bottom_y: float,
    stamp_height: float,
    *,
    page_max_y: float = PAGE_MAX_Y,
    gap: float = 4.0,
) -> float:
    """Place stamp directly under footer text, not at the page bottom."""
    stamp_y = content_bottom_y + gap
    max_stamp_y = page_max_y - stamp_height
    return min(stamp_y, max_stamp_y)


def _standard_table_layout(*, single_page: bool) -> dict[str, float | bool]:
    return {
        "single_page": single_page,
        "row_font_size": float(ROW_FONT_SIZE),
        "line_height": float(LINE_HEIGHT),
        "row_gap": float(ROW_GAP),
        "compact_footer": single_page,
        "allow_tighten": False,
    }


def _single_page_layout(item_count: int) -> dict[str, float | bool]:
    if item_count > SINGLE_PAGE_MAX_ITEMS:
        return _standard_table_layout(single_page=False)

    if item_count <= SINGLE_PAGE_RELAXED_MAX_ITEMS:
        return _standard_table_layout(single_page=True)

    row_font_size = 8.5
    line_height = row_font_size + 2.0
    row_gap = 2.0

    return {
        "single_page": True,
        "row_font_size": row_font_size,
        "line_height": line_height,
        "row_gap": row_gap,
        "compact_footer": True,
        "allow_tighten": True,
    }


def _first_existing_path(candidates: list[Path]) -> Path | None:
    for path in candidates:
        if path and path.exists():
            return path
    return None


def resolve_pdf_font_paths() -> tuple[Path, Path]:
    bundled_regular = KP_TEMPLATES_DIR / "fonts" / "DejaVuSans.ttf"
    bundled_bold = KP_TEMPLATES_DIR / "fonts" / "DejaVuSans-Bold.ttf"
    regular = _first_existing_path(
        [
            KP_PDF_FONT_PATH,
            bundled_regular,
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/Library/Fonts/Arial.ttf"),
        ],
    )
    bold = _first_existing_path(
        [
            KP_PDF_FONT_BOLD_PATH,
            bundled_bold,
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
            Path("/Library/Fonts/Arial Bold.ttf"),
            regular,
        ],
    )
    if regular is None:
        raise FileNotFoundError(
            "Не найден TTF-шрифт с поддержкой кириллицы для PDF. "
            f"Положите DejaVuSans.ttf в {bundled_regular.parent}",
        )
    if bold is None:
        bold = regular
    return regular, bold


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _fmt_qty(value: float | int | None) -> str:
    if value is None:
        return "—"
    qty = float(value)
    if qty.is_integer():
        return str(int(qty))
    return f"{qty:g}".replace(".", ",")


def _col_width(col_key: str) -> float:
    x0, x1 = TABLE_COLS[col_key]
    return x1 - x0 - 2


def _wrap_text(text: str, font: fitz.Font, fontsize: float, max_width: float) -> list[str]:
    normalized = " ".join(str(text).split())
    if not normalized:
        return [""]

    lines: list[str] = []
    for word in normalized.split(" "):
        if font.text_length(word, fontsize=fontsize) <= max_width:
            if not lines:
                lines.append(word)
            else:
                candidate = f"{lines[-1]} {word}"
                if font.text_length(candidate, fontsize=fontsize) <= max_width:
                    lines[-1] = candidate
                else:
                    lines.append(word)
            continue

        if lines and lines[-1]:
            lines.append("")
        chunk = ""
        for char in word:
            candidate = f"{chunk}{char}"
            if font.text_length(candidate, fontsize=fontsize) <= max_width:
                chunk = candidate
            else:
                if chunk:
                    lines.append(chunk)
                chunk = char
        if chunk:
            if lines and lines[-1] == "":
                lines[-1] = chunk
            else:
                lines.append(chunk)

    return lines or [""]


def _draw_wrapped_cell(
    page: fitz.Page,
    col_key: str,
    row_top: float,
    text: str,
    *,
    fontname: str,
    font: fitz.Font,
    fontsize: float = ROW_FONT_SIZE,
    line_height: float | None = None,
) -> int:
    x0, x1 = TABLE_COLS[col_key]
    max_width = _col_width(col_key)
    lh = line_height if line_height is not None else LINE_HEIGHT
    lines = _wrap_text(text, font, fontsize, max_width)
    baseline = row_top + fontsize
    for index, line in enumerate(lines):
        page.insert_text(
            fitz.Point(x0, baseline + index * lh),
            line,
            fontsize=fontsize,
            fontname=fontname,
            color=(0, 0, 0),
        )
    return max(1, len(lines))


def _draw_single_cell(
    page: fitz.Page,
    col_key: str,
    row_top: float,
    text: str,
    *,
    fontname: str,
    fontsize: float = ROW_FONT_SIZE,
) -> None:
    x0, _ = TABLE_COLS[col_key]
    page.insert_text(
        fitz.Point(x0, row_top + fontsize),
        text,
        fontsize=fontsize,
        fontname=fontname,
        color=(0, 0, 0),
    )


class PdfGenerator:
    def generate(
        self,
        results: list[MatchResult],
        summary: ProposalSummary,
        output_path: Path,
        request_number: str = "б/н",
    ) -> Path:
        regular_path, bold_path = resolve_pdf_font_paths()
        font_regular = fitz.Font(fontfile=str(regular_path))
        font_bold = fitz.Font(fontfile=str(bold_path))

        doc = fitz.open()
        page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        y = 48
        today = datetime.now().strftime("%d.%m.%Y")

        def ensure_page_fonts(target_page: fitz.Page) -> None:
            target_page.insert_font(fontname=PDF_FONT_REGULAR_NAME, fontfile=str(regular_path))
            if bold_path == regular_path:
                target_page.insert_font(fontname=PDF_FONT_BOLD_NAME, fontfile=str(regular_path))
            else:
                target_page.insert_font(fontname=PDF_FONT_BOLD_NAME, fontfile=str(bold_path))

        ensure_page_fonts(page)

        def write_line(
            text: str,
            *,
            size: float = 11,
            bold: bool = False,
            center: bool = False,
        ) -> None:
            nonlocal y, page
            fontname = PDF_FONT_BOLD_NAME if bold else PDF_FONT_REGULAR_NAME
            font_obj = font_bold if bold else font_regular
            x = MARGIN_X
            if center:
                x = max(MARGIN_X, (PAGE_WIDTH - font_obj.text_length(text, fontsize=size)) / 2)
            page.insert_text(
                fitz.Point(x, y),
                text,
                fontsize=size,
                fontname=fontname,
                color=(0, 0, 0),
            )
            y += size + 6

        def draw_table_header() -> None:
            nonlocal y, page
            header_top = y
            headers = {
                "num": "№",
                "name": "Наименование",
                "unit": "Ед.",
                "qty": "Кол-во",
                "price": f"Цена, {KP_VAT_LABEL}",
                "sum": "Сумма",
            }
            max_header_lines = 1
            for col_key, header in headers.items():
                line_count = _draw_wrapped_cell(
                    page,
                    col_key,
                    header_top,
                    header,
                    fontname=PDF_FONT_BOLD_NAME,
                    font=font_bold,
                    fontsize=HEADER_FONT_SIZE,
                )
                max_header_lines = max(max_header_lines, line_count)

            header_height = max_header_lines * LINE_HEIGHT + 4
            separator_y = header_top + header_height + 2
            page.draw_line(
                fitz.Point(MARGIN_X, separator_y),
                fitz.Point(MARGIN_RIGHT, separator_y),
                width=0.8,
            )
            y = separator_y + HEADER_GAP_AFTER_LINE

        def start_new_page() -> None:
            nonlocal page, y
            page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            ensure_page_fonts(page)
            y = 48
            draw_table_header()

        def start_footer_page() -> None:
            nonlocal page, y
            page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            ensure_page_fonts(page)
            y = 48

        stamp_path = resolve_kp_stamp_image()
        stamp_display_width = 0
        stamp_display_height = 0
        if stamp_path:
            stamp_display_width, stamp_display_height = _stamp_display_size(stamp_path)

        layout = _single_page_layout(len(results))
        if layout["single_page"]:
            while True:
                footer_try = _footer_block_height(
                    compact=True,
                    stamp_height=stamp_display_height,
                )
                table_try = _estimate_table_body_height(
                    results,
                    font_regular,
                    row_font_size=float(layout["row_font_size"]),
                    line_height=float(layout["line_height"]),
                    row_gap=float(layout["row_gap"]),
                )
                if SINGLE_PAGE_HEADER_RESERVE + table_try + footer_try <= PAGE_MAX_Y:
                    break
                if layout.get("allow_tighten"):
                    if not _tighten_single_page_layout(layout):
                        layout.update(_standard_table_layout(single_page=False))
                        break
                else:
                    layout.update(_standard_table_layout(single_page=False))
                    break

        single_page = bool(layout["single_page"])
        row_font_size = float(layout["row_font_size"])
        line_height = float(layout["line_height"])
        row_gap = float(layout["row_gap"])
        compact_footer = bool(layout["compact_footer"])

        footer_height = _footer_block_height(
            compact=compact_footer,
            stamp_height=stamp_display_height,
        )

        write_line("КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ", size=16, bold=True, center=True)
        write_line(f"на запрос {request_number} от {today} г.", size=11, center=True)

        legal = [COMPANY_NAME]
        if COMPANY_INN:
            legal.append(f"ИНН {COMPANY_INN}")
        if COMPANY_KPP:
            legal.append(f"КПП {COMPANY_KPP}")
        if COMPANY_OGRN:
            legal.append(f"ОГРН {COMPANY_OGRN}")
        write_line(". ".join(legal), size=10)
        if COMPANY_ADDRESS:
            write_line(COMPANY_ADDRESS, size=10)

        y += 8
        draw_table_header()

        total_rows = len(results)
        for row_num, result in enumerate(results, start=1):
            qty = result.tz_item.quantity
            name = result.tz_item.name
            if result.status == MatchStatus.SIMILAR:
                name = f"{name} *"
            elif result.status == MatchStatus.NOT_FOUND:
                name = f"{name} (не подобрано)"

            unit_price = _money(result.unit_price) if result.unit_price is not None else None
            line_total = _money(result.total_price) if result.total_price is not None else None

            name_lines = _wrap_text(
                name,
                font_regular,
                row_font_size,
                _col_width("name"),
            )
            row_height = max(row_font_size + 2, len(name_lines) * line_height + 2)

            if not single_page:
                if row_num == SINGLE_PAGE_MAX_ITEMS + 1:
                    start_new_page()
                else:
                    is_last_row = row_num == total_rows
                    row_limit_y = (
                        min(TABLE_BOTTOM_Y, PAGE_MAX_Y - footer_height)
                        if is_last_row
                        else TABLE_BOTTOM_Y
                    )
                    if y + row_height > row_limit_y:
                        start_new_page()
                        if is_last_row:
                            row_limit_y = min(TABLE_BOTTOM_Y, PAGE_MAX_Y - footer_height)
                            if y + row_height > row_limit_y:
                                start_footer_page()

            row_top = y
            _draw_wrapped_cell(
                page,
                "name",
                row_top,
                name,
                fontname=PDF_FONT_REGULAR_NAME,
                font=font_regular,
                fontsize=row_font_size,
                line_height=line_height,
            )
            _draw_single_cell(
                page,
                "num",
                row_top,
                str(row_num),
                fontname=PDF_FONT_REGULAR_NAME,
                fontsize=row_font_size,
            )
            _draw_single_cell(
                page,
                "unit",
                row_top,
                result.tz_item.unit,
                fontname=PDF_FONT_REGULAR_NAME,
                fontsize=row_font_size,
            )
            _draw_single_cell(
                page,
                "qty",
                row_top,
                _fmt_qty(qty),
                fontname=PDF_FONT_REGULAR_NAME,
                fontsize=row_font_size,
            )
            _draw_single_cell(
                page,
                "price",
                row_top,
                _fmt_money(unit_price),
                fontname=PDF_FONT_REGULAR_NAME,
                fontsize=row_font_size,
            )
            _draw_single_cell(
                page,
                "sum",
                row_top,
                _fmt_money(line_total),
                fontname=PDF_FONT_REGULAR_NAME,
                fontsize=row_font_size,
            )

            y = row_top + row_height + row_gap

        if not single_page and y + footer_height > PAGE_MAX_Y:
            start_footer_page()

        y += TOTAL_SEPARATOR_GAP_BEFORE
        page.draw_line(fitz.Point(MARGIN_X, y), fitz.Point(MARGIN_RIGHT, y), width=0.8)
        y += TOTAL_SEPARATOR_GAP_AFTER_COMPACT if compact_footer else TOTAL_SEPARATOR_GAP_AFTER
        write_line(f"Всего: {_fmt_money(summary.total_price)}", size=11, bold=True)
        y += 4 if compact_footer else 8
        write_line(f"Условия оплаты: {PAYMENT_TERMS}", size=10)
        write_line(f"Срок поставки: {DELIVERY_DAYS}", size=10)
        write_line(f"Доставка: {DELIVERY_TERMS}", size=10)
        y += 6 if compact_footer else 10
        write_line(
            f"Наценка: {get_markup_percent()}% (каталог/прайс); интернет −5%",
            size=9,
        )
        y += 8 if compact_footer else 18
        write_line(COMPANY_DIRECTOR, size=11)

        if stamp_path:
            stamp_x = 305
            stamp_y = resolve_stamp_y(y, stamp_display_height)
            page.insert_image(
                fitz.Rect(
                    stamp_x,
                    stamp_y,
                    stamp_x + stamp_display_width,
                    stamp_y + stamp_display_height,
                ),
                filename=str(stamp_path),
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(output_path)
        doc.close()
        return output_path
