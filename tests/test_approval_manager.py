"""Voice ApprovalManager lifecycle — exactly-once resolution emission,
idempotent resolve, timeout/cancel/supersede/deny_all paths.

Companion: [[agent/approval.py]]. The gate/_make_global_wrapper/
_flows_async_kwargs tests live in tests/test_approval_gate.py.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from app.ai.voice.agents.breeze_buddy.agent.approval import (
    RTVI_APPROVAL_REQUEST,
    RTVI_APPROVAL_RESOLVED,
    ApprovalManager,
)
from app.ai.voice.agents.breeze_buddy.template.types import ApprovalConfig


class _EmitRecorder:
    def __init__(self):
        self.events: List[tuple] = []

    async def __call__(self, event_type, payload=None):
        self.events.append((event_type, payload))


async def test_manager_approve_resolves_and_emits_once():
    emit = _EmitRecorder()
    manager = ApprovalManager(emit=emit)
    cfg = ApprovalConfig(timeout_secs=5)

    task = asyncio.create_task(manager.request("fn", {"a": 1}, cfg))
    await asyncio.sleep(0.01)
    request_events = [e for e in emit.events if e[0] == RTVI_APPROVAL_REQUEST]
    assert len(request_events) == 1
    approval_id = request_events[0][1]["approval_id"]

    assert manager.resolve(approval_id, True) is True
    outcome = await task
    assert outcome.approved is True and outcome.status == "approved"
    resolved = [e for e in emit.events if e[0] == RTVI_APPROVAL_RESOLVED]
    assert len(resolved) == 1
    assert resolved[0][1] == {"approval_id": approval_id, "status": "approved"}
    assert not manager.has_pending()


async def test_manager_duplicate_resolve_is_stale():
    emit = _EmitRecorder()
    manager = ApprovalManager(emit=emit)
    task = asyncio.create_task(
        manager.request("fn", {}, ApprovalConfig(timeout_secs=5))
    )
    await asyncio.sleep(0.01)
    approval_id = emit.events[0][1]["approval_id"]
    assert manager.resolve(approval_id, False, "no") is True
    assert manager.resolve(approval_id, True) is False  # idempotent
    outcome = await task
    assert outcome.status == "denied" and outcome.reason == "no"


async def test_manager_timeout_then_late_resolve_is_stale():
    emit = _EmitRecorder()
    manager = ApprovalManager(emit=emit)
    outcome = await manager.request("fn", {}, ApprovalConfig(timeout_secs=0.01))
    assert outcome.status == "timeout" and outcome.approved is False
    approval_id = emit.events[0][1]["approval_id"]
    # wait_for cancelled the future; a late decision must be a clean no-op.
    assert manager.resolve(approval_id, True) is False
    assert not manager.has_pending()


async def test_manager_supersede_on_identical_recall():
    """An identical re-call (same function AND same args) supersedes the
    stale pending request."""
    emit = _EmitRecorder()
    manager = ApprovalManager(emit=emit)
    cfg = ApprovalConfig(timeout_secs=5)
    first = asyncio.create_task(manager.request("fn", {"v": 1}, cfg))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(manager.request("fn", {"v": 1}, cfg))
    await asyncio.sleep(0.01)

    first_outcome = await first
    assert first_outcome.status == "superseded" and first_outcome.approved is False

    second_id = [e for e in emit.events if e[0] == RTVI_APPROVAL_REQUEST][1][1][
        "approval_id"
    ]
    manager.resolve(second_id, True)
    second_outcome = await second
    assert second_outcome.approved is True
    assert not manager.has_pending()


async def test_manager_supersede_emits_resolved_before_new_request():
    """On supersede the wire order is request(old) -> resolved(old) ->
    request(new): the old card is dismissed before its replacement appears,
    and the superseded id resolves exactly once."""
    emit = _EmitRecorder()
    manager = ApprovalManager(emit=emit)
    cfg = ApprovalConfig(timeout_secs=5)
    first = asyncio.create_task(manager.request("fn", {"v": 1}, cfg))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(manager.request("fn", {"v": 1}, cfg))
    await asyncio.sleep(0.01)

    first_outcome = await first
    assert first_outcome.status == "superseded"

    # request(old), resolved(old, superseded), request(new) — in that order.
    assert [e[0] for e in emit.events] == [
        RTVI_APPROVAL_REQUEST,
        RTVI_APPROVAL_RESOLVED,
        RTVI_APPROVAL_REQUEST,
    ]
    first_id = emit.events[0][1]["approval_id"]
    assert emit.events[1][1] == {"approval_id": first_id, "status": "superseded"}

    second_id = emit.events[2][1]["approval_id"]
    assert second_id != first_id
    manager.resolve(second_id, True)
    assert (await second).approved is True

    # The superseded id never resolves twice (eager emit + finally-skip).
    resolved_first = [
        e
        for e in emit.events
        if e[0] == RTVI_APPROVAL_RESOLVED and e[1]["approval_id"] == first_id
    ]
    assert len(resolved_first) == 1
    assert not manager.has_pending()


async def test_manager_no_supersede_on_distinct_parallel_args():
    """Two distinct parallel calls to the same function (DIFFERENT args, e.g.
    two add_to_cart) each keep their own pending card — neither supersedes the
    other (the parallel-drop bug fix)."""
    emit = _EmitRecorder()
    manager = ApprovalManager(emit=emit)
    cfg = ApprovalConfig(timeout_secs=5)
    first = asyncio.create_task(manager.request("add_to_cart", {"item": "A"}, cfg))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(manager.request("add_to_cart", {"item": "B"}, cfg))
    await asyncio.sleep(0.01)

    # Both pending — no supersede, and nothing resolved yet.
    assert manager.has_pending()
    req_events = [e for e in emit.events if e[0] == RTVI_APPROVAL_REQUEST]
    assert len(req_events) == 2
    assert [e for e in emit.events if e[0] == RTVI_APPROVAL_RESOLVED] == []

    # Each resolves independently to its own decision.
    ids = [e[1]["approval_id"] for e in req_events]
    manager.resolve(ids[0], True)
    manager.resolve(ids[1], False)
    o1 = await first
    o2 = await second
    assert o1.approved is True
    assert o2.approved is False
    assert not manager.has_pending()


async def test_manager_deny_all():
    emit = _EmitRecorder()
    manager = ApprovalManager(emit=emit)
    cfg = ApprovalConfig(timeout_secs=5)
    t1 = asyncio.create_task(manager.request("fn_a", {}, cfg))
    t2 = asyncio.create_task(manager.request("fn_b", {}, cfg))
    await asyncio.sleep(0.01)
    assert manager.has_pending()
    manager.deny_all("client_disconnected")
    o1, o2 = await t1, await t2
    assert o1.status == "denied" and o2.status == "denied"
    assert o1.reason == "client_disconnected"
    assert not manager.has_pending()


async def test_manager_cancelled_emits_cancelled_and_reraises():
    emit = _EmitRecorder()
    manager = ApprovalManager(emit=emit)
    task = asyncio.create_task(
        manager.request("fn", {}, ApprovalConfig(timeout_secs=5))
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    resolved = [e for e in emit.events if e[0] == RTVI_APPROVAL_RESOLVED]
    assert len(resolved) == 1
    assert resolved[0][1]["status"] == "cancelled"
    assert not manager.has_pending()


async def test_manager_pending_requests_for_reemit():
    emit = _EmitRecorder()
    manager = ApprovalManager(emit=emit)
    task = asyncio.create_task(
        manager.request("fn", {"a": 1}, ApprovalConfig(timeout_secs=5))
    )
    await asyncio.sleep(0.01)
    payloads = manager.pending_requests()
    assert len(payloads) == 1
    assert payloads[0]["function_name"] == "fn"
    manager.resolve(payloads[0]["approval_id"], False)
    await task
