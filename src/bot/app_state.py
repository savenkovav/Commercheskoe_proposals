from __future__ import annotations

from src.services.proposal_processor import ProposalProcessor

_processor: ProposalProcessor | None = None


def get_processor() -> ProposalProcessor:
    global _processor
    if _processor is None:
        _processor = ProposalProcessor()
    return _processor


def reload_processor(bot_data: dict | None = None) -> int:
    processor = get_processor()
    processor.reload_catalog()
    processor.reload_registry()
    return processor.reload_price_lists()
