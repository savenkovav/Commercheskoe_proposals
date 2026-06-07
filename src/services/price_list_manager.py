from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.config import PRICE_LIST_PATHS, PRICES_DIR, PRICES_REGISTRY_PATH
from src.services.data_loader import load_price_list

logger = logging.getLogger(__name__)

SKIP_SHEETS = {"Лист1", "ПРОСМОТР ЗАКАЗА"}


@dataclass
class PriceListEntry:
    id: str
    name: str
    supplier: str
    filename: str
    items_count: int
    updated_at: str

    @classmethod
    def from_dict(cls, data: dict) -> PriceListEntry:
        return cls(
            id=data["id"],
            name=data["name"],
            supplier=data["supplier"],
            filename=data["filename"],
            items_count=int(data.get("items_count", 0)),
            updated_at=data.get("updated_at", ""),
        )


class PriceListManager:
    def __init__(
        self,
        registry_path: Path = PRICES_REGISTRY_PATH,
        prices_dir: Path = PRICES_DIR,
    ) -> None:
        self.registry_path = registry_path
        self.prices_dir = prices_dir
        self.prices_dir.mkdir(parents=True, exist_ok=True)
        self._entries: list[PriceListEntry] = []
        self._load_registry()
        if not self._entries:
            self._seed_from_env()
            self._save_registry()

    def _load_registry(self) -> None:
        if not self.registry_path.exists():
            self._entries = []
            return

        with open(self.registry_path, encoding="utf-8") as f:
            data = json.load(f)

        self._entries = [PriceListEntry.from_dict(item) for item in data.get("price_lists", [])]

    def _save_registry(self) -> None:
        payload = {
            "price_lists": [asdict(entry) for entry in self._entries],
        }
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _seed_from_env(self) -> None:
        for path in PRICE_LIST_PATHS:
            if not path.exists():
                continue

            name = path.stem.replace("_", " ").replace("-", " ").title()
            supplier = name
            entry_id = self._make_id(name)

            dest = self._store_file(entry_id, path)
            items_count = len(load_price_list(dest, supplier=supplier))

            self._entries.append(
                PriceListEntry(
                    id=entry_id,
                    name=name,
                    supplier=supplier,
                    filename=dest.name,
                    items_count=items_count,
                    updated_at=self._now(),
                )
            )
            logger.info("Seeded price list %s from %s", entry_id, path)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _make_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40]
        if not base:
            base = "price"

        existing = {entry.id for entry in self._entries}
        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    def list_entries(self) -> list[PriceListEntry]:
        return list(self._entries)

    def get_entry(self, price_id: str) -> PriceListEntry | None:
        normalized = price_id.lower().strip()
        for entry in self._entries:
            if entry.id.lower() == normalized:
                return entry
        return None

    def file_path(self, entry: PriceListEntry) -> Path:
        return self.prices_dir / entry.filename

    def validate_price_file(self, path: Path, supplier: str) -> tuple[int, str | None]:
        suffix = path.suffix.lower()
        if suffix not in {".xls", ".xlsx"}:
            return 0, "Допустимы только файлы .xls и .xlsx"

        try:
            items = load_price_list(path, supplier=supplier)
        except Exception as exc:
            return 0, f"Не удалось прочитать прайс: {exc}"

        if not items:
            return 0, "В файле не найдено позиций с ценами"

        return len(items), None

    def _store_file(self, entry_id: str, source_path: Path) -> Path:
        suffix = source_path.suffix.lower()
        dest = self.prices_dir / f"{entry_id}{suffix}"
        shutil.copy2(source_path, dest)
        return dest

    def add(self, name: str, supplier: str, source_path: Path) -> PriceListEntry:
        name = name.strip()
        supplier = supplier.strip() or name
        if not name:
            raise ValueError("Укажите название прайса")

        count, error = self.validate_price_file(source_path, supplier)
        if error:
            raise ValueError(error)

        entry_id = self._make_id(name)
        dest = self._store_file(entry_id, source_path)

        entry = PriceListEntry(
            id=entry_id,
            name=name,
            supplier=supplier,
            filename=dest.name,
            items_count=count,
            updated_at=self._now(),
        )
        self._entries.append(entry)
        self._save_registry()
        return entry

    def replace(self, price_id: str, source_path: Path) -> PriceListEntry:
        entry = self.get_entry(price_id)
        if not entry:
            raise ValueError(f"Прайс '{price_id}' не найден")

        count, error = self.validate_price_file(source_path, entry.supplier)
        if error:
            raise ValueError(error)

        old_path = self.file_path(entry)
        if old_path.exists() and old_path.suffix.lower() != source_path.suffix.lower():
            old_path.unlink()

        dest = self._store_file(entry.id, source_path)

        entry.filename = dest.name
        entry.items_count = count
        entry.updated_at = self._now()
        self._save_registry()
        return entry

    def update_meta(self, price_id: str, name: str | None = None, supplier: str | None = None) -> PriceListEntry:
        entry = self.get_entry(price_id)
        if not entry:
            raise ValueError(f"Прайс '{price_id}' не найден")

        if name:
            entry.name = name.strip()
        if supplier:
            entry.supplier = supplier.strip()

        entry.updated_at = self._now()
        self._save_registry()
        return entry

    def remove(self, price_id: str) -> PriceListEntry:
        entry = self.get_entry(price_id)
        if not entry:
            raise ValueError(f"Прайс '{price_id}' не найден")

        file_path = self.file_path(entry)
        if file_path.exists():
            file_path.unlink()

        self._entries = [item for item in self._entries if item.id != entry.id]
        self._save_registry()
        return entry

    def load_all_items(self) -> list:
        items = []
        for entry in self._entries:
            path = self.file_path(entry)
            if path.exists():
                items.extend(load_price_list(path, supplier=entry.supplier))
        return items

    def format_list_text(self) -> str:
        if not self._entries:
            return "📋 *Прайсы:*\n\nПрайс-листы не загружены."

        lines = ["📋 *Загруженные прайсы:*\n"]
        for entry in self._entries:
            updated = entry.updated_at[:10] if entry.updated_at else "—"
            lines.append(
                f"• `{entry.id}` — *{entry.name}*\n"
                f"  Поставщик: {entry.supplier}\n"
                f"  Позиций: {entry.items_count} | Обновлён: {updated}"
            )
        return "\n".join(lines)


_manager: PriceListManager | None = None


def get_price_list_manager() -> PriceListManager:
    global _manager
    if _manager is None:
        _manager = PriceListManager()
    return _manager
