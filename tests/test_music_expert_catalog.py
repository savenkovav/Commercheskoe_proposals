from src.services.competitor_catalog_service import (
    _extract_music_expert_listing_product_urls,
    _is_music_expert_product_page_html,
    _normalize_music_expert_catalog_url,
)

_SAMPLE_LISTING_HTML = """
<a href="/catalog/zvukovoe_oborudovanie/behringer_ulm300usb/" class="product_card_img">
<p itemprop="name">Behringer ULM300USB</p>
<a href="/catalog/zvukovoe_oborudovanie/behringer_c1/" class="product_card_img">
<p itemprop="name">Behringer C1</p>
"""

_SAMPLE_PRODUCT_HTML = """
<div class="product_card_price_actual" data-price="8280.00"></div>
<div class="product_about"><p>Описание товара</p></div>
"""


def test_normalize_music_expert_catalog_url() -> None:
    url = _normalize_music_expert_catalog_url(
        "https://www.music-expert.ru/catalog/zvukovoe_oborudovanie/"
    )
    assert url == "https://www.music-expert.ru/catalog/zvukovoe_oborudovanie/"


def test_extract_music_expert_listing_product_urls() -> None:
    urls = _extract_music_expert_listing_product_urls(
        _SAMPLE_LISTING_HTML,
        page_url="https://www.music-expert.ru/catalog/zvukovoe_oborudovanie/",
    )
    assert len(urls) == 2
    assert all("music-expert.ru/catalog/" in url for url in urls)


def test_is_music_expert_product_page_html() -> None:
    assert _is_music_expert_product_page_html(_SAMPLE_PRODUCT_HTML)
