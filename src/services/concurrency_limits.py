"""Глобальные лимиты параллельности для многопользовательского режима."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from src.config import AI_MAX_CONCURRENT_REQUESTS

logger = logging.getLogger(__name__)

_ai_semaphore = threading.Semaphore(AI_MAX_CONCURRENT_REQUESTS)
AI_QUEUE_TIMEOUT_SECONDS = 120.0


@contextmanager
def ai_request_slot(*, label: str = "ai"):
    acquired = _ai_semaphore.acquire(timeout=AI_QUEUE_TIMEOUT_SECONDS)
    if not acquired:
        logger.warning("AI request queue timeout (%s)", label)
        raise TimeoutError(
            "Превышено время ожидания ответа нейросети — попробуйте через минуту"
        )
    try:
        yield
    finally:
        _ai_semaphore.release()
