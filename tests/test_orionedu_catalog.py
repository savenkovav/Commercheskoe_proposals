from src.services.competitor_catalog_service import (
    _is_orionedu_product_page_html,
    _is_orionedu_product_url,
    _parse_orionedu_page,
    _parse_orionedu_price,
)
from src.services.competitor_sites import is_competitor_product_page_url

_SAMPLE_HTML = """
<h1>Анатомический тренажер для отработки внутримышечных инъекций</h1>
<div class="elementor-heading-title elementor-size-default">Артикул 9223</div>
<p class="price"><span class="woocommerce-Price-amount amount"><bdi>71140,00&nbsp;<span class="woocommerce-Price-currencySymbol">₽</span></bdi></span></p>
<div class="elementor-widget-woocommerce-product-content default">
  <div class="elementor-widget-container">
    <p>Анатомический тренажер для отработки навыков.</p>
  </div>
</div>
<meta property="og:image" content="https://orionedu.ru/wp-content/uploads/product.jpg" />
"""


def test_orionedu_product_url_detection() -> None:
    url = (
        "https://orionedu.ru/product/anatomicheskij-trenazher-dlja-otrabotki-"
        "vnutrimyshechnyh-inekcij-v-jagodicu-prozrachnaja-model/"
    )
    assert _is_orionedu_product_url(url)
    assert is_competitor_product_page_url(url)


def test_orionedu_price_parsing() -> None:
    price = _parse_orionedu_price(_SAMPLE_HTML)
    assert price == 71140.0


def test_parse_orionedu_page() -> None:
    products = _parse_orionedu_page(
        _SAMPLE_HTML,
        domain="orionedu.ru",
        site_label="Орион",
        page_url=(
            "https://orionedu.ru/product/anatomicheskij-trenazher-dlja-otrabotki-"
            "vnutrimyshechnyh-inekcij-v-jagodicu-prozrachnaja-model/"
        ),
    )
    assert len(products) == 1
    product = products[0]
    assert "тренажер" in product.name.lower()
    assert product.articul == "9223"
    assert product.price == 71140.0
    assert product.image_url == "https://orionedu.ru/wp-content/uploads/product.jpg"
    assert _is_orionedu_product_page_html(_SAMPLE_HTML)
