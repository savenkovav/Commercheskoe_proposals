from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.config import (
    COMPETITOR_SEARCH_ENABLED,
    EXACT_MATCH_THRESHOLD,
    SIMILAR_MATCH_THRESHOLD,
    WEB_PRICE_DISCOUNT_PERCENT,
    WEB_SEARCH_ENABLED,
)
from src.services.markup_settings import get_markup_percent
from src.services.data_loader import normalize_name
from src.services.models import CatalogItem, MatchSource, MatchStatus, PriceListItem, PriceQuote, RegistryItem, TZItem
from src.services.ai_agent import AIAgent
from src.services.matcher import FuzzyHit, ItemMatcher
from src.services.web_quote_priority import meets_web_display_threshold
from src.services.web_search_service import WebSearchService

logger = logging.getLogger(__name__)

LOOKUP_MATCH_LIMIT = 20


class LookupField(str, Enum):
    COST = "cost"
    PRICE = "price"
    PRICE_KP = "price_kp"
    PRICE_SUPPLIER = "price_supplier"
    QUANTITY = "quantity"
    UNIT = "unit"
    SUPPLIER = "code_supplier"
    CODE = "code"
    MATCH = "match"


FIELD_LABELS = {
    LookupField.COST: "Себестоимость",
    LookupField.PRICE: "Цена",
    LookupField.PRICE_KP: "Цена КП",
    LookupField.PRICE_SUPPLIER: "Цена прайса",
    LookupField.QUANTITY: "Остаток / количество",
    LookupField.UNIT: "Ед. изм.",
    LookupField.SUPPLIER: "Поставщик",
    LookupField.CODE: "Код в прайсе",
    LookupField.MATCH: "Совпадение",
}


def get_field_labels() -> dict[LookupField, str]:
    labels = dict(FIELD_LABELS)
    labels[LookupField.PRICE_KP] = f"Цена КП (+{get_markup_percent()}%)"
    return labels

FIELD_ALIASES: dict[LookupField, list[str]] = {
    LookupField.COST: ["себестоимость", "себест", "закупочн", "закуп"],
    LookupField.PRICE: ["продажная цена", "цена продажи", "отпускная цена"],
    LookupField.PRICE_KP: [
        "цена кп",
        "цена продажи",
        "с наценкой",
        "наценк",
        "продажн",
        "кп",
    ],
    LookupField.PRICE_SUPPLIER: [
        "прайс",
        "цена поставщика",
        "цена прайса",
        "закупочная цена",
    ],
    LookupField.QUANTITY: [
        "количество",
        "остаток",
        "наличие",
        "сколько есть",
        "склад",
        "кол-во",
        "кол во",
    ],
    LookupField.UNIT: ["единица", "ед. изм", "единицы", "едизм"],
    LookupField.SUPPLIER: ["поставщик"],
    LookupField.CODE: ["код", "артикул"],
}

DEFAULT_FIELDS = [
    LookupField.COST,
    LookupField.PRICE,
    LookupField.PRICE_SUPPLIER,
    LookupField.PRICE_KP,
    LookupField.QUANTITY,
    LookupField.UNIT,
]

PRICE_ALIASES = ["цена", "стоимость", "сколько стоит", "прайс"]

NOISE_PHRASES = [
    r"сколько\s+стоит",
    r"какая\s+(?:цена|стоимость)",
    r"какой\s+(?:остаток|количество)",
    r"и\s+какой",
    r"есть\s+ли",
    r"найди(?:те)?",
    r"найти",
    r"покажи(?:те)?",
    r"узнай(?:те)?",
    r"информация\s+о",
    r"данные\s+по",
    r"подскажи(?:те)?",
    r"что\s+с",
    r"дай(?:те)?",
    r"нужн(?:а|о|ы)?",
    r"интересует",
    r"\?",
]

LOOKUP_TRIGGER = re.compile(
    r"|".join(
        [
            r"\b(?:сколько\s+стоит|какая\s+цена|какой\s+остаток)\b",
            r"\b(?:найди|найти|покажи|узнай|есть\s+ли)\b",
            r"\b(?:цена|стоимость|остаток|количество|себестоимость)\b",
            r"[|:]",
        ]
    ),
    re.IGNORECASE,
)

BULK_KP_SEARCH = re.compile(
    r"(?:"
    r"найди\s+(?:в\s+)?(?:каталог|прайс|склад|все)|"
    r"обработай\s+(?:тз|все|позици)|"
    r"пересчитай\s+все|"
    r"только\s+поиск|"
    r"начни\s+поиск|"
    r"запусти\s+поиск|"
    r"задача\s*1|"
    r"задача\s*1\+2|"
    r"1\+2|"
    r"сформируй\s+(?:excel|кп)"
    r")",
    re.IGNORECASE,
)

SINGLE_ITEM_PRICE_QUERY = re.compile(
    r"\b(?:"
    r"сколько\s+стоит|"
    r"какая\s+(?:цена|стоимость)|"
    r"стоимость|"
    r"себестоимость|"
    r"цена\b"
    r")",
    re.IGNORECASE,
)

TZ_ITEM_NUMBER = re.compile(r"(?:позици\w*|№|#)\s*(\d+)", re.IGNORECASE)

PRICE_LOOKUP_FIELDS = [
    LookupField.COST,
    LookupField.PRICE,
    LookupField.PRICE_KP,
    LookupField.PRICE_SUPPLIER,
]


@dataclass
class ProductQuery:
    product_name: str
    requested_fields: list[LookupField] = field(default_factory=list)


REGISTRY_NOT_FOUND_MESSAGE = "В Реестре остатков нет такого наименования"


@dataclass
class ProductLookupResult:
    query_name: str
    matched_name: str
    match_score: float
    status: MatchStatus
    values: dict[LookupField, str]
    sources: list[str]
    alternatives: list[str]
    not_found: bool = False
    catalog: dict[str, object] = field(default_factory=dict)
    price_list: dict[str, object] = field(default_factory=dict)
    registry: dict[str, object] = field(default_factory=dict)
    competitors: dict[str, object] = field(default_factory=dict)
    ai_insight: dict[str, object] = field(default_factory=dict)


