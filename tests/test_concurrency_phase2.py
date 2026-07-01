import json
import threading
import time

import pytest

from src.services import competitor_index_queue as index_queue
from src.services.kp_session_db import KpSessionDatabase
from src.services.system_status import get_system_status


@pytest.fixture
def index_jobs():
    jobs: dict[str, dict[str, object]] = {}
    index_queue.bind_job_registry(jobs)
    with index_queue._lock:
        index_queue._queue.clear()
    yield jobs
    with index_queue._lock:
        index_queue._queue.clear()
        jobs.clear()


def test_index_queue_waits_when_limit_reached(index_jobs, monkeypatch) -> None:
    monkeypatch.setattr("src.services.competitor_index_queue.INDEX_MAX_CONCURRENT_JOBS", 1)

    started: list[str] = []

    def start_worker(spec: dict[str, object]) -> None:
        started.append(str(spec["domain"]))

    index_jobs["site-a.example"] = {"domain": "site-a.example", "running": True}

    first = index_queue.submit_index_job(
        "site-b.example",
        {"kind": "reindex"},
        initial_job={"phase": "catalog"},
        start_worker=start_worker,
        queued_log=lambda _domain, _pos: None,
    )
    assert first["queued"] is True
    assert first["queue_position"] == 1
    assert started == []

    index_jobs["site-a.example"]["running"] = False
    index_queue.drain_index_queue(
        start_worker=start_worker,
        queued_log=lambda _domain, _pos: None,
    )
    assert started == ["site-b.example"]
    assert index_jobs["site-b.example"]["running"] is True


def test_index_queue_starts_immediately_when_slot_free(index_jobs, monkeypatch) -> None:
    monkeypatch.setattr("src.services.competitor_index_queue.INDEX_MAX_CONCURRENT_JOBS", 1)

    started: list[str] = []

    result = index_queue.submit_index_job(
        "site-c.example",
        {"kind": "reindex"},
        initial_job={"phase": "catalog"},
        start_worker=lambda spec: started.append(str(spec["domain"])),
        queued_log=lambda _domain, _pos: None,
    )
    assert result["started"] is True
    assert result["queued"] is False
    assert started == ["site-c.example"]


def test_kp_session_db_migrates_legacy_json(tmp_path, monkeypatch) -> None:
    json_path = tmp_path / "kp_sessions.json"
    db_path = tmp_path / "kp_sessions.db"
    json_path.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "session_id": "abc123",
                        "created_at": time.time(),
                        "tz_items": [],
                        "results": [],
                        "summary": {},
                        "output_path": str(tmp_path / "out.xlsx"),
                        "use_ai": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.services.kp_session_db.KP_SESSIONS_PATH", json_path)
    monkeypatch.setattr("src.services.kp_session_db.KP_SESSIONS_DB_PATH", db_path)

    db = KpSessionDatabase(db_path=db_path)
    assert db.count_sessions() == 1
    payload = db.get_payload("abc123")
    assert payload is not None
    assert payload["session_id"] == "abc123"


def test_get_system_status_shape() -> None:
    status = get_system_status()
    assert "uptime_seconds" in status
    assert "limits" in status
    assert "ai" in status
    assert "index_queue" in status
    assert "kp_sessions" in status
    assert status["index_queue"]["max_concurrent_jobs"] >= 1


def test_index_queue_stats_thread_safe(index_jobs) -> None:
    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            index_queue.get_index_queue_stats()
            index_queue.submit_index_job(
                f"site-{index}.example",
                {"kind": "reindex"},
                initial_job={"phase": "catalog"},
                start_worker=lambda _spec: None,
                queued_log=lambda _domain, _pos: None,
            )
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
