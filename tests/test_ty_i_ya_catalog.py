from src.services.competitor_catalog_service import (
    _is_ty_i_ya_product_page_html,
    _is_ty_i_ya_product_url,
    _parse_ty_i_ya_page,
    _parse_ty_i_ya_price,
)
from src.services.competitor_sites import (
    canonical_competitor_domain,
    get_builtin_competitor_site,
    is_competitor_product_page_url,
)

_SAMPLE_HTML = """
<h1>Стеллаж "УютБук" с зоной для чтения</h1>
<span>Артикул: <b class="black">БС-003</b></span>
<div class="product-buy__price">108 100<span class="currency">₽</span></div>
<dl class="product-info__list">
  <div class="product-info__row">
    <dt>Производитель</dt>
    <dd class="product-info__list-text">Ты и Я!</dd>
  </div>
  <div class="product-info__row">
    <dt>Размер</dt>
    <dd class="product-info__list-text">ширина - 2000 мм</dd>
  </div>
</dl>
<div class="product-tabs__content">Стеллаж «УютБук» воплощает гармоничное соединение.</div>
<meta property="og:image" content="/upload/webp/100/product.webp" />
"""

_DOMAIN = "xn--54-vlc3b6bza.xn--p1ai"
_CYRILLIC_DOMAIN = "тыия54.рф"
_SAMPLE_URL = (
    "https://xn--54-vlc3b6bza.xn--p1ai/products/stellazh-uyutbuk-s-zonoy-dlya-chteniya/"
)


def test_canonical_competitor_domain_cyrillic_ty_i_ya() -> None:
    assert canonical_competitor_domain(_CYRILLIC_DOMAIN) == _DOMAIN
    assert canonical_competitor_domain(f"www.{_CYRILLIC_DOMAIN}") == _DOMAIN
    builtin = get_builtin_competitor_site(_CYRILLIC_DOMAIN)
    assert builtin is not None
    assert builtin.label == "Ты и Я"


def test_ty_i_ya_product_url_detection() -> None:
    assert _is_ty_i_ya_product_url(_SAMPLE_URL)
    assert is_competitor_product_page_url(_SAMPLE_URL)


def test_ty_i_ya_price_parsing() -> None:
    price = _parse_ty_i_ya_price(_SAMPLE_HTML)
    assert price == 108100.0


def test_parse_ty_i_ya_page() -> None:
    products = _parse_ty_i_ya_page(
        _SAMPLE_HTML,
        domain=_DOMAIN,
        site_label="Ты и Я",
        page_url=_SAMPLE_URL,
    )
    assert len(products) == 1
    product = products[0]
    assert "уютбук" in product.name.lower()
    assert product.articul == "БС-003"
    assert product.price == 108100.0
    assert product.details and "Производитель" in product.details
    assert product.description and "УютБук" in product.description
    assert product.image_url and product.image_url.endswith("product.webp")
    assert _is_ty_i_ya_product_page_html(_SAMPLE_HTML)


def test_parse_ty_i_ya_page_accepts_cyrillic_domain() -> None:
    products = _parse_ty_i_ya_page(
        _SAMPLE_HTML,
        domain=_CYRILLIC_DOMAIN,
        site_label="Ты и Я",
        page_url=_SAMPLE_URL,
    )
    assert len(products) == 1
