from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.config import (
    CATALOG_PATH,
    REGISTRY_PATH,
    REGISTRY_PHOTOS_DIR,
    SOURCES_REGISTRY_PATH,
)
from src.services.data_loader import load_catalog, load_registry

logger = logging.getLogger(__name__)

STATIC_SOURCE_IDS = frozenset({"catalog", "registry"})


@dataclass(frozen=True)
class StaticSourceConfig:
    source_id: str
    default_name: str
    type_label: str
    path: Path


STATIC_SOURCES: dict[str, StaticSourceConfig] = {
    "catalog": StaticSourceConfig(
        source_id="catalog",
        default_name="Каталог",
        type_label="Каталог",
        path=CATALOG_PATH,
    ),
    "registry": StaticSourceConfig(
        source_id="registry",
        default_name="Реестр остатков",
        type_label="Реестр остатков",
        path=REGISTRY_PATH,
    ),
}


class StaticSourceManager:
    def __init__(self, registry_path: Path = SOURCES_REGISTRY_PATH) -> None:
        self.registry_path = registry_path
        self._meta: dict[str, dict[str, str]] = {}
        self._load_meta()

    def _load_meta(self) -> None:
        if not self.registry_path.exists():
            self._meta = {}
            return

        with open(self.registry_path, encoding="utf-8") as f:
            data = json.load(f)

        raw = data.get("sources", {})
        self._meta = {
            key: {"name": str(value.get("name", "")).strip()}
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    def _save_meta(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sources": {
                source_id: {"name": meta["name"]}
                for source_id, meta in self._meta.items()
                if meta.get("name")
            }
        }
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def is_static_source(source_id: str) -> bool:
        return source_id.lower().strip() in STATIC_SOURCE_IDS

    def get_config(self, source_id: str) -> StaticSourceConfig:
        normalized = source_id.lower().strip()
        config = STATIC_SOURCES.get(normalized)
        if not config:
            raise ValueError(f"Источник '{source_id}' не найден")
        return config

    def get_display_name(self, source_id: str) -> str:
        config = self.get_config(source_id)
        stored = self._meta.get(config.source_id, {}).get("name", "").strip()
        return stored or config.default_name

    def update_name(self, source_id: str, name: str) -> StaticSourceConfig:
        config = self.get_config(source_id)
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Укажите название")

        self._meta[config.source_id] = {"name": cleaned}
        self._save_meta()
        return config

    def validate_file(self, source_id: str, path: Path) -> tuple[int, str | None]:
        config = self.get_config(source_id)
        suffix = path.suffix.lower()
        if suffix != ".xlsx":
            return 0, "Допустим только файл .xlsx"

        try:
            if config.source_id == "catalog":
                items = load_catalog(path)
            else:
                items = load_registry(path, REGISTRY_PHOTOS_DIR)
        except Exception as exc:
            return 0, f"Не удалось прочитать файл: {exc}"

        if not items:
            return 0, "В файле не найдено позиций"

        return len(items), None

    def replace_file(self, source_id: str, source_path: Path) -> int:
        config = self.get_config(source_id)
        count, error = self.validate_file(source_id, source_path)
        if error:
            raise ValueError(error)

        config.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, config.path)
        logger.info("Replaced %s at %s (%s items)", config.source_id, config.path, count)
        return count

    def remove(self, source_id: str) -> StaticSourceConfig:
        config = self.get_config(source_id)
        if config.path.exists():
            config.path.unlink()
            logger.info("Removed %s file %s", config.source_id, config.path)
        return config

    def file_updated_at(self, source_id: str) -> str | None:
        config = self.get_config(source_id)
        if not config.path.exists():
            return None
        return datetime.fromtimestamp(
            config.path.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat()

    def to_dict(self, source_id: str, items_count: int) -> dict[str, object]:
        config = self.get_config(source_id)
        return {
            "id": config.source_id,
            "type": config.source_id,
            "type_label": config.type_label,
            "name": self.get_display_name(source_id),
            "filename": config.path.name,
            "items_count": items_count,
            "exists": config.path.exists(),
            "updated_at": self.file_updated_at(source_id),
        }


_manager: StaticSourceManager | None = None


def get_static_source_manager() -> StaticSourceManager:
    global _manager
    if _manager is None:
        _manager = StaticSourceManager()
    return _manager
