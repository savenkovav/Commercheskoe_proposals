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
