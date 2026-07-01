from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.services.user_db import ROLE_MANAGER, UserDatabase


@pytest.fixture
def db() -> UserDatabase:
    with tempfile.TemporaryDirectory() as tmp:
        yield UserDatabase(Path(tmp) / "users.db")


def _create_manager(db: UserDatabase, login: str = "mgr") -> int:
    user = db.create_user(login, "hash", "salt", role=ROLE_MANAGER)
    return user.id


def test_delete_download_event_removes_row(db: UserDatabase) -> None:
    user_id = _create_manager(db)
    export_id = db.record_file_export(
        user_id,
        session_id="sess",
        tz_filename="tz.docx",
        xlsx_filename="KP_test.xlsx",
        pdf_filename="KP_test.pdf",
    )
    event_id = db.record_download(
        user_id,
        filename="KP_test.xlsx",
        file_type="xlsx",
        export_id=export_id,
    )

    result = db.delete_download_event(event_id, acting_user_id=user_id)

    assert result is not None
    assert result["id"] == event_id
    assert db.get_download_event(event_id) is None
    assert not db.list_download_history(user_id=user_id)


def test_delete_download_event_denies_other_manager(db: UserDatabase) -> None:
    owner_id = _create_manager(db, "owner")
    other_id = _create_manager(db, "other")
    event_id = db.record_download(
        owner_id,
        filename="KP_test.xlsx",
        file_type="xlsx",
    )

    with pytest.raises(PermissionError):
        db.delete_download_event(event_id, acting_user_id=other_id)

    assert db.get_download_event(event_id) is not None


def test_delete_last_event_removes_export_metadata(db: UserDatabase) -> None:
    user_id = _create_manager(db)
    export_id = db.record_file_export(
        user_id,
        session_id="sess",
        tz_filename="tz.docx",
        xlsx_filename="KP_pair.xlsx",
        pdf_filename="KP_pair.pdf",
    )
    xlsx_event = db.record_download(
        user_id,
        filename="KP_pair.xlsx",
        file_type="xlsx",
        export_id=export_id,
    )
    db.record_download(
        user_id,
        filename="KP_pair.pdf",
        file_type="pdf",
        export_id=export_id,
    )

    db.delete_download_event(xlsx_event, acting_user_id=user_id)
    assert len(db.list_download_history(user_id=user_id)) == 1

    pdf_event = db.list_download_history(user_id=user_id)[0].id
    result = db.delete_download_event(pdf_event, acting_user_id=user_id)

    assert result is not None
    assert set(result["files_to_remove"]) == {"KP_pair.pdf", "KP_pair.xlsx"}
