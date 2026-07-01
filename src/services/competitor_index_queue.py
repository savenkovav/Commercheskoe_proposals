"""Очередь фоновых индексаций каталогов конкурентов."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from src.config import INDEX_MAX_CONCURRENT_JOBS

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_queue: deque[dict[str, object]] = deque()
_jobs: dict[str, dict[str, object]] = {}
_on_job_finished: Callable[[str, Any], None] | None = None


def bind_job_registry(jobs: dict[str, dict[str, object]]) -> None:
    global _jobs
    _jobs = jobs


def set_job_finished_handler(handler: Callable[[str, Any], None]) -> None:
    global _on_job_finished
    _on_job_finished = handler


def _count_running_unlocked() -> int:
    return sum(1 for job in _jobs.values() if job.get("running"))


def _refresh_queue_positions() -> None:
    for index, spec in enumerate(_queue, start=1):
        domain = str(spec.get("domain", ""))
        job = _jobs.get(domain)
        if job and job.get("queued"):
            job["queue_position"] = index


def get_index_queue_stats() -> dict[str, object]:
    with _lock:
        queued = [
            {
                "domain": str(spec.get("domain", "")),
                "kind": spec.get("kind"),
                "queue_position": index,
                "queued_at": spec.get("queued_at"),
            }
            for index, spec in enumerate(_queue, start=1)
        ]
        running = [
            {
                "domain": domain,
                "phase": job.get("phase"),
                "started_at": job.get("started_at"),
                "is_builtin": job.get("is_builtin"),
            }
            for domain, job in _jobs.items()
            if job.get("running")
        ]
        return {
            "max_concurrent_jobs": INDEX_MAX_CONCURRENT_JOBS,
            "running_count": len(running),
            "queued_count": len(queued),
            "running": running,
            "queued": queued,
        }


def submit_index_job(
    domain: str,
    spec: dict[str, object],
    *,
    initial_job: dict[str, object],
    start_worker: Callable[[dict[str, object]], None],
    queued_log: Callable[[str, int], None],
) -> dict[str, object]:
    normalized = domain.lower().removeprefix("www.")
    spec = {**spec, "domain": normalized}

    with _lock:
        existing = _jobs.get(normalized)
        if existing and (existing.get("running") or existing.get("queued")):
            return {
                "started": False,
                "running": bool(existing.get("running")),
                "queued": bool(existing.get("queued")),
                "domain": normalized,
                "queue_position": existing.get("queue_position"),
                "message": (
                    "Индексация уже выполняется"
                    if existing.get("running")
                    else f"Уже в очереди (позиция {existing.get('queue_position')})"
                ),
                **existing,
            }

        if _count_running_unlocked() >= INDEX_MAX_CONCURRENT_JOBS:
            spec["queued_at"] = time.time()
            _queue.append(spec)
            position = len(_queue)
            _jobs[normalized] = {
                **initial_job,
                "domain": normalized,
                "running": False,
                "queued": True,
                "queue_position": position,
                "phase": "queued",
                "queued_at": spec["queued_at"],
            }
            _refresh_queue_positions()
            queued_log(normalized, position)
            return {
                "started": False,
                "running": False,
                "queued": True,
                "domain": normalized,
                "queue_position": position,
                "phase": "queued",
                "message": f"Задача в очереди индексации (позиция {position})",
            }

        _jobs[normalized] = {
            **initial_job,
            "domain": normalized,
            "running": True,
            "queued": False,
            "started_at": time.time(),
        }

    start_worker(spec)
    return {
        "started": True,
        "running": True,
        "queued": False,
        "domain": normalized,
        **{key: value for key, value in _jobs.get(normalized, {}).items() if key != "result"},
    }


def drain_index_queue(
    *,
    start_worker: Callable[[dict[str, object]], None],
    queued_log: Callable[[str, int], None],
) -> None:
    to_start: list[dict[str, object]] = []
    with _lock:
        while _count_running_unlocked() < INDEX_MAX_CONCURRENT_JOBS and _queue:
            spec = _queue.popleft()
            domain = str(spec.get("domain", ""))
            if not domain:
                continue
            existing = _jobs.get(domain)
            if existing and existing.get("running"):
                continue
            _jobs[domain] = {
                "domain": domain,
                "running": True,
                "queued": False,
                "phase": "starting",
                "started_at": time.time(),
                "is_builtin": spec.get("is_builtin"),
            }
            to_start.append(spec)
        _refresh_queue_positions()
        for index, queued_spec in enumerate(_queue, start=1):
            queued_domain = str(queued_spec.get("domain", ""))
            if queued_domain:
                queued_log(queued_domain, index)

    for spec in to_start:
        domain = str(spec.get("domain", ""))
        logger.info("Dequeuing index job for %s", domain)
        start_worker(spec)


def notify_index_job_finished(domain: str, doc_rag_index: Any) -> None:
    if _on_job_finished is None:
        return
    _on_job_finished(domain, doc_rag_index)
