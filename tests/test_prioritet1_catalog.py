from src.services.competitor_catalog_service import (
    _is_prioritet1_product_page_html,
    _is_prioritet1_product_url,
    _parse_prioritet1_page,
    _parse_prioritet1_price,
)
from src.services.competitor_sites import is_competitor_product_page_url

_SAMPLE_HTML = """
<h1>Интерактивный комплекс Рычи, не молчи</h1>
<div class="bCard__article"><span>Артикул:</span> 45702</div>
<div class="bCard__cost" itemprop="price" content="260800"> 260 800 руб. </div>
<ul class="bCard__features">
  <li><span>Вес, кг: </span><span>30</span></li>
  <li><span>Цвет: </span><span>Натуральный</span></li>
</ul>
<div class="bCard__desc">Подходит под ФГОС ДО.</div>
"""


def test_prioritet1_product_url_detection() -> None:
    url = "https://prioritet1.com/katalog/interaktivnyj-kompleks-rychi-ne-molchi"
    assert _is_prioritet1_product_url(url)
    assert is_competitor_product_page_url(url)


def test_prioritet1_price_parsing() -> None:
    price = _parse_prioritet1_price(_SAMPLE_HTML)
    assert price == 260800.0


def test_parse_prioritet1_page() -> None:
    products = _parse_prioritet1_page(
        _SAMPLE_HTML,
        domain="prioritet1.com",
        site_label="Приоритет",
        page_url="https://prioritet1.com/katalog/interaktivnyj-kompleks-rychi-ne-molchi",
    )
    assert len(products) == 1
    product = products[0]
    assert product.name == "Интерактивный комплекс Рычи, не молчи"
    assert product.articul == "45702"
    assert product.price == 260800.0
    assert "Вес, кг: 30" in (product.description or "")
    assert _is_prioritet1_product_page_html(_SAMPLE_HTML)
