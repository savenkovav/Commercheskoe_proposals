from __future__ import annotations

from src.services.data_loader import normalize_name
from src.services.models import CatalogItem, KitComponentLine


class CatalogStructure:
    def __init__(self, catalog: list[CatalogItem]) -> None:
        self.catalog = catalog
        self._by_name = {normalize_name(item.name): item for item in catalog}

    def find_item(self, name: str) -> CatalogItem | None:
        return self._by_name.get(normalize_name(name))

    def kit_breakdown(self, matched: CatalogItem) -> list[KitComponentLine]:
        if matched.entry_type == "kit_total":
            return self._components_for_kit_total(matched)
        if matched.entry_type == "sub_kit":
            return [
                KitComponentLine(
                    name=matched.name,
                    unit_cost=matched.cost,
                    unit_price=matched.price,
                    quantity=1.0,
                )
            ]
        return []

    def _components_for_kit_total(self, kit_total: CatalogItem) -> list[KitComponentLine]:
        if not kit_total.components_group:
            return []
        return self._components_in_group(kit_total.components_group, exclude_kit_totals=True)

    def _components_in_group(
        self,
        group_name: str,
        exclude_kit_totals: bool = False,
    ) -> list[KitComponentLine]:
        lines: list[KitComponentLine] = []
        in_group = False
        for item in self.catalog:
            if item.entry_type == "components_header" and item.name == group_name:
                in_group = True
                continue
            if not in_group:
                continue
            if item.entry_type == "components_header" and item.name != group_name:
                break
            if item.entry_type == "section":
                break
            if exclude_kit_totals and item.entry_type == "kit_total":
                continue
            if item.cost is None and item.price is None:
                continue
            if item.entry_type in {"sub_kit", "item"}:
                lines.append(
                    KitComponentLine(
                        name=item.name,
                        unit_cost=item.cost,
                        unit_price=item.price,
                        quantity=1.0,
                    )
                )
        return lines

    def aggregate_kit_cost(self, components: list[KitComponentLine]) -> float | None:
        if not components:
            return None
        total = 0.0
        for line in components:
            if line.unit_cost is None:
                return None
            total += line.unit_cost * line.quantity
        return round(total, 2)
