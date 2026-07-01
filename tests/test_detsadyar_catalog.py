from src.services.competitor_catalog_service import (
    _extract_detsadyar_properties,
    _is_detsadyar_product_page_html,
    _is_detsadyar_product_url,
    _parse_detsadyar_page,
    _parse_detsadyar_price,
)
from src.services.competitor_sites import is_competitor_product_page_url

_SAMPLE_HTML = """
<h1>Набор перкуссии 7 видов (10 предметов), Fleet, FLT-PS5</h1>
<div class="product-item-detail-price-current mb-1" id="bx_117848907_1208_price" style="font-size: 30px;">2&nbsp;624 руб.</div>
<div class="tab-pane fade product-item-detail-tab-content active show" id="properties">
    <ul class="product-item-detail-properties">
        <li class="product-item-detail-properties-item">
            <span class="product-item-detail-properties-name">Артикул</span>
            <span class="product-item-detail-properties-dots"></span>
            <span class="product-item-detail-properties-value">FLT-PS5</span>
        </li>
        <li class="product-item-detail-properties-item">
            <span class="product-item-detail-properties-name">Производитель</span>
            <span class="product-item-detail-properties-dots"></span>
            <span class="product-item-detail-properties-value">Fleet</span>
        </li>
        <li class="product-item-detail-properties-item">
            <span class="product-item-detail-properties-name">Вес</span>
            <span class="product-item-detail-properties-dots"></span>
            <span class="product-item-detail-properties-value">920 гр.</span>
        </li>
        <li class="product-item-detail-properties-item">
            <span class="product-item-detail-properties-name">Комплектация</span>
            <span class="product-item-detail-properties-dots"></span>
            <span class="product-item-detail-properties-value">Тамбурин - 1 шт. / Ксилофон -1 шт.</span>
        </li>
    </ul>
</div>
<div class="product-item-detail-slider-controls-block" id="bx_117848907_1208_slider_cont">
    <div class="product-item-detail-slider-controls-image active" data-entity="slider-control">
        <img src="/upload/iblock/a25/a2501c68e52a4e41101120a282500554.png">
    </div>
</div>
"""

_SAMPLE_URL = (
    "https://detsadyar.ru/catalog/nabory_perkusii/"
    "nabor_perkussii_7_vidov_10_predmetov_fleet_flt_ps5/"
)


def test_detsadyar_product_url_detection() -> None:
    assert _is_detsadyar_product_url(_SAMPLE_URL)
    assert is_competitor_product_page_url(_SAMPLE_URL)


def test_detsadyar_price_parsing() -> None:
    assert _parse_detsadyar_price(_SAMPLE_HTML) == 2624.0


def test_detsadyar_properties_parsing() -> None:
    properties = _extract_detsadyar_properties(_SAMPLE_HTML)
    assert properties
    assert "FLT-PS5" in properties
    assert "Fleet" in properties
    assert "920 гр." in properties


def test_parse_detsadyar_page() -> None:
    assert _is_detsadyar_product_page_html(_SAMPLE_HTML)
    products = _parse_detsadyar_page(
        _SAMPLE_HTML,
        domain="detsadyar.ru",
        site_label="ДетсадЯр",
        page_url=_SAMPLE_URL,
    )
    assert len(products) == 1
    product = products[0]
    assert "перкуссии" in product.name.lower()
    assert product.articul == "FLT-PS5"
    assert product.price == 2624.0
    assert product.image_url == (
        "https://detsadyar.ru/upload/iblock/a25/a2501c68e52a4e41101120a282500554.png"
    )
    assert product.description and "Производитель: Fleet" in product.description
