from pathlib import Path

import fitz

from src.services.pdf_generator import (
    PAGE_MAX_Y,
    ROW_FONT_SIZE,
    ROW_GAP,
    LINE_HEIGHT,
    _draw_title_with_logo,
    _single_page_layout,
    resolve_stamp_y,
)


def test_draw_title_with_logo_advances_y(tmp_path: Path) -> None:
    logo_path = Path(__file__).resolve().parents[1] / "data" / "templates" / "kp_logo.png"
    if not logo_path.exists():
        return

    regular = Path(__file__).resolve().parents[1] / "data" / "templates" / "fonts" / "DejaVuSans-Bold.ttf"
    if not regular.exists():
        return

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="KpPdfBold", fontfile=str(regular))
    font = fitz.Font(fontfile=str(regular))
    next_y = _draw_title_with_logo(
        page,
        y=48,
        title="КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ",
        fontname="KpPdfBold",
        font=font,
        fontsize=16,
        logo_path=logo_path,
    )
    assert next_y > 48
    assert page.get_images()
    doc.close()


def test_single_page_layout_keeps_standard_rows_up_to_16_items() -> None:
    for count in (1, 2, 10, 16):
        layout = _single_page_layout(count)
        assert layout["single_page"] is True
        assert layout["row_font_size"] == ROW_FONT_SIZE
        assert layout["line_height"] == LINE_HEIGHT
        assert layout["row_gap"] == ROW_GAP
        assert layout["allow_tighten"] is False


def test_single_page_layout_compresses_rows_for_17_to_20_items() -> None:
    layout = _single_page_layout(18)
    assert layout["single_page"] is True
    assert layout["row_font_size"] == 8.5
    assert layout["row_gap"] == 2.0
    assert layout["allow_tighten"] is True


def test_resolve_stamp_y_places_stamp_under_content() -> None:
    content_bottom_y = 420.0
    stamp_height = 120.0
    stamp_y = resolve_stamp_y(content_bottom_y, stamp_height)
    assert stamp_y == 424.0
    assert stamp_y < PAGE_MAX_Y - stamp_height


def test_resolve_stamp_y_clamps_to_page_bottom_when_footer_is_long() -> None:
    stamp_height = 120.0
    content_bottom_y = PAGE_MAX_Y - 10.0
    stamp_y = resolve_stamp_y(content_bottom_y, stamp_height)
    assert stamp_y == PAGE_MAX_Y - stamp_height
