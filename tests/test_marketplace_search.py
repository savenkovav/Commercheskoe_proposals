from src.services.web_search_service import (
    _extract_marketplace_description,
    _extract_marketplace_image,
    _extract_marketplace_price,
    _extract_marketplace_product_url,
    _extract_wildberries_articul,
    is_exact_title_match,
)


OZON_SEARCH_SNIPPET = """
<a href="/product/jbl-partybox-120-club-moshchnyy-zvuk-2536801079/">JBL PartyBox</a>
"link":"https://www.ozon.ru/product/jbl-partybox-120-club-moshchnyy-zvuk-2536801079/"
"""

OZON_PRODUCT_SNIPPET = """
<title>JBL PARTYBOX 120 club</title>
<span class="tsHeadline600Large">27&nbsp;635&nbsp;₽</span>
<img src="https://ir.ozone.ru/s3/multimedia-1-2/wc1000/7732680302.jpg" alt="JBL">
<div data-widget="webShortCharacteristics">
  <span class="tsBodyM">Тип</span><span class="tsBody400Small">Беспроводная колонка</span>
  <span class="tsBodyM">Максимальная мощность, Вт</span><span class="tsBody400Small">160</span>
</div>
"""

WB_PRODUCT_SNIPPET = """
<ins class="mo-typography priceBlockFinalPrice--iToZR">30&nbsp;313&nbsp;₽</ins>
<span class="mo-typography_color_primary">282267839</span>
<table class="table--CGApj shortCells--u8o5E">
  <tr><th><span class="cellWrapper--i4h93">Модель</span></th>
  <td><div class="mo-typography_color_primary">PartyBox CLUB 120</div></td></tr>
</table>
<img src="https://basket-17.wbbasket.ru/vol2822/part282267/282267839/images/c246x328/1.webp">
"""


def test_extract_ozon_product_url_from_search_page() -> None:
    url = _extract_marketplace_product_url(OZON_SEARCH_SNIPPET, "Ozon")
    assert url
    assert "ozon.ru/product/jbl-partybox-120" in url


def test_extract_ozon_price_from_product_page() -> None:
    price = _extract_marketplace_price(OZON_PRODUCT_SNIPPET)
    assert price == 27635.0


def test_extract_ozon_image_and_description() -> None:
    image = _extract_marketplace_image(OZON_PRODUCT_SNIPPET, "Ozon")
    assert image and "ir.ozone.ru" in image
    description = _extract_marketplace_description(OZON_PRODUCT_SNIPPET, "Ozon")
    assert description
    assert "Беспроводная колонка" in description


def test_extract_wildberries_price_and_articul() -> None:
    url = "https://www.wildberries.ru/catalog/282267839/detail.aspx"
    price = _extract_marketplace_price(WB_PRODUCT_SNIPPET)
    assert price == 30313.0
    articul = _extract_wildberries_articul(url, WB_PRODUCT_SNIPPET)
    assert articul == "282267839"
    description = _extract_marketplace_description(WB_PRODUCT_SNIPPET, "Wildberries")
    assert description
    assert "PartyBox CLUB 120" in description


def test_marketplace_title_match_for_partybox() -> None:
    query = "Портативная аудиосистема JBL PartyBox Club 120"
    title = "JBL PARTYBOX 120 club мощный звук караоке"
    assert is_exact_title_match(query, title, threshold=75)
