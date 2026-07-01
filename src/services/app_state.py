from __future__ import annotations

import threading

from src.services.proposal_processor import ProposalProcessor

_processor: ProposalProcessor | None = None
_processor_lock = threading.RLock()


def get_processor() -> ProposalProcessor:
    global _processor
    with _processor_lock:
        if _processor is None:
            _processor = ProposalProcessor()
        return _processor


def reload_processor() -> int:
    with _processor_lock:
        processor = get_processor()
        processor.reload_catalog()
        processor.reload_registry()
        return processor.reload_price_lists()