class ProductLookupService:
    def __init__(
        self,
        matcher: ItemMatcher,
        ai_agent: AIAgent | None = None,
        web_search: WebSearchService | None = None,
    ) -> None:
        self.matcher = matcher
        self.ai = ai_agent
        self.web_search = web_search or WebSearchService()

    def lookup(self, product_name: str, requested_fields: list[LookupField] | None = None) -> ProductLookupResult:
        fields = requested_fields or DEFAULT_FIELDS
        tz_item = TZItem(number=1, name=product_name.strip(), unit="шт.", quantity=1)
        candidates = self.matcher.find_candidates(tz_item)

        catalog_hit = self._best_hit(candidates["catalog"], product_name)
        registry_hit = self._best_hit(candidates["registry"], product_name)
        price_hit = self._best_hit(candidates["price"], product_name)

        catalog_block = self._build_catalog_block(
            catalog_hit, candidates["catalog"], product_name, self.matcher
        )
        price_block = self._build_price_block(
            price_hit, candidates["price"], product_name, self.matcher
        )
        registry_block = self._build_registry_block(
            registry_hit, candidates["registry"], product_name, self.matcher
        )
        competitors_block = (
            self._build_competitors_block(product_name)
            if self._should_search_competitors(fields)
            else {"found": False, "items": []}
        )

        best_hit = self._select_primary_hit(catalog_hit, price_hit)
        any_found = self._any_source_found(
            catalog_block, price_block, registry_block, competitors_block
        )

        ai_insight = self._maybe_build_ai_insight(
            tz_item,
            catalog_block,
            price_block,
            registry_block,
            competitors_block,
        )

        if not best_hit or best_hit.score < SIMILAR_MATCH_THRESHOLD:
            if not any_found:
                return ProductLookupResult(
                    query_name=product_name,
                    matched_name="",
                    match_score=0,
                    status=MatchStatus.NOT_FOUND,
                    values={},
                    sources=[],
                    alternatives=self._collect_alternatives(candidates),
                    not_found=True,
                    catalog=catalog_block,
                    price_list=price_block,
                    registry=registry_block,
                    competitors=competitors_block,
                    ai_insight=ai_insight,
                )

            primary_name = self._resolve_display_name(
                best_hit,
                catalog_block,
                price_block,
                competitors_block,
            )
            return ProductLookupResult(
                query_name=product_name,
                matched_name=primary_name,
                match_score=float(best_hit.score if best_hit else 0),
                status=MatchStatus.SIMILAR,
                values=self._build_values(fields, catalog_hit, registry_hit, price_hit),
                sources=self._build_sources(
                    catalog_hit, registry_hit, price_hit, competitors_block
                ),
                alternatives=self._collect_alternatives(candidates),
                not_found=False,
                catalog=catalog_block,
                price_list=price_block,
                registry=registry_block,
                competitors=competitors_block,
                ai_insight=ai_insight,
            )

        catalog_hit = catalog_hit if self._is_relevant_hit(catalog_hit, best_hit) else None
        registry_hit = registry_hit if self._is_relevant_hit(registry_hit, best_hit) else None
        price_hit = price_hit if self._is_relevant_hit(price_hit, best_hit) else None

        status = (
            MatchStatus.EXACT
            if best_hit.score >= EXACT_MATCH_THRESHOLD
            else MatchStatus.SIMILAR
        )

        values = self._build_values(fields, catalog_hit, registry_hit, price_hit)
        sources = self._build_sources(
            catalog_hit, registry_hit, price_hit, competitors_block
        )

        return ProductLookupResult(
            query_name=product_name,
            matched_name=best_hit.name,
            match_score=best_hit.score,
            status=status,
            values=values,
            sources=sources,
            alternatives=self._collect_alternatives(candidates, exclude=best_hit.name),
            catalog=catalog_block,
            price_list=price_block,
            registry=registry_block,
            competitors=competitors_block,
            ai_insight=ai_insight,
        )

    @staticmethod
    def _all_sources_missing(
        catalog_block: dict[str, object],
        price_block: dict[str, object],
        registry_block: dict[str, object],
        competitors_block: dict[str, object] | None = None,
    ) -> bool:
        competitors_block = competitors_block or {"found": False}
        return (
            not catalog_block.get("found")
            and not price_block.get("found")
            and not registry_block.get("found")
            and not competitors_block.get("found")
        )

    @staticmethod
    def _any_source_found(
        catalog_block: dict[str, object],
        price_block: dict[str, object],
        registry_block: dict[str, object],
        competitors_block: dict[str, object],
    ) -> bool:
        return not ProductLookupService._all_sources_missing(
            catalog_block,
            price_block,
            registry_block,
            competitors_block,
        )

    @staticmethod
    def _should_search_competitors(fields: list[LookupField]) -> bool:
        if not WEB_SEARCH_ENABLED or not COMPETITOR_SEARCH_ENABLED:
            return False
        price_fields = {
            LookupField.COST,
            LookupField.PRICE,
            LookupField.PRICE_KP,
            LookupField.PRICE_SUPPLIER,
        }
        return bool(price_fields.intersection(fields))

    def _build_competitors_block(self, query: str) -> dict[str, object]:
        quotes: list[PriceQuote] = []
        try:
            quotes.extend(self.web_search.search_competitor_offers(query))
            if not any(q.price is not None or q.cost is not None for q in quotes):
                quotes.extend(self.web_search.search_web_price_fallback(query))
        except Exception:
            logger.warning("Competitor lookup failed for %r", query, exc_info=True)
            return {"found": False, "items": []}

        items: list[dict[str, object]] = []
        seen_urls: set[str] = set()
        for quote in quotes:
            if not meets_web_display_threshold(quote.url, float(quote.match_score or 0)):
                continue
            if quote.url and quote.url in seen_urls:
                continue
            if quote.url:
                seen_urls.add(quote.url)

            base_price = quote.price if quote.price is not None else quote.cost
            price_kp = (
                round(base_price * (1 - WEB_PRICE_DISCOUNT_PERCENT / 100), 2)
                if base_price is not None
                else None
            )
            items.append(
                {
                    "label": quote.label or "Конкурент",
                    "name": quote.matched_name or query,
                    "match_score": round(float(quote.match_score or 0), 1),
                    "price": self._format_money(base_price),
                    "price_kp": self._format_money(price_kp),
                    "url": quote.url,
                    "notes": quote.notes or "",
                    "has_price": base_price is not None,
                    "_sort_price": base_price if base_price is not None else float("inf"),
                }
            )

        items.sort(key=lambda item: (0 if item.get("has_price") else 1, item["_sort_price"]))
        for item in items:
            item.pop("_sort_price", None)
        return {"found": bool(items), "items": items}

    @staticmethod
    def _resolve_display_name(
        best_hit: FuzzyHit | None,
        catalog_block: dict[str, object],
        price_block: dict[str, object],
        competitors_block: dict[str, object],
    ) -> str:
        if best_hit and best_hit.score >= SIMILAR_MATCH_THRESHOLD:
            return best_hit.name
        for block in (catalog_block, price_block, competitors_block):
            name = block.get("name")
            if isinstance(name, str) and name.strip():
                return name
            items = block.get("items")
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict) and first.get("name"):
                    return str(first["name"])
        return ""

    def _maybe_build_ai_insight(
        self,
        tz_item: TZItem,
        catalog_block: dict[str, object],
        price_block: dict[str, object],
        registry_block: dict[str, object],
        competitors_block: dict[str, object],
    ) -> dict[str, object]:
        if not self._all_sources_missing(
            catalog_block, price_block, registry_block, competitors_block
        ):
            return {"found": False, "requested": False}
        return self._build_ai_insight(tz_item)

    def _build_ai_insight(self, tz_item: TZItem) -> dict[str, object]:
        if not self.ai or not self.ai.enabled:
            return {
                "found": False,
                "requested": True,
                "available": False,
                "message": "Нейросеть недоступна — проверьте PROXYAPI_API_KEY в .env",
            }

        candidates = self.matcher.candidates_for_ai(tz_item)
        ai_result = self.ai.match_item(
            tz_item,
            candidates["catalog"],
            candidates["price"],
            candidates["registry"],
        )

        status = AIAgent.parse_status(ai_result.get("status", "not_found"))
        source = AIAgent.parse_source(ai_result.get("source", "none"))

        if status == MatchStatus.NOT_FOUND and source == MatchSource.NONE:
            ai_result = self.ai.estimate_web_price(tz_item)
            status = AIAgent.parse_status(ai_result.get("status", "not_found"))
            source = AIAgent.parse_source(ai_result.get("source", "none"))

        matched_name = str(ai_result.get("matched_name") or "").strip()
        notes = str(ai_result.get("notes") or "").strip()
        unit_cost_raw = ai_result.get("unit_cost")
        match_score = float(ai_result.get("match_score", 0) or 0)

        if status == MatchStatus.NOT_FOUND and not matched_name and unit_cost_raw is None:
            return {
                "found": False,
                "requested": True,
                "available": True,
                "message": notes or "Нейросеть не нашла подходящую информацию по запросу",
            }

        unit_cost = float(unit_cost_raw) if unit_cost_raw is not None else None
        unit_price_kp = (
            round(unit_cost * (1 + get_markup_percent() / 100), 2)
            if unit_cost is not None
            else None
        )

        source_labels = {
            MatchSource.WEB.value: "Оценка по открытым источникам",
            MatchSource.AI.value: "Подбор нейросетью",
            MatchSource.CATALOG.value: "Каталог",
            MatchSource.PRICE_LIST.value: "Прайс поставщика",
            MatchSource.REGISTRY.value: "Реестр остатков",
        }
        price_source = ProductLookupService._resolve_ai_price_source(
            ai_result, source, candidates, matched_name
        )
        raw_url = ProductLookupService._resolve_ai_product_url(
            ai_result, source, candidates, matched_name
        )
        product_url = ProductLookupService._accept_product_url(raw_url, source)
        search_query = matched_name or tz_item.name
        search_links = (
            ProductLookupService._marketplace_search_links(search_query)
            if not product_url
            else []
        )
        if product_url:
            price_source = ProductLookupService._clean_price_source_label(
                price_source, product_url
            )
        link_note = (
            "Прямая ссылка от AI не подтверждена — используйте поиск на площадках"
            if source == MatchSource.WEB and not product_url
            else None
        )

        return {
            "found": True,
            "requested": True,
            "available": True,
            "matched_name": matched_name,
            "unit_cost": ProductLookupService._format_money(unit_cost),
            "unit_price_kp": ProductLookupService._format_money(unit_price_kp),
            "match_score": round(match_score, 1),
            "source": source.value,
            "source_label": source_labels.get(source.value, "Нейросеть"),
            "price_source": price_source,
            "product_url": product_url,
            "search_links": search_links,
            "link_note": link_note,
            "notes": notes,
        }

    @staticmethod
    def _normalize_url(value: str) -> str | None:
        text = value.strip()
        if not text:
            return None
        if text.startswith(("http://", "https://")):
            return text
        if text.startswith("www.") or re.match(r"^[\w.-]+\.\w{2,}(/|\?|$)", text):
            return f"https://{text.lstrip('/')}"
        return None

    @staticmethod
    def _extract_url(value: str) -> str | None:
        match = re.search(r"https?://[^\s<>\"']+", value)
        if match:
            return match.group(0).rstrip(".,);]")
        normalized = ProductLookupService._normalize_url(value)
        if normalized and normalized.startswith("http"):
            return normalized
        return None

    @staticmethod
    def _is_product_url(url: str) -> bool:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = (parsed.path or "").strip("/")
        if parsed.query or parsed.fragment:
            return True
        if not path:
            return False
        home_paths = {"catalog", "search", "shop", "market", "products"}
        return path.lower() not in home_paths

    @staticmethod
    def _clean_price_source_label(text: str, product_url: str | None) -> str:
        cleaned = re.sub(r"https?://[^\s<>\"']+", "", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.—-")
        return cleaned or "Источник из базы"

    @staticmethod
    def _resolve_ai_product_url(
        ai_result: dict,
        source: MatchSource,
        candidates: dict,
        matched_name: str,
    ) -> str | None:
        for key in ("product_url", "source_url", "url", "link"):
            candidate = ProductLookupService._normalize_url(str(ai_result.get(key) or ""))
            if candidate and ProductLookupService._is_product_url(candidate):
                return candidate

        for field in (ai_result.get("price_source"), ai_result.get("notes")):
            candidate = ProductLookupService._extract_url(str(field or ""))
            if candidate and ProductLookupService._is_product_url(candidate):
                return candidate

        name_lower = matched_name.lower()
        if source == MatchSource.REGISTRY:
            for item in candidates.get("registry", []):
                item_name = str(item.get("name") or "")
                link = ProductLookupService._normalize_url(str(item.get("link") or ""))
                if not link or not ProductLookupService._is_product_url(link):
                    continue
                if name_lower and (
                    item_name.lower() == name_lower
                    or name_lower in item_name.lower()
                    or item_name.lower() in name_lower
                ):
                    return link

        return None

    @staticmethod
    def _accept_product_url(url: str | None, source: MatchSource) -> str | None:
        if not url or source == MatchSource.WEB:
            return None
        if ProductLookupService._validate_url(url):
            return url
        return None

    @staticmethod
    def _validate_url(url: str, timeout: float = 6.0) -> bool:
        try:
            import httpx

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            }
            with httpx.Client(
                follow_redirects=True,
                timeout=timeout,
                headers=headers,
            ) as client:
                for method in ("head", "get"):
                    response = client.request(method, url)
                    if response.status_code < 400:
                        return True
                    if response.status_code not in {403, 405}:
                        break
        except Exception:
            logger.debug("URL validation failed for %s", url, exc_info=True)
        return False

    @staticmethod
    def _marketplace_search_links(query: str) -> list[dict[str, str]]:
        from urllib.parse import quote_plus

        text = query.strip()
        if not text:
            return []
        encoded = quote_plus(text)
        return [
            {
                "label": "Ozon",
                "url": f"https://www.ozon.ru/search/?text={encoded}&from_global=true",
            },
            {
                "label": "Яндекс.Маркет",
                "url": f"https://market.yandex.ru/search?text={encoded}",
            },
        ]

    @staticmethod
    def _resolve_ai_price_source(
        ai_result: dict,
        source: MatchSource,
        candidates: dict,
        matched_name: str,
    ) -> str:
        explicit = str(
            ai_result.get("price_source") or ai_result.get("source_detail") or ""
        ).strip()
        if explicit:
            return explicit

        name_lower = matched_name.lower()
        if source == MatchSource.CATALOG:
            for item in candidates.get("catalog", []):
                item_name = str(item.get("name") or "")
                if name_lower and (
                    item_name.lower() == name_lower
                    or name_lower in item_name.lower()
                    or item_name.lower() in name_lower
                ):
                    return f"Каталог: {item_name}"
            return "Каталог"

        if source == MatchSource.PRICE_LIST:
            for item in candidates.get("price", []):
                item_name = str(item.get("name") or "")
                if name_lower and (
                    item_name.lower() == name_lower
                    or name_lower in item_name.lower()
                    or item_name.lower() in name_lower
                ):
                    supplier = item.get("supplier") or "прайс"
                    code = item.get("code") or ""
                    code_part = f", код {code}" if code else ""
                    return f"Прайс ({supplier}): {item_name}{code_part}"
            return "Прайс поставщика"

        if source == MatchSource.REGISTRY:
            for item in candidates.get("registry", []):
                item_name = str(item.get("name") or "")
                if name_lower and (
                    item_name.lower() == name_lower
                    or name_lower in item_name.lower()
                    or item_name.lower() in name_lower
                ):
                    return f"Реестр остатков: {item_name}"
            return "Реестр остатков"

        if source == MatchSource.WEB:
            notes = str(ai_result.get("notes") or "").strip()
            if notes and "источник" in notes.lower():
                return notes
            return notes or "Оценка по открытым источникам (конкретный сайт не указан)"

        return "Нейросеть"

    def _build_values(
        self,
        fields: list[LookupField],
        catalog_hit: FuzzyHit | None,
        registry_hit: FuzzyHit | None,
        price_hit: FuzzyHit | None,
    ) -> dict[LookupField, str]:
        values: dict[LookupField, str] = {}
        expanded = self._expand_price_fields(fields)

        for lookup_field in expanded:
            value = self._resolve_field(lookup_field, catalog_hit, registry_hit, price_hit)
            if value is not None:
                values[lookup_field] = value

        return values

    def _resolve_field(
        self,
        lookup_field: LookupField,
        catalog_hit: FuzzyHit | None,
        registry_hit: FuzzyHit | None,
        price_hit: FuzzyHit | None,
    ) -> Optional[str]:
        if lookup_field == LookupField.COST:
            return self._format_money(self._catalog_cost(catalog_hit))

        if lookup_field == LookupField.PRICE:
            if catalog_hit and catalog_hit.score >= SIMILAR_MATCH_THRESHOLD:
                item: CatalogItem = catalog_hit.payload
                if item.price is not None:
                    return self._format_money(item.price)
            return None

        if lookup_field == LookupField.PRICE_KP:
            base_price = self._price_for_markup(catalog_hit, price_hit)
            return self._format_money(
                round(base_price * (1 + get_markup_percent() / 100), 2)
                if base_price is not None
                else None
            )

        if lookup_field == LookupField.PRICE_SUPPLIER:
            if price_hit and price_hit.score >= SIMILAR_MATCH_THRESHOLD:
                item: PriceListItem = price_hit.payload
                return self._format_money(item.price)
            return None

        if lookup_field == LookupField.QUANTITY:
            parts: list[str] = []
            if registry_hit and registry_hit.score >= SIMILAR_MATCH_THRESHOLD:
                item: RegistryItem = registry_hit.payload
                parts.append(f"реестр: {self._format_qty(item.quantity)} шт.")
            if catalog_hit and catalog_hit.score >= SIMILAR_MATCH_THRESHOLD:
                cat: CatalogItem = catalog_hit.payload
                if cat.stock is not None:
                    parts.append(f"каталог: {self._format_qty(cat.stock)} шт.")
            return "; ".join(parts) if parts else "нет данных"

        if lookup_field == LookupField.UNIT:
            if catalog_hit and catalog_hit.score >= SIMILAR_MATCH_THRESHOLD:
                cat: CatalogItem = catalog_hit.payload
                return cat.unit or "шт."
            return "шт."

        if lookup_field == LookupField.SUPPLIER:
            if price_hit and price_hit.score >= SIMILAR_MATCH_THRESHOLD:
                item: PriceListItem = price_hit.payload
                return item.supplier
            return None

        if lookup_field == LookupField.CODE:
            if price_hit and price_hit.score >= SIMILAR_MATCH_THRESHOLD:
                item: PriceListItem = price_hit.payload
                return item.code
            return None

        if lookup_field == LookupField.MATCH:
            hits = [h for h in (catalog_hit, registry_hit, price_hit) if h]
            if not hits:
                return None
            best = max(hits, key=lambda h: h.score)
            return f"{best.score:.0f}%"

        return None

    @staticmethod
    def _expand_price_fields(fields: list[LookupField]) -> list[LookupField]:
        unique: list[LookupField] = []
        for item in fields:
            if item not in unique:
                unique.append(item)
        return unique

    @staticmethod
    def _catalog_cost(catalog_hit: FuzzyHit | None) -> Optional[float]:
        if not catalog_hit or catalog_hit.score < SIMILAR_MATCH_THRESHOLD:
            return None
        item: CatalogItem = catalog_hit.payload
        return item.cost

    @staticmethod
    def _price_for_markup(
        catalog_hit: FuzzyHit | None,
        price_hit: FuzzyHit | None,
    ) -> Optional[float]:
        if catalog_hit and catalog_hit.score >= SIMILAR_MATCH_THRESHOLD:
            item: CatalogItem = catalog_hit.payload
            if item.price is not None:
                return item.price
        if price_hit and price_hit.score >= SIMILAR_MATCH_THRESHOLD:
            item: PriceListItem = price_hit.payload
            return item.price
        return None

    @staticmethod
    def _is_relevant_hit(hit: FuzzyHit | None, primary: FuzzyHit) -> bool:
        if not hit or hit.score < SIMILAR_MATCH_THRESHOLD:
            return False
        if hit.name == primary.name:
            return True

        primary_tokens = set(normalize_name(primary.name).split())
        hit_tokens = set(normalize_name(hit.name).split())
        if not primary_tokens:
            return hit.score >= EXACT_MATCH_THRESHOLD

        overlap = len(primary_tokens & hit_tokens) / len(primary_tokens)
        return overlap >= 0.45 or hit.score >= EXACT_MATCH_THRESHOLD

    @staticmethod
    def _build_catalog_block(
        catalog_hit: FuzzyHit | None,
        candidates: list[FuzzyHit],
        query: str,
        matcher: ItemMatcher,
    ) -> dict[str, object]:
        ranked = matcher.rank_hits(query, candidates, limit=LOOKUP_MATCH_LIMIT)
        if not ranked:
            return {"found": False, "items": []}

        primary_name = catalog_hit.name if catalog_hit else ranked[0].name
        items: list[dict[str, object]] = []
        for hit in ranked:
            item: CatalogItem = hit.payload
            items.append(
                {
                    "name": hit.name,
                    "match_score": round(hit.score, 1),
                    "is_primary": hit.name == primary_name,
                    "cost": ProductLookupService._format_money(item.cost),
                    "price": ProductLookupService._format_money(item.price),
                    "stock": (
                        f"{ProductLookupService._format_qty(item.stock)} шт."
                        if item.stock is not None
                        else None
                    ),
                    "unit": item.unit or "шт",
                }
            )

        primary = next((item for item in items if item["is_primary"]), items[0])
        return {
            "found": True,
            "name": primary["name"],
            "match_score": primary["match_score"],
            "cost": primary.get("cost"),
            "price": primary.get("price"),
            "stock": primary.get("stock"),
            "unit": primary.get("unit"),
            "items": items,
        }

    @staticmethod
    def _build_price_block(
        price_hit: FuzzyHit | None,
        candidates: list[FuzzyHit],
        query: str,
        matcher: ItemMatcher,
    ) -> dict[str, object]:
        ranked = matcher.rank_hits(query, candidates, limit=LOOKUP_MATCH_LIMIT)
        if not ranked:
            return {"found": False, "items": []}

        primary_name = price_hit.name if price_hit else ranked[0].name
        items: list[dict[str, object]] = []
        for hit in ranked:
            item: PriceListItem = hit.payload
            items.append(
                {
                    "name": hit.name,
                    "match_score": round(hit.score, 1),
                    "is_primary": hit.name == primary_name,
                    "price": ProductLookupService._format_money(item.price),
                    "code": item.code,
                    "supplier": item.supplier,
                    "recommended_qty": ProductLookupService._format_price_qty(
                        item.recommended_qty
                    ),
                    "order_qty": ProductLookupService._format_price_qty(item.order_qty),
                    "order_sum": ProductLookupService._format_price_sum(
                        item.order_qty, item.order_sum
                    ),
                }
            )

        primary = next((item for item in items if item["is_primary"]), items[0])
        return {
            "found": True,
            "name": primary["name"],
            "match_score": primary["match_score"],
            "price": primary.get("price"),
            "code": primary.get("code"),
            "supplier": primary.get("supplier"),
            "recommended_qty": primary.get("recommended_qty"),
            "order_qty": primary.get("order_qty"),
            "order_sum": primary.get("order_sum"),
            "items": items,
        }

    @staticmethod
    def _build_registry_block(
        registry_hit: FuzzyHit | None,
        candidates: list[FuzzyHit],
        query: str,
        matcher: ItemMatcher,
    ) -> dict[str, object]:
        ranked = matcher.rank_hits(query, candidates, limit=LOOKUP_MATCH_LIMIT)
        if not ranked:
            return {
                "found": False,
                "message": REGISTRY_NOT_FOUND_MESSAGE,
                "items": [],
            }

        primary_name = registry_hit.name if registry_hit else ranked[0].name
        items: list[dict[str, object]] = []
        for hit in ranked:
            item: RegistryItem = hit.payload
            items.append(
                {
                    "name": hit.name,
                    "match_score": round(hit.score, 1),
                    "is_primary": hit.name == primary_name,
                    "quantity": ProductLookupService._format_qty(item.quantity),
                    "condition": item.condition,
                    "link": item.link,
                    "photo_files": list(item.photo_files),
                }
            )

        primary = next((item for item in items if item["is_primary"]), items[0])
        primary_item = next(
            (hit.payload for hit in ranked if hit.name == primary["name"]),
            ranked[0].payload,
        )
        if not isinstance(primary_item, RegistryItem):
            return {"found": False, "message": REGISTRY_NOT_FOUND_MESSAGE, "items": []}

        return {
            "found": True,
            "name": primary["name"],
            "match_score": primary["match_score"],
            "quantity": primary_item.quantity,
            "condition": primary_item.condition,
            "link": primary_item.link,
            "photo_files": list(primary_item.photo_files),
            "items": items,
        }

    @staticmethod
    def _format_price_qty(value: Optional[float]) -> str:
        if value is None:
            return "—"
        return f"{ProductLookupService._format_qty(value)} шт."

    @staticmethod
    def _format_price_sum(order_qty: Optional[float], order_sum: Optional[float]) -> str:
        if order_qty is not None and order_qty > 0:
            return ProductLookupService._format_money(order_sum or 0.0) or "—"
        if order_sum is not None and order_sum > 0:
            return ProductLookupService._format_money(order_sum) or "—"
        return "—"

    @staticmethod
    def registry_photo_url(photo_file: str | None) -> str | None:
        if not photo_file:
            return None
        return f"/api/registry/photos/{photo_file}"

    @staticmethod
    def registry_photo_urls(photo_files: list[str]) -> list[str]:
        return [
            url
            for photo in photo_files
            if (url := ProductLookupService.registry_photo_url(photo))
        ]

    def _best_hit(self, hits: list[FuzzyHit], query: str = "") -> FuzzyHit | None:
        if not hits:
            return None
        if query:
            mini = TZItem(number=0, name=query, unit="шт", quantity=1)
            return self.matcher.pick_best_hit(mini, hits)
        return hits[0]

    @staticmethod
    def _select_primary_hit(*hits: FuzzyHit | None) -> FuzzyHit | None:
        valid = [hit for hit in hits if hit]
        if not valid:
            return None
        return max(valid, key=lambda hit: (hit.score, len(hit.name)))

    @staticmethod
    def _build_sources(
        catalog_hit: FuzzyHit | None,
        registry_hit: FuzzyHit | None,
        price_hit: FuzzyHit | None,
        competitors_block: dict[str, object] | None = None,
    ) -> list[str]:
        sources: list[str] = []
        if catalog_hit and catalog_hit.score >= SIMILAR_MATCH_THRESHOLD:
            sources.append(f"Каталог: {catalog_hit.name}")
        if registry_hit and registry_hit.score >= SIMILAR_MATCH_THRESHOLD:
            sources.append(f"Реестр: {registry_hit.name}")
        if price_hit and price_hit.score >= SIMILAR_MATCH_THRESHOLD:
            item: PriceListItem = price_hit.payload
            sources.append(f"Прайс ({item.supplier}): {price_hit.name}")
        if competitors_block and competitors_block.get("found"):
            items = competitors_block.get("items")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    label = str(item.get("label") or "Конкурент")
                    name = str(item.get("name") or "")
                    sources.append(f"{label}: {name}")
        return sources

    @staticmethod
    def _collect_alternatives(candidates: dict, exclude: str = "") -> list[str]:
        names: list[str] = []
        for key in ("catalog", "registry", "price"):
            for hit in candidates.get(key, [])[1:4]:
                if hit.name != exclude and hit.name not in names:
                    names.append(hit.name)
        return names[:3]

    @staticmethod
    def _format_money(value: Optional[float]) -> Optional[str]:
        if value is None:
            return None
        return f"{value:,.2f} ₽".replace(",", " ")

    @staticmethod
    def _format_qty(value: float) -> str:
        if float(value).is_integer():
            return str(int(value))
        return str(value)


