from unittest.mock import MagicMock, patch

from src.services.kp_preferences import KpPreferences
from src.services.models import PriceQuote, TZItem
from src.services.tz_match_service import TZMatchService


def test_fetch_internet_comparison_searches_competitors_when_local_hit() -> None:
    matcher = MagicMock()
    matcher.is_distinctive_mismatch.return_value = False
    service = TZMatchService(matcher, MagicMock(), [], [], MagicMock())
    tz_item = TZItem(number=1, name="Комплект муляжей", unit="шт", quantity=1)
    competitor_quote = PriceQuote(
        source="web",
        label="Конкурент: EPP24",
        matched_name="Комплект муляжей",
        price=5390.0,
        cost=5390.0,
        match_score=100.0,
        url="https://epp24.ru/product/test/",
        description="Описание из базы",
    )
    service.web_search.search_competitor_offers = MagicMock(return_value=[competitor_quote])
    service.web_search.search_internet_cascade = MagicMock(return_value=[])

    web_quote, quotes = service._fetch_internet_comparison(
        tz_item,
        KpPreferences(),
        use_ai=False,
        local_miss=False,
        competitors_only=True,
    )

    service.web_search.search_competitor_offers.assert_called_once()
    service.web_search.search_internet_cascade.assert_not_called()
    assert web_quote is not None
    assert quotes[0].description == "Описание из базы"


def test_enrich_web_quotes_with_catalog_descriptions(tmp_path, monkeypatch) -> None:
    from src.services.competitor_catalog_db import CompetitorCatalogDatabase
    from src.services.competitor_catalog_service import CompetitorCatalogProduct
    from src.services.competitor_product_store import CompetitorProductStore
    from src.services.web_quote_priority import enrich_web_quotes_with_catalog_descriptions

    db_path = tmp_path / "competitor.db"
    db = CompetitorCatalogDatabase(db_path)
    store = CompetitorProductStore(db)
    store.merge_products(
        [
            CompetitorCatalogProduct(
                domain="epp24.ru",
                site_label="EPP24",
                name="Тестовый товар",
                price=100.0,
                url="https://epp24.ru/product/test-item/",
                description="Описание товара из каталога",
            )
        ],
        domain="epp24.ru",
        site_label="EPP24",
    )

    monkeypatch.setattr(
        "src.services.competitor_product_store.get_competitor_product_store",
        lambda: store,
    )

    quotes = enrich_web_quotes_with_catalog_descriptions(
        [
            PriceQuote(
                source="web",
                label="Конкурент: EPP24",
                matched_name="Тестовый товар",
                price=100.0,
                url="https://epp24.ru/product/test-item/",
                match_score=100.0,
            )
        ]
    )
    assert quotes[0].description == "Описание товара из каталога"
