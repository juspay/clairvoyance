"""Client tools: browser-run tools riding the HITL approval gate."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.chat import approvals
from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent
from app.ai.voice.agents.breeze_buddy.chat.tools.client_tools import (
    CLIENT_TOOL_EXPIRED_RESULT,
    GET_PAGE_CONTEXT,
    build_client_tool_functions,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
)
from app.schemas.breeze_buddy.chat import (
    CLIENT_TOOL_RESULT_MAX_BYTES,
    ApproveToolRequest,
    ToolApproval,
    ToolApprovalStatus,
)

PAGE = {"url": "https://shop.example/products/tee", "page_type": "product"}


def _row(function_name: str, *, expired: bool = False) -> ToolApproval:
    now = datetime.now(timezone.utc)
    return ToolApproval(
        id="a1",
        session_id="s1",
        tool_call_id="tc1",
        function_name=function_name,
        status=ToolApprovalStatus.PENDING,
        requested_at=now,
        expires_at=now + (timedelta(seconds=-1) if expired else timedelta(minutes=1)),
    )


@pytest.fixture
def claim_db(monkeypatch):
    """Fake the approval DB; returns the list of persisted tool_result rows."""
    written = []
    state = {}

    async def _get(session_id, tool_call_id):
        return state["row"]

    async def _decide(session_id, tool_call_id, status, reason):
        return state["row"].model_copy(update={"status": status})

    async def _insert(**kwargs):
        written.append(kwargs["content_blocks"])

    async def _none(*args, **kwargs):
        return []

    monkeypatch.setattr(approvals, "get_tool_approval", _get)
    monkeypatch.setattr(approvals, "decide_tool_approval", _decide)
    monkeypatch.setattr(approvals, "insert_chat_message", _insert)
    monkeypatch.setattr(approvals, "resolve_dangling_approvals", _none)
    monkeypatch.setattr(approvals, "list_pending_tool_approvals", _none)
    return state, written


@pytest.mark.parametrize("client_tools", [True, False])
async def test_client_tools_published_only_when_enabled(client_tools):
    # Voice turns pass client_tools=False: nothing on a call answers them yet.
    template = TemplateModel.model_construct(
        id="t1",
        name="t",
        flow={"mode": "direct", "functions": [], "system_prompt": "x"},
        configurations=ConfigurationModel(client_tools=[GET_PAGE_CONTEXT]),
    )
    agent = ChatAgent(
        session_id="s1", template=template, llm=object(), client_tools=client_tools
    )
    prep = await agent._prepare_tools()
    names = [fn.name for fn in prep.global_funcs]
    assert (GET_PAGE_CONTEXT in names) is client_tools
    assert (GET_PAGE_CONTEXT in agent._approval_map) is client_tools
    # Older page reads shrink once superseded (stale pages, token cost).
    retention = prep.tool_retention or {}
    assert (retention.get(GET_PAGE_CONTEXT) == "last_turn_only") is client_tools


async def test_client_tool_name_clash_is_not_published(monkeypatch):
    # Two functions with one name are rejected by some LLM providers: the
    # template's own function wins, the client tool is skipped.
    own = build_client_tool_functions([GET_PAGE_CONTEXT])[0]
    monkeypatch.setattr(
        FlowConfigBuilder, "build_global_functions", lambda *a, **k: [own]
    )
    template = TemplateModel.model_construct(
        id="t1",
        name="t",
        flow={"mode": "direct", "functions": [], "system_prompt": "x"},
        configurations=ConfigurationModel(client_tools=[GET_PAGE_CONTEXT]),
    )
    agent = ChatAgent(session_id="s1", template=template, llm=object())

    prep = await agent._prepare_tools()

    names = [fn.name for fn in prep.global_funcs]
    assert names.count(GET_PAGE_CONTEXT) == 1
    assert GET_PAGE_CONTEXT not in agent._approval_map


async def test_client_tool_result_becomes_the_tool_result(claim_db):
    state, written = claim_db
    state["row"] = _row(GET_PAGE_CONTEXT)

    page = {**PAGE, "secret": "x"}

    claim = await approvals.claim_tool_approval("s1", "tc1", True, None, page)

    assert claim.outcome == "proceed"
    # Nothing executes server-side: the resume turn takes the result as given.
    assert claim.effective_approved is False
    assert claim.wire_status == "approved"
    assert len(written) == 1
    # Read as untrusted data, and only the known keys pass.
    result = claim.synthetic_result
    assert result is not None
    assert "untrusted" in result["note"].lower()
    assert result["url"] == PAGE["url"]
    assert "secret" not in result


async def test_client_tool_without_result_is_an_error(claim_db):
    state, _ = claim_db
    state["row"] = _row(GET_PAGE_CONTEXT)

    claim = await approvals.claim_tool_approval("s1", "tc1", True, None)

    assert claim.effective_approved is False
    assert claim.synthetic_result is not None
    assert claim.synthetic_result["status"] == "error"


async def test_expired_client_tool_times_out(claim_db):
    state, _ = claim_db
    state["row"] = _row(GET_PAGE_CONTEXT, expired=True)

    claim = await approvals.claim_tool_approval("s1", "tc1", True, None, PAGE)

    assert claim.wire_status == "timeout"
    # No one was asked anything: the model must not say the shopper declined.
    assert claim.synthetic_result == CLIENT_TOOL_EXPIRED_RESULT


async def test_result_is_ignored_for_server_tools(claim_db):
    state, written = claim_db
    state["row"] = _row("refund_order")

    claim = await approvals.claim_tool_approval("s1", "tc1", True, None, PAGE)

    # The browser cannot answer for a server tool: it still executes.
    assert claim.effective_approved is True
    assert claim.synthetic_result is None
    assert written == []


def test_result_size_is_capped():
    big = {"text": "x" * CLIENT_TOOL_RESULT_MAX_BYTES}
    with pytest.raises(ValidationError):
        ApproveToolRequest(tool_call_id="tc1", approved=True, client_tool_result=big)
    ApproveToolRequest(tool_call_id="tc1", approved=True, client_tool_result=PAGE)