def parse_lookup_query(text: str) -> ProductQuery | None:
    raw = text.strip()
    if not raw:
        return None

    if raw.lower().startswith("/find"):
        raw = raw[5:].strip()

    fields_part = ""
    name_part = raw

    for separator in ("|", ":"):
        if separator in raw:
            left, right = raw.split(separator, 1)
            name_part = left.strip()
            fields_part = right.strip()
            break

    requested_fields = parse_requested_fields(fields_part or raw)
    product_name = extract_product_name(name_part, requested_fields)

    if len(product_name) < 2:
        return None

    if not requested_fields:
        requested_fields = list(DEFAULT_FIELDS)

    return ProductQuery(product_name=product_name, requested_fields=requested_fields)


def parse_requested_fields(text: str) -> list[LookupField]:
    lowered = text.lower()
    found: list[LookupField] = []

    if re.search(r"\b(?:цена|стоимость|сколько\s+стоит)\b", lowered):
        found.extend(
            [
                LookupField.COST,
                LookupField.PRICE,
                LookupField.PRICE_KP,
                LookupField.PRICE_SUPPLIER,
            ]
        )

    for lookup_field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", lowered):
                if lookup_field not in found:
                    found.append(lookup_field)

    unique: list[LookupField] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique


def extract_product_name(text: str, requested_fields: list[LookupField]) -> str:
    result = text

    for phrase in NOISE_PHRASES:
        result = re.sub(phrase, " ", result, flags=re.IGNORECASE)

    for aliases in FIELD_ALIASES.values():
        for alias in aliases:
            result = re.sub(rf"\b{re.escape(alias)}\b", " ", result, flags=re.IGNORECASE)

    for alias in PRICE_ALIASES:
        result = re.sub(rf"\b{re.escape(alias)}\b", " ", result, flags=re.IGNORECASE)

    result = re.sub(r"\b(?:и|или|а также)\b", " ", result, flags=re.IGNORECASE)
    result = re.sub(r"[,;]+", " ", result)
    result = re.sub(r"\s+", " ", result).strip(" .,-")
    return result


