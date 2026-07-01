from src.services.product_lookup import ProductLookupService


def test_should_search_marketplaces_when_everything_missing() -> None:
    assert ProductLookupService._should_search_marketplaces(
        {"found": False},
        {"found": False},
        {"found": False},
        {"found": False, "items": []},
    )


def test_should_search_marketplaces_when_competitors_unpriced() -> None:
    assert ProductLookupService._should_search_marketplaces(
        {"found": False},
        {"found": False},
        {"found": False},
        {
            "found": True,
            "items": [
                {
                    "name": "Товар",
                    "has_price": False,
                    "price_amount": None,
                }
            ],
        },
    )


def test_should_not_search_marketplaces_when_catalog_found() -> None:
    assert not ProductLookupService._should_search_marketplaces(
        {"found": True},
        {"found": False},
        {"found": False},
        {"found": False, "items": []},
    )


def test_should_not_search_marketplaces_when_competitor_priced() -> None:
    assert not ProductLookupService._should_search_marketplaces(
        {"found": False},
        {"found": False},
        {"found": False},
        {
            "found": True,
            "items": [
                {
                    "name": "Товар",
                    "has_price": True,
                    "price_amount": 1000.0,
                    "price": "1 000.00 ₽",
                }
            ],
        },
    )
