#!/usr/bin/env python3
"""Проверка обезличивания ПДн перед отправкой в OpenAI."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.services.pii_anonymizer import PIIAnonymizer  # noqa: E402


def main() -> None:
    anonymizer = PIIAnonymizer(
        enabled=True,
        redact_org_data=True,
        org_terms=["ООО «Учтендер»", "4727015060"],
    )
    session = anonymizer.new_session()

    sample = (
        "Контакт: ivanov@company.ru, тел. +7 (999) 123-45-67. "
        "ИНН 4727015060, ОГРН 125470001348. "
        "Адрес: г. Тихвин, ул. Карла Маркса, д. 86д. "
        "Ответственный: Петров Пётр Петрович."
    )
    redacted = anonymizer.anonymize_text(sample, session)

    checks = [
        ("email removed", "@" not in redacted),
        ("phone removed", "999" not in redacted),
        ("inn removed", "4727015060" not in redacted),
        ("fio removed", "Петров" not in redacted),
        ("placeholders added", "[" in redacted),
    ]

    print("Original:", sample)
    print("Redacted:", redacted)
    print("Replacements:", session.replacements)
    print()

    failed = [name for name, ok in checks if not ok]
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)

    restored = anonymizer.deanonymize_text(redacted, session)
    response = anonymizer.deanonymize_response(
        {
            "matched_name": redacted,
            "notes": "Связаться: [EMAIL_1]",
            "alternatives": [],
        },
        session,
    )
    assert "@" in response["notes"]
    assert "ivanov@company.ru" in restored
    print("All checks passed")


if __name__ == "__main__":
    main()