def is_lookup_message(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return stripped.lower().startswith("/find")
    return bool(LOOKUP_TRIGGER.search(stripped))


def is_bulk_kp_search_message(text: str) -> bool:
    return bool(BULK_KP_SEARCH.search(text.strip()))


def _ensure_price_lookup_fields(fields: list[LookupField]) -> list[LookupField]:
    unique = list(fields)
    for field in PRICE_LOOKUP_FIELDS:
        if field not in unique:
            unique.append(field)
    return unique


def _find_tz_item_in_message(message: str, tz_items: list[TZItem]) -> TZItem | None:
    msg_norm = normalize_name(message)
    if not msg_norm:
        return None

    best_item: TZItem | None = None
    best_score = 0

    for item in tz_items:
        name_norm = normalize_name(item.name)
        if len(name_norm) < 4:
            continue

        if name_norm in msg_norm or msg_norm in name_norm:
            score = len(name_norm)
            if score > best_score:
                best_item = item
                best_score = score
            continue

        tokens = [token for token in name_norm.split() if len(token) >= 4]
        if len(tokens) < 2:
            continue
        matched = sum(1 for token in tokens if token in msg_norm)
        threshold = max(2, int(len(tokens) * 0.6))
        if matched >= threshold:
            score = matched * 100 + len(name_norm)
            if score > best_score:
                best_item = item
                best_score = score

    return best_item


def resolve_kp_price_lookup(message: str, tz_items: list[TZItem]) -> ProductQuery | None:
    text = message.strip()
    if not text or is_bulk_kp_search_message(text):
        return None

    has_price_intent = bool(SINGLE_ITEM_PRICE_QUERY.search(text)) or is_lookup_message(text)
    if not has_price_intent:
        return None

    number_match = TZ_ITEM_NUMBER.search(text)
    if number_match and tz_items:
        try:
            item_number = int(number_match.group(1))
        except ValueError:
            item_number = 0
        tz_item = next((item for item in tz_items if item.number == item_number), None)
        if tz_item:
            parsed = parse_lookup_query(
                text if text.lower().startswith("/find") else f"/find {text}"
            )
            fields = (
                _ensure_price_lookup_fields(parsed.requested_fields)
                if parsed
                else list(PRICE_LOOKUP_FIELDS)
            )
            return ProductQuery(product_name=tz_item.name, requested_fields=fields)

    parsed = parse_lookup_query(
        text if text.lower().startswith("/find") else f"/find {text}"
    )
    if parsed and len(parsed.product_name) >= 2:
        return ProductQuery(
            product_name=parsed.product_name,
            requested_fields=_ensure_price_lookup_fields(parsed.requested_fields),
        )

    if tz_items:
        tz_item = _find_tz_item_in_message(text, tz_items)
        if tz_item:
            return ProductQuery(
                product_name=tz_item.name,
                requested_fields=list(PRICE_LOOKUP_FIELDS),
            )

    return None


def kp_lookup_reply(result: ProductLookupResult) -> str:
    if result.not_found and not ProductLookupService._any_source_found(
        result.catalog,
        result.price_list,
        result.registry,
        result.competitors,
    ):
        return (
            f"По запросу «{result.query_name}» ничего не найдено "
            "в каталоге, прайсах, реестре и на сайтах конкурентов."
        )

    name = result.matched_name or result.query_name
    score_part = f" ({result.match_score:.0f}%)" if result.match_score else ""
    return (
        f"Сводка по «{name}»{score_part}: каталог, прайсы, реестр "
        "и сайты конкурентов — см. таблицу ниже."
    )


def format_lookup_response(result: ProductLookupResult) -> str:
    if result.not_found:
        lines = [
            f"❌ Позиция *«{result.query_name}»* не найдена.",
            "",
            "Попробуйте уточнить название или проверьте /status.",
        ]
        if result.alternatives:
            lines.append("\n*Похожие позиции:*")
            for alt in result.alternatives:
                lines.append(f"• {alt}")
        lines.extend(["", *_format_ai_insight_block(result.ai_insight)])
        lines.extend(["", *_format_source_blocks(result)])
        return "\n".join(lines)

    status_label = (
        "точное совпадение"
        if result.status == MatchStatus.EXACT
        else "похожая позиция — проверьте"
    )
    icon = "✅" if result.status == MatchStatus.EXACT else "⚠️"

    lines = [
        f"{icon} *{result.matched_name}*",
        f"Запрос: _{result.query_name}_",
        f"Статус: {status_label} ({result.match_score:.0f}%)",
        "",
        "*Запрошенная информация:*",
    ]

    for lookup_field, value in result.values.items():
        label = FIELD_LABELS.get(lookup_field, lookup_field.value)
        lines.append(f"• {label}: *{value}*")

    if result.sources:
        lines.extend(["", "*Источники:*"])
        for source in result.sources:
            lines.append(f"• {source}")

    if result.alternatives and result.status != MatchStatus.EXACT:
        lines.extend(["", "*Альтернативы:*"])
        for alt in result.alternatives:
            lines.append(f"• {alt}")

    lines.extend(["", *_format_source_blocks(result)])
    return "\n".join(lines)


def _format_source_blocks(result: ProductLookupResult) -> list[str]:
    lines: list[str] = ["*Данные по источникам:*"]
    lines.extend(_format_catalog_source_lines(result.catalog))
    lines.extend(_format_price_source_lines(result.price_list))
    lines.extend(_format_registry_source_lines(result.registry))
    lines.extend(_format_competitors_source_lines(result.competitors))
    return lines


def _format_competitors_source_lines(competitors: dict[str, object]) -> list[str]:
    if not competitors.get("found"):
        return ["• Сайты конкурентов: не найдено"]

    items = competitors.get("items")
    if not isinstance(items, list) or not items:
        return ["• Сайты конкурентов: не найдено"]

    lines = ["• Сайты конкурентов:"]
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"  ◦ {item.get('label', 'Конкурент')}: {item.get('name')} "
            f"({item.get('match_score')}%)"
        )
        if item.get("price"):
            lines.append(f"    — цена: {item['price']}")
        elif item.get("has_price") is False:
            lines.append("    — цена не указана")
        if item.get("price_kp"):
            lines.append(f"    — цена КП (−{WEB_PRICE_DISCOUNT_PERCENT}%): {item['price_kp']}")
        if item.get("url"):
            lines.append(f"    — {item['url']}")
    return lines


