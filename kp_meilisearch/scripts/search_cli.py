#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MEILI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MEILI_ROOT))

from kp_search.config import MeiliSettings  # noqa: E402
from kp_search.search import ProductSearchEngine  # noqa: E402


def main() -> int:
    query = " ".join(sys.argv[1:]).strip()
    if not query:
        print("Usage: search_cli.py <query>", file=sys.stderr)
        return 1

    engine = ProductSearchEngine(MeiliSettings.from_env())
    if not engine.enabled:
        print("MEILISEARCH_ENABLED=false", file=sys.stderr)
        return 2

    for source in (None, "catalog", "registry", "price_list"):
        label = source or "all"
        hits = engine.search(query, source=source, limit=5)
        print(f"\n[{label}] {len(hits)} hits")
        for hit in hits:
            print(f"  {hit.score:5.1f} | {hit.source:11} | {hit.name[:70]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
