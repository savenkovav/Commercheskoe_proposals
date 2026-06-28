from pathlib import Path

import pytest

from src.services.models import TZItem
from src.services.tz_parser import parse_tz
from src.services.tz_search import (
    combined_match_score,
    is_relevant_match,
    spec_identity_tokens,
    spec_required_tokens,
)

TZ_DOC = Path(
    "/Users/aleksandrsavenkov/Desktop/Проект КП/archive/"
    "Описание объекта закупки (1) (1).docx"
)


@pytest.fixture(scope="module")
def microphone_item() -> TZItem:
    if not TZ_DOC.exists():
        pytest.skip(f"TZ file not found: {TZ_DOC}")
    return parse_tz(TZ_DOC)[1]


def test_microphone_spec_tokens(microphone_item: TZItem) -> None:
    required = spec_required_tokens(microphone_item)
    identity = spec_identity_tokens(microphone_item)
    assert "behringer" in required
    assert "ulm300usb" in required
    assert "behringer" in identity
    assert "ulm300usb" in identity


def test_yamaha_microphone_rejected(microphone_item: TZItem) -> None:
    yamaha = "Yamaha Микрофон для живого вокала DM-105, черный"
    assert not is_relevant_match(microphone_item, yamaha, score=100.0)
    assert combined_match_score(microphone_item, yamaha) < 95.0


def test_behringer_other_model_rejected(microphone_item: TZItem) -> None:
    behringer_speaker = (
        "Behringer B208D Активная AC, 200 Вт., 8 дюймов ] 01634"
    )
    assert not is_relevant_match(microphone_item, behringer_speaker, score=80.0)


def test_behringer_ulm300usb_accepted(microphone_item: TZItem) -> None:
    match = "Behringer ULM300USB цифровая вокальная радиосистема"
    assert is_relevant_match(microphone_item, match, score=95.0)


def test_specific_name_still_matches_without_identity_in_candidate() -> None:
    """При конкретном наименовании в ТЗ логика не ужесточается до identity-токенов."""
    item = TZItem(
        number=1,
        name="Behringer ULM300USB микрофон",
        specifications="Behringer ULM300USB цифровая вокальная радиосистема",
        quantity=1,
        unit="шт",
    )
    candidate = "Behringer ULM300USB беспроводной микрофон"
    assert is_relevant_match(item, candidate, score=100.0)
