from src.services.pdf_generator import PAGE_MAX_Y, resolve_stamp_y


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
