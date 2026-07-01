from __future__ import annotations

from src.services.kp_preferences import KpPreferences
from src.services.models import PriceQuote
from src.services.product_lookup import LookupField, ProductLookupService


def test_kp_preferences_web_search_allowed_respects_base_only() -> None:
    assert KpPreferences().web_search_allowed() is True
    assert KpPreferences(base_only=True).web_search_allowed() is False
    assert KpPreferences(disabled_sources=["web"]).web_search_allowed() is False


def test_lookup_base_only_skips_competitors() -> None:
    class StubMatcher:
        def find_candidates(self, tz_item):
            return {"catalog": [], "registry": [], "price": []}

        def pick_best_hit(self, tz_item, hits):
            return None

        def rank_hits(self, query, candidates, limit=20):
            return []

    class StubWebSearch:
        def search_competitor_offers(self, query: str, sort_by_match: bool = False) -> list[PriceQuote]:
            raise AssertionError("competitor search should not run in base_only mode")

        def search_web_price_fallback(self, query: str) -> list[PriceQuote]:
            raise AssertionError("web fallback should not run in base_only mode")

    service = ProductLookupService.__new__(ProductLookupService)
    service.matcher = StubMatcher()
    service.ai = None
    service.web_search = StubWebSearch()

    result = service.lookup(
        "термометр",
        [LookupField.PRICE],
        base_only=True,
    )
    assert result.competitors == {"found": False, "items": []}
