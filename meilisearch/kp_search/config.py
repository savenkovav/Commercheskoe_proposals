from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class MeiliSettings:
    enabled: bool
    host: str
    api_key: str
    index_name: str
    auto_sync: bool
    search_limit: int

    @classmethod
    def from_env(cls) -> "MeiliSettings":
        return cls(
            enabled=os.getenv("MEILISEARCH_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"},
            host=os.getenv("MEILISEARCH_HOST", "http://127.0.0.1:7700").rstrip("/"),
            api_key=os.getenv("MEILISEARCH_API_KEY", "masterKey"),
            index_name=os.getenv("MEILISEARCH_INDEX", "products"),
            auto_sync=os.getenv("MEILISEARCH_AUTO_SYNC", "true").lower()
            in {"1", "true", "yes", "on"},
            search_limit=max(5, int(os.getenv("MEILISEARCH_SEARCH_LIMIT", "20"))),
        )