def _format_catalog_source_lines(catalog: dict[str, object]) -> list[str]:
    if not catalog.get("found"):
        return ["• Каталог: не найдено"]

    items = catalog.get("items")
    if not isinstance(items, list) or not items:
        items = [catalog]

    lines = ["• Каталог:"]
    for item in items:
        if not isinstance(item, dict):
            continue
        marker = "★" if item.get("is_primary") else "◦"
        lines.append(f"  {marker} {item.get('name')} ({item.get('match_score')}%)")
        if item.get("cost"):
            lines.append(f"    — себест.: {item['cost']}")
        if item.get("price"):
            lines.append(f"    — цена: {item['price']}")
        if item.get("stock"):
            lines.append(f"    — остаток: {item['stock']}")
    return lines


def _format_price_source_lines(price: dict[str, object]) -> list[str]:
    if not price.get("found"):
        return ["• Прайс: не найдено"]

    items = price.get("items")
    if not isinstance(items, list) or not items:
        items = [price]

    lines = ["• Прайс:"]
    for item in items:
        if not isinstance(item, dict):
            continue
        marker = "★" if item.get("is_primary") else "◦"
        lines.append(
            f"  {marker} {item.get('name')} ({item.get('match_score')}%) — {item.get('price', '—')}"
        )
        if item.get("supplier"):
            lines.append(f"    — поставщик: {item['supplier']}")
    return lines


