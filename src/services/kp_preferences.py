from __future__ import annotations

from dataclasses import dataclass, field

from src.config import LOCAL_MATCH_THRESHOLD
from src.services.competitor_sites import meets_web_display_threshold
from src.services.web_quote_priority import is_search_listing_url


@dataclass
class KpPreferences:
    excluded_platforms: list[str] = field(default_factory=list)
    disabled_sources: list[str] = field(default_factory=list)
    search_kit_component_links: bool = False
    force_kit_component_pricing: bool = False
    rules: list[str] = field(default_factory=list)

    def merge_ai_patch(self, patch: dict) -> None:
        for platform in patch.get("excluded_platforms_add") or []:
            if platform and platform not in self.excluded_platforms:
                self.excluded_platforms.append(str(platform))
        for platform in patch.get("excluded_platforms_remove") or []:
            self.excluded_platforms = [
                p for p in self.excluded_platforms if p.lower() != str(platform).lower()
            ]

        for source in patch.get("disabled_sources_add") or []:
            key = str(source).lower()
            if key and key not in self.disabled_sources:
                self.disabled_sources.append(key)
        for source in patch.get("disabled_sources_remove") or []:
            key = str(source).lower()
            self.disabled_sources = [s for s in self.disabled_sources if s != key]

        if patch.get("search_kit_component_links") is True:
            self.search_kit_component_links = True
        if patch.get("search_kit_component_links") is False:
            self.search_kit_component_links = False

        if patch.get("force_kit_component_pricing") is True:
            self.force_kit_component_pricing = True
        if patch.get("force_kit_component_pricing") is False:
            self.force_kit_component_pricing = False

    def add_rule(self, rule_text: str) -> None:
        text = rule_text.strip()
        if text and text not in self.rules:
            self.rules.append(text)

    def to_dict(self) -> dict:
        return {
            "excluded_platforms": list(self.excluded_platforms),
            "disabled_sources": list(self.disabled_sources),
            "search_kit_component_links": self.search_kit_component_links,
            "force_kit_component_pricing": self.force_kit_component_pricing,
            "rules": list(self.rules),
        }


def platform_is_excluded(label: str, url: str | None, excluded: list[str]) -> bool:
    if not excluded:
        return False
    haystack = f"{label} {url or ''}".lower()
    return any(token.lower() in haystack for token in excluded if token)


def web_quote_meets_match_threshold(quote: object) -> bool:
    if getattr(quote, "source", None) != "web":
        return True
    score = float(getattr(quote, "match_score", 0) or 0)
    url = getattr(quote, "url", None)
    return meets_web_display_threshold(url, score)


def local_quote_meets_match_threshold(quote: object) -> bool:
    source = str(getattr(quote, "source", "") or "")
    if source == "web":
        return web_quote_meets_match_threshold(quote)
    return float(getattr(quote, "match_score", 0) or 0) >= LOCAL_MATCH_THRESHOLD


def filter_comparison_quotes(quotes: list, preferences: KpPreferences) -> list:
    filtered: list = []
    for quote in quotes:
        source = str(getattr(quote, "source", "") or "")
        if source and source in preferences.disabled_sources:
            continue
        if source == "web":
            if "web" in preferences.disabled_sources:
                continue
            is_reference_link = getattr(quote, "notes", "") == "Поисковая ссылка"
            if not is_reference_link and not web_quote_meets_match_threshold(quote):
                continue
            if is_search_listing_url(getattr(quote, "url", None)):
                if not is_reference_link:
                    continue
            if platform_is_excluded(
                getattr(quote, "label", ""),
                getattr(quote, "url", None),
                preferences.excluded_platforms,
            ):
                continue
        elif not local_quote_meets_match_threshold(quote):
            continue
        filtered.append(quote)
    return filtered


def filter_web_quotes(quotes: list, preferences: KpPreferences) -> list:
    return filter_comparison_quotes(quotes, preferences)


def competitor_link_urls(
    quotes: list,
    product_name: str,
    preferences: KpPreferences | None,
    limit: int = 3,
) -> list[str]:
    from src.services.competitor_urls import competitor_urls_from_quotes

    if preferences and "web" in preferences.disabled_sources:
        return []

    web_quotes = [
        q for q in quotes if getattr(q, "source", None) == "web"
    ]
    excluded = preferences.excluded_platforms if preferences else []
    allow_fallback = not (preferences and "web" in preferences.disabled_sources)
    return competitor_urls_from_quotes(
        web_quotes,
        product_name,
        limit=limit,
        excluded_platforms=excluded,
        allow_fallback=allow_fallback,
    )
