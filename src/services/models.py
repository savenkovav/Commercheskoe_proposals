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
