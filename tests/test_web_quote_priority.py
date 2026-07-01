from src.services.models import PriceQuote
from src.services.web_quote_priority import pick_primary_internet_pricing_quote


def test_primary_prefers_competitor_product_over_ai_estimate() -> None:
    ai_estimate = PriceQuote(
        source="web",
        label="Интернет (оценка рынка)",
        matched_name="Комплект гипсовых моделей растений",
        price=5526.0,
        cost=5526.0,
        match_score=100.0,
        url="https://epp24.ru/search?text=plants",
    )
    competitor = PriceQuote(
        source="web",
        label="EPP24",
        matched_name="2.12.11. Комплект гипсовых моделей растений",
        price=3526.0,
        cost=3526.0,
        match_score=100.0,
        url="https://epp24.ru/product/2-12-11-komplekt-gipsovyh-modelej-rastenij/",
    )

    best = pick_primary_internet_pricing_quote([ai_estimate, competitor])

    assert best is competitor
    assert best.price == 3526.0


def test_primary_picks_cheapest_competitor_product() -> None:
    expensive = PriceQuote(
        source="web",
        label="Rostcom",
        matched_name="Комплект гипсовых моделей растений",
        price=5526.0,
        cost=5526.0,
        match_score=95.0,
        url="https://rostcom.ru/product/example/",
    )
    cheaper = PriceQuote(
        source="web",
        label="EPP24",
        matched_name="2.12.11. Комплект гипсовых моделей растений",
        price=3526.0,
        cost=3526.0,
        match_score=100.0,
        url="https://epp24.ru/product/2-12-11-komplekt-gipsovyh-modelej-rastenij/",
    )

    best = pick_primary_internet_pricing_quote([expensive, cheaper])

    assert best is cheaper


def test_primary_prefers_higher_match_over_cheaper_price() -> None:
    exact = PriceQuote(
        source="web",
        label="Rostcom",
        matched_name="Рабочий словарик. 1 класс",
        price=733.0,
        cost=733.0,
        match_score=100.0,
        url="https://rostcom.com/catalog/element/rabochiy_slovarik_1_klass_/",
    )
    cheaper_partial = PriceQuote(
        source="web",
        label="Rostcom",
        matched_name="Англо-русский словарь: 1-4 классы",
        price=255.0,
        cost=255.0,
        match_score=96.0,
        url="https://rostcom.com/catalog/element/anglo_russkiy/",
    )

    best = pick_primary_internet_pricing_quote([cheaper_partial, exact])

    assert best is exact