def _format_registry_source_lines(registry: dict[str, object]) -> list[str]:
    if not registry.get("found"):
        return [f"• Реестр: {registry.get('message', REGISTRY_NOT_FOUND_MESSAGE)}"]

    items = registry.get("items")
    if not isinstance(items, list) or not items:
        items = [registry]

    lines = ["• Реестр:"]
    for item in items:
        if not isinstance(item, dict):
            continue
        marker = "★" if item.get("is_primary") else "◦"
        lines.append(
            f"  {marker} {item.get('name')} ({item.get('match_score')}%) — "
            f"остаток {item.get('quantity', registry.get('quantity'))} шт."
        )
    return lines


def _format_ai_insight_block(ai_insight: dict[str, object]) -> list[str]:
    if not ai_insight.get("requested"):
        return []

    lines = ["*Найденная информация в базе от нейросети:*"]

    if not ai_insight.get("available"):
        lines.append(f"• {ai_insight.get('message', 'Нейросеть недоступна')}")
        return lines

    if not ai_insight.get("found"):
        lines.append(f"• {ai_insight.get('message', 'Информация не найдена')}")
        return lines

    if ai_insight.get("matched_name"):
        lines.append(
            f"• {ai_insight['matched_name']} ({ai_insight.get('match_score', 0)}%)"
        )
    if ai_insight.get("price_source"):
        lines.append(f"  — источник цены: {ai_insight['price_source']}")
    elif ai_insight.get("source_label"):
        lines.append(f"  — источник: {ai_insight['source_label']}")
    if ai_insight.get("product_url"):
        lines.append(f"  — ссылка: {ai_insight['product_url']}")
    search_links = ai_insight.get("search_links")
    if isinstance(search_links, list) and search_links:
        for item in search_links:
            if isinstance(item, dict) and item.get("url"):
                label = item.get("label") or "Поиск"
                lines.append(f"  — {label}: {item['url']}")
    if ai_insight.get("link_note"):
        lines.append(f"  — {ai_insight['link_note']}")
    if ai_insight.get("unit_cost"):
        lines.append(f"  — себестоимость: {ai_insight['unit_cost']}")
    if ai_insight.get("unit_price_kp"):
        lines.append(f"  — цена КП: {ai_insight['unit_price_kp']}")
    if ai_insight.get("notes"):
        lines.append(f"  — {ai_insight['notes']}")

    return lines
