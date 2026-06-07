#!/usr/bin/env python3
"""CLI-демо: обработка sample_tz.docx без Telegram и без AI (только локальный поиск)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.services.proposal_processor import ProposalProcessor  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Демо-генерация КП из ТЗ")
    parser.add_argument(
        "--tz",
        type=Path,
        default=PROJECT_ROOT / "data" / "sample_tz.docx",
        help="Путь к файлу ТЗ (.docx)",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Отключить AI-подбор (только fuzzy-поиск)",
    )
    args = parser.parse_args()

    processor = ProposalProcessor()
    output_path, summary, results = processor.process_tz_file(
        args.tz,
        use_ai=not args.no_ai,
    )

    print(f"\n{'='*60}")
    print(processor.format_summary_text(summary).replace("*", ""))
    print(f"{'='*60}")
    print(f"\nФайл сохранён: {output_path}\n")

    print("Детализация:")
    for r in results:
        status_icon = {"exact": "✅", "similar": "⚠️", "not_found": "❌"}[r.status.value]
        cost = f"{r.unit_cost:.2f}" if r.unit_cost else "—"
        price = f"{r.unit_price:.2f}" if r.unit_price else "—"
        print(
            f"{status_icon} {r.tz_item.number}. {r.tz_item.name[:50]}"
            f" → {r.matched_name[:50] if r.matched_name else '—'}"
            f" | score={r.match_score:.0f} | себест={cost} | цена={price}"
        )


if __name__ == "__main__":
    main()
