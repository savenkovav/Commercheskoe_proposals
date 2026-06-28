from src.services.competitor_catalog_service import _diversify_catalog_quotes
from src.services.models import PriceQuote


def test_catalog_diversify_prefers_best_match_over_cheapest_price() -> None:
    quotes = [
        PriceQuote(
            source="web",
            label="Rostcom",
            matched_name="Рабочий словарик. 1 класс",
            price=733.0,
            cost=733.0,
            match_score=100.0,
            url="https://rostcom.com/catalog/element/rabochiy_slovarik_1_klass_/",
            notes="Индекс каталога",
        ),
        PriceQuote(
            source="web",
            label="Rostcom",
            matched_name="Англо-русский словарь: 1-4 классы",
            price=255.0,
            cost=255.0,
            match_score=96.0,
            url="https://rostcom.com/catalog/element/anglo_russkiy/",
            notes="Индекс каталога",
        ),
    ]
    result = _diversify_catalog_quotes(quotes, limit=5, max_per_domain=2)
    assert result[0].matched_name == "Рабочий словарик. 1 класс"
    scores = [q.match_score for q in result]
    assert scores == sorted(scores, reverse=True)


def test_finalize_sort_by_match_orders_descending() -> None:
    from src.services.web_search_service import WebSearchService

    quotes = [
        PriceQuote(
            source="web",
            label="Rostcom",
            matched_name="low",
            price=100.0,
            cost=100.0,
            match_score=96.0,
            url="https://rostcom.com/catalog/element/low/",
            notes="Индекс каталога",
        ),
        PriceQuote(
            source="web",
            label="EPP24",
            matched_name="exact",
            price=500.0,
            cost=500.0,
            match_score=100.0,
            url="https://epp24.ru/product/exact/",
            notes="Индекс каталога",
        ),
    ]
    result = WebSearchService._finalize_competitor_quotes(
        quotes,
        limit=5,
        sort_by_match=True,
    )
    assert result[0].match_score == 100.0
    scores = [q.match_score for q in result]
    assert scores == sorted(scores, reverse=True)
