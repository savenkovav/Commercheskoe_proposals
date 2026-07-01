"""Сводная информация о нагрузке приложения для admin dashboard."""

from __future__ import annotations

import os
import resource
import time
from typing import Any

from src.config import (
    AI_MAX_CONCURRENT_REQUESTS,
    INDEX_MAX_CONCURRENT_JOBS,
    KP_MAX_SESSIONS,
)
from src.services.competitor_index_queue import get_index_queue_stats
from src.services.concurrency_limits import ai_concurrency_stats

_PROCESS_START = time.time()


def _memory_usage_mb() -> float | None:
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # macOS reports bytes, Linux kilobytes
        if os.uname().sysname == "Darwin":
            return round(usage.ru_maxrss / (1024 * 1024), 1)
        return round(usage.ru_maxrss / 1024, 1)
    except Exception:
        return None


def get_system_status() -> dict[str, Any]:
    from src.services.kp_session_db import get_kp_session_database

    uptime = round(time.time() - _PROCESS_START, 1)
    index_stats = get_index_queue_stats()
    ai_stats = ai_concurrency_stats()
    session_stats = get_kp_session_database().stats()

    return {
        "uptime_seconds": uptime,
        "memory_mb": _memory_usage_mb(),
        "pid": os.getpid(),
        "limits": {
            "ai_max_concurrent_requests": AI_MAX_CONCURRENT_REQUESTS,
            "index_max_concurrent_jobs": INDEX_MAX_CONCURRENT_JOBS,
            "kp_max_sessions": KP_MAX_SESSIONS,
        },
        "ai": ai_stats,
        "index_queue": index_stats,
        "kp_sessions": session_stats,
    }
