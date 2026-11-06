"""Chat terminal approval sweep — terminate_pending_approvals.

The chat counterpart of voice's ApprovalManager.deny_all on a terminal event:
when the idle-cleanup task ends a session, any still-PENDING approvals must be
resolved (EXPIRED) instead of left dangling. The two DB calls
(claim_pending_tool_approvals + insert_chat_message) are monkeypatched so this
is a pure logic test.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.ai.voice.agents.breeze_buddy.chat import approvals as approvals_mod
from app.ai.voice.agents.breeze_buddy.chat.approvals import terminate_pending_approvals
from app.schemas.breeze_buddy.chat import ToolApprovalStatus


async def test_terminate_claims_all_pending_as_expired(monkeypatch):
    claim: dict = {}
    inserted: dict = {}

    async def fake_claim(*, session_id, new_status, reason, only_expired):
        claim.update(
            session_id=session_id, new_status=new_status, only_expired=only_expired
        )
        return [SimpleNamespace(tool_call_id="t1"), SimpleNamespace(tool_call_id="t2")]

    async def fake_insert(*, session_id, role, content, content_blocks):
        inserted.update(session_id=session_id, content_blocks=content_blocks)

    monkeypatch.setattr(approvals_mod, "claim_pending_tool_approvals", fake_claim)
    monkeypatch.setattr(approvals_mod, "insert_chat_message", fake_insert)

    claimed = await terminate_pending_approvals("sess-1")

    # Claims ALL pending (not only_expired) and marks them EXPIRED — the
    # terminal status (vs SUPERSEDED for a mid-session new message).
    assert claim["new_status"] == ToolApprovalStatus.EXPIRED
    assert claim["only_expired"] is False
    assert claim["session_id"] == "sess-1"
    assert [r.tool_call_id for r in claimed] == ["t1", "t2"]
    # One coalesced synthetic tool_result row answers both claimed approvals,
    # keeping the dangling-tool_use invariant intact on the ended session.
    assert inserted["session_id"] == "sess-1"
    assert inserted["content_blocks"]


async def test_terminate_noop_when_nothing_pending(monkeypatch):
    insert_calls = {"n": 0}

    async def fake_claim(**_kwargs):
        return []

    async def fake_insert(**_kwargs):
        insert_calls["n"] += 1

    monkeypatch.setattr(approvals_mod, "claim_pending_tool_approvals", fake_claim)
    monkeypatch.setattr(approvals_mod, "insert_chat_message", fake_insert)

    claimed = await terminate_pending_approvals("sess-2")
    assert claimed == []
    assert insert_calls["n"] == 0  # no synthetic row written when nothing pending
