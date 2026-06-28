from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MatchStatus(str, Enum):
    EXACT = "exact"
    SIMILAR = "similar"
    NOT_FOUND = "not_found"


class MatchSource(str, Enum):
    CATALOG = "catalog"
    REGISTRY = "registry"
    PRICE_LIST = "price_list"
    WEB = "web"
    AI = "ai"
    NONE = "none"


@dataclass
class CatalogItem:
    name: str
    cost: Optional[float]
    price: Optional[float] = None
    unit: str = "шт"
    stock: Optional[float] = None
    source_file: str = "catalog"
    actual_markup_pct: Optional[float] = None
    entry_type: str = "item"
    components_group: Optional[str] = None
    row_index: int = 0
    supplier: Optional[str] = None
    supplier_note: Optional[str] = None


@dataclass
class GoodsReportItem:
    name: str
    supplier: Optional[str] = None
    purchase_date: Optional[str] = None
    cost: Optional[float] = None
    price: Optional[float] = None
    unit: str = "шт"
    source_file: str = "goods_report"


@dataclass
class PriceQuote:
    source: str
    label: str
    matched_name: str = ""
    price: Optional[float] = None
    cost: Optional[float] = None
    price_label: Optional[str] = None
    wholesale_price: Optional[float] = None
    articul: Optional[str] = None
    supplier: Optional[str] = None
    purchase_date: Optional[str] = None
    match_score: float = 0.0
    url: Optional[str] = None
    notes: str = ""
    image_url: Optional[str] = None
    description: Optional[str] = None


@dataclass
class KitComponentLine:
    name: str
    unit_cost: Optional[float] = None
    unit_price: Optional[float] = None
    quantity: float = 1.0
    supplier: Optional[str] = None
    price_list_price: Optional[float] = None
    purchase_date: Optional[str] = None
    competitor_url: Optional[str] = None
    competitor_platform: Optional[str] = None
    found_in_catalog: bool = False
    catalog_matched_name: Optional[str] = None


@dataclass
class RegistryItem:
    name: str
    quantity: float
    condition: Optional[str] = None
    link: Optional[str] = None
    photo_files: list[str] = field(default_factory=list)


@dataclass
class PriceListItem:
    code: str
    name: str
    price: float
    sheet: str
    supplier: str
    recommended_qty: Optional[float] = None
    order_qty: Optional[float] = None
    order_sum: Optional[float] = None


@dataclass
class TZItem:
    number: int
    name: str
    unit: str
    quantity: float
    specifications: str = ""
    country_of_origin: str = ""


@dataclass
class MatchResult:
    tz_item: TZItem
    status: MatchStatus
    source: MatchSource
    matched_name: str = ""
    match_score: float = 0.0
    unit_cost: Optional[float] = None
    unit_base_price: Optional[float] = None
    unit_price: Optional[float] = None
    total_cost: Optional[float] = None
    total_price: Optional[float] = None
    notes: str = ""
    source_detail: str = ""
    alternatives: list[str] = field(default_factory=list)
    supplier: Optional[str] = None
    purchase_date: Optional[str] = None
    comparison: list[PriceQuote] = field(default_factory=list)
    competitors: list[PriceQuote] = field(default_factory=list)
    kit_components: list[KitComponentLine] = field(default_factory=list)
    price_list_check: Optional[PriceQuote] = None
    is_kit: bool = False
    internet_priced: bool = False
    applied_markup_pct: Optional[float] = None


@dataclass
class ProposalSummary:
    total_items: int
    exact_count: int
    similar_count: int
    not_found_count: int
    total_cost: float
    total_base_price: float
    total_price: float
    processing_seconds: float
