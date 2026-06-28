import threading

from src.services.concurrency_limits import ai_request_slot
from src.services.kp_session import ChatTurn, KpSession, KpSessionStore, new_session_id
from src.services.models import ProposalSummary


def test_kp_session_store_thread_safe(tmp_path) -> None:
    store = KpSessionStore(path=tmp_path / "sessions.json")
    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            session = KpSession(
                session_id=new_session_id(),
                tz_items=[],
                results=[],
                summary=ProposalSummary(
                    total_items=0,
                    exact_count=0,
                    similar_count=0,
                    not_found_count=0,
                    total_cost=0,
                    total_base_price=0,
                    total_price=0,
                    processing_seconds=0,
                ),
                output_path=tmp_path / f"out-{index}.xlsx",
                use_ai=False,
                chat_history=[ChatTurn(role="user", text=f"msg-{index}")],
            )
            store.create(session)
            loaded = store.get(session.session_id)
            assert loaded is not None
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(store._sessions) == 12


def test_ai_request_slot_releases() -> None:
    with ai_request_slot(label="test"):
        pass
