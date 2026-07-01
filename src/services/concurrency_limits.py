"""Глобальные лимиты параллельности для многопользовательского режима."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from src.config import AI_MAX_CONCURRENT_REQUESTS

logger = logging.getLogger(__name__)

_ai_semaphore = threading.Semaphore(AI_MAX_CONCURRENT_REQUESTS)
AI_QUEUE_TIMEOUT_SECONDS = 120.0
_ai_stats_lock = threading.Lock()
_ai_in_flight = 0
_ai_wait_timeouts = 0


def ai_concurrency_stats() -> dict[str, int | float]:
    with _ai_stats_lock:
        return {
            "max_concurrent": AI_MAX_CONCURRENT_REQUESTS,
            "in_flight": _ai_in_flight,
            "queue_timeouts": _ai_wait_timeouts,
        }


@contextmanager
def ai_request_slot(*, label: str = "ai"):
    global _ai_in_flight, _ai_wait_timeouts
    acquired = _ai_semaphore.acquire(timeout=AI_QUEUE_TIMEOUT_SECONDS)
    if not acquired:
        with _ai_stats_lock:
            _ai_wait_timeouts += 1
        logger.warning("AI request queue timeout (%s)", label)
        raise TimeoutError(
            "Превышено время ожидания ответа нейросети — попробуйте через минуту"
        )
    with _ai_stats_lock:
        _ai_in_flight += 1
    try:
        yield
    finally:
        with _ai_stats_lock:
            _ai_in_flight = max(0, _ai_in_flight - 1)
        _ai_semaphore.release()
