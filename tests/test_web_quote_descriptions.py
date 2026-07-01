from src.services.competitor_catalog_db import CompetitorCatalogDatabase
from src.services.competitor_catalog_service import CompetitorCatalogProduct
from src.services.competitor_product_store import CompetitorProductStore
from src.services.models import PriceQuote
from src.services.web_quote_priority import enrich_web_quotes_with_catalog_descriptions


def test_enrich_web_quotes_with_catalog_descriptions(tmp_path, monkeypatch) -> None:
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
