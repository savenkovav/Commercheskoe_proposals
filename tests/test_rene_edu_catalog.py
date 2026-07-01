from src.services.competitor_catalog_service import (
    _is_rene_edu_product_page_html,
    _is_rene_edu_product_url,
    _parse_rene_edu_page,
    _parse_rene_edu_price,
)
from src.services.competitor_sites import is_competitor_product_page_url

_SAMPLE_HTML = """
<h1>Школьная метеостанция</h1>
<div class="vendor-code"><span>Артикул</span> <h2>67128</h2></div>
<div class="price">83 000<span>₽</span></div>
<div data-tab="1" style="display: block;">
  <div class="editor--default editor">
    <p>Комплект оборудования для метеонаблюдений.</p>
  </div>
</div>
"""


def test_rene_edu_product_url_detection() -> None:
    url = "https://www.rene-edu.ru/srednyaya-i-starshaya-shkola/327.html"
    assert _is_rene_edu_product_url(url)
    assert is_competitor_product_page_url(url)


def test_rene_edu_price_parsing() -> None:
    price, label = _parse_rene_edu_price('<div class="price">83 000<span>₽</span></div>')
    assert price == 83000.0
    assert label is None

    price2, label2 = _parse_rene_edu_price('<div class="price">по запросу</div>')
    assert price2 is None
    assert label2


def test_parse_rene_edu_page() -> None:
    products = _parse_rene_edu_page(
        _SAMPLE_HTML,
        domain="rene-edu.ru",
        site_label="Рене",
        page_url="https://www.rene-edu.ru/srednyaya-i-starshaya-shkola/327.html",
    )
    assert len(products) == 1
    product = products[0]
    assert product.name == "Школьная метеостанция"
    assert product.articul == "67128"
    assert product.price == 83000.0
    assert _is_rene_edu_product_page_html(_SAMPLE_HTML)
