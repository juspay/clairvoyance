"""The waiting-call hooks: the CRM states its facts without importing the
dialler. Unregistered nothing takes a call; a failing hook never raises."""

from datetime import datetime, timezone
from typing import Any, List

import pytest

from app.crm.outreach import waiting_calls as call_queue
from app.crm.outreach.waiting_calls import WaitingCall

REQUEST = WaitingCall(
    lead_id="lead-1",
    template_id="tpl-1",
    run_id="run-1",
    ready_at=datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc),
    rank=2,
    order="newest_event",
    event_at=None,
)


@pytest.fixture(autouse=True)
def unregistered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(call_queue, "_hooks", None)


async def test_with_no_hook_nothing_is_queued_and_nothing_raises() -> None:
    assert await call_queue.call_waits(REQUEST) is False
    await call_queue.call_left("tpl-1", "lead-1")
    await call_queue.call_reranked("tpl-1", "lead-1", 1, "first_ready", None)


async def test_registered_hooks_receive_the_calls() -> None:
    seen: List[Any] = []

    async def queue(request: WaitingCall) -> bool:
        seen.append(request)
        return True

    async def withdraw(template_id: str, lead_id: str) -> None:
        seen.append((template_id, lead_id))

    async def rerank(*args: Any) -> None:
        seen.append(args)

    call_queue.register(queue, withdraw, rerank)
    assert await call_queue.call_waits(REQUEST) is True
    await call_queue.call_left("tpl-1", "lead-1")
    await call_queue.call_reranked("tpl-1", "lead-1", 1, "first_ready", None)
    assert seen == [
        REQUEST,
        ("tpl-1", "lead-1"),
        ("tpl-1", "lead-1", 1, "first_ready", None),
    ]


async def test_a_failing_hook_never_raises_into_the_caller() -> None:
    async def boom(*args: Any) -> Any:
        raise RuntimeError("redis is down")

    call_queue.register(boom, boom, boom)
    assert await call_queue.call_waits(REQUEST) is False  # made today's way
    await call_queue.call_left("tpl-1", "lead-1")
    await call_queue.call_reranked("tpl-1", "lead-1", 1, "first_ready", None)


async def test_rerank_carries_the_next_day_rank_only_when_there_is_one() -> None:
    """What a live call falls to tomorrow reaches the hook as keywords; a pile
    call has none, and a hook written before them is called as it always was."""
    seen: List[Any] = []

    async def rerank(*args: Any, **later: Any) -> None:
        seen.append((args, later))

    call_queue.register(rerank, rerank, rerank)
    await call_queue.call_reranked(
        "tpl-1",
        "lead-1",
        1,
        "first_ready",
        None,
        next_rank=2,
        next_order="newest_event",
    )
    await call_queue.call_reranked("tpl-1", "lead-1", 3, "newest_event", None)

    live = ("tpl-1", "lead-1", 1, "first_ready", None)
    assert seen == [
        (live, {"next_rank": 2, "next_order": "newest_event"}),
        (("tpl-1", "lead-1", 3, "newest_event", None), {}),
    ]
