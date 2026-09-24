"""Client tools — the browser-executed gate.

The contract under test is that a client tool reuses the APPROVAL gate rather
than growing a second pause/resume path: same partition, same pending row,
same claim, and only the answer differs. Each test below pins one half of
that — the gate closing, or the answer reopening it.

The tool itself returns one fact: the URL of the page the shopper has open.
Everything richer belongs to the catalog tools, which are authoritative where
a shopper-editable page is not.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional, cast

from app.ai.voice.agents.breeze_buddy.chat.agent import _partition_gated_calls
from app.ai.voice.agents.breeze_buddy.chat.tools.client_tools import (
    CLIENT_TOOL_BUILDERS,
    GET_CURRENT_PAGE_PRODUCT_TOOL_NAME,
    KNOWN_CLIENT_TOOLS,
    build_get_current_page_product_schema,
    enabled_client_tools,
)
from app.schemas.breeze_buddy.chat import (
    MAX_CLIENT_TOOL_RESULT_BYTES,
    SubmitClientToolResultRequest,
)


def _call(name: str):
    return SimpleNamespace(function_name=name, tool_call_id=f"tc_{name}", arguments={})


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------


def test_the_list_is_the_switch():
    """No second on/off flag: a template offers a client tool by naming it."""
    assert enabled_client_tools(SimpleNamespace(client_tools=[])) == frozenset()
    assert enabled_client_tools(
        SimpleNamespace(client_tools=[GET_CURRENT_PAGE_PRODUCT_TOOL_NAME])
    ) == frozenset({GET_CURRENT_PAGE_PRODUCT_TOOL_NAME})


def test_an_unknown_name_is_dropped_not_published():
    """A typo must not put a tool on the wire that nothing can execute —
    the agent would call it and the turn would hang until expiry."""
    cfg = SimpleNamespace(client_tools=["get_current_page_ur", "perform_page_task"])
    assert enabled_client_tools(cfg) == frozenset()


def test_a_template_with_no_configurations_offers_none():
    assert enabled_client_tools(None) == frozenset()


def test_every_known_tool_can_actually_be_built():
    """KNOWN_CLIENT_TOOLS is what the config validates against; the builders
    map is what the turn publishes. A name in one and not the other is a
    KeyError mid-turn."""
    assert set(CLIENT_TOOL_BUILDERS) == set(KNOWN_CLIENT_TOOLS)


# ---------------------------------------------------------------------------
# the gate closing
# ---------------------------------------------------------------------------


def test_a_client_tool_is_gated_so_it_never_dispatches_here():
    gated, ungated = _partition_gated_calls(
        [_call(GET_CURRENT_PAGE_PRODUCT_TOOL_NAME), _call("search_catalog")],
        {},
        {},
        frozenset({GET_CURRENT_PAGE_PRODUCT_TOOL_NAME}),
    )
    assert [c.function_name for c in gated] == [GET_CURRENT_PAGE_PRODUCT_TOOL_NAME]
    assert [c.function_name for c in ungated] == ["search_catalog"]


def test_node_shadowing_does_not_free_a_client_tool():
    """A per-node function of the same name relaxes an APPROVAL gate. It must
    not relax this one: the reason a client tool cannot run here is that the
    server has no page to read, which no node can change."""

    class _Schema:
        name = GET_CURRENT_PAGE_PRODUCT_TOOL_NAME

        def __init__(self):
            pass

    node = {"functions": [_Schema()]}
    gated, _ = _partition_gated_calls(
        [_call(GET_CURRENT_PAGE_PRODUCT_TOOL_NAME)],
        {},
        node,
        frozenset({GET_CURRENT_PAGE_PRODUCT_TOOL_NAME}),
    )
    assert len(gated) == 1


def test_no_client_tools_leaves_the_partition_exactly_as_it_was():
    """The default argument keeps every existing caller's behaviour."""
    calls = [_call("search_catalog"), _call("add_to_cart")]
    assert _partition_gated_calls(calls, {}, {}) == ([], calls)


# ---------------------------------------------------------------------------
# the tool the model sees
# ---------------------------------------------------------------------------


def test_the_tool_takes_no_arguments():
    """'Which page?' has one answer at any moment; a parameter would invite
    the model to ask about a page the shopper is not on."""
    schema = build_get_current_page_product_schema()
    assert schema.name == GET_CURRENT_PAGE_PRODUCT_TOOL_NAME
    assert schema.properties == {}
    assert schema.required == []


def test_the_description_carries_the_rules_the_payload_cannot():
    """With an id, the card is already drawn by id — the model only writes.

    Search matches on the name, and this store answers every query with its
    nearest guess: on a title shared by ten products the page's own product
    was not even among the results. The widget draws the card from the id
    (the ``show_page_product`` intent); the model must not add its own.
    """
    description = build_get_current_page_product_schema().description

    # With an id: the card exists; no search, no second card.
    assert "ALREADY on screen" in description
    assert "Do NOT search the catalogue" in description
    assert "do NOT render a card" in description
    assert "'options'" in description
    # Without one: ask, and do not search-and-show instead.
    assert "Do NOT search by the page title" in description
    assert "DIFFERENT product" in description
    # A non-product page is an answer, not an error to report.
    assert "not as a failure" in description


# ---------------------------------------------------------------------------
# the answer coming back
# ---------------------------------------------------------------------------


def test_a_result_is_accepted_and_kept_verbatim():
    req = SubmitClientToolResultRequest(
        tool_call_id="tc_1",
        result={
            "url": "https://shop.example/products/black-shirt",
            "product_id": "gid://shopify/Product/123",
            "source": "shopify",
        },
    )
    assert req.result["product_id"] == "gid://shopify/Product/123"


def test_an_oversized_result_is_refused():
    """It lands in the context window for the rest of the session and comes
    from a browser we do not control — a page dumped verbatim costs a 422,
    not the session's budget."""
    import pytest

    with pytest.raises(ValueError):
        SubmitClientToolResultRequest(
            tool_call_id="tc_1",
            result={"blob": "x" * (MAX_CLIENT_TOOL_RESULT_BYTES + 1)},
        )


# ---------------------------------------------------------------------------
# the claim — a result standing in for execution
# ---------------------------------------------------------------------------


def _pending_row(expires_in_secs: int = 60):
    from datetime import datetime, timedelta, timezone

    from app.schemas.breeze_buddy.chat import ToolApproval, ToolApprovalStatus

    now = datetime.now(timezone.utc)
    return ToolApproval(
        id="row1",
        session_id="s1",
        tool_call_id="tc_1",
        function_name=GET_CURRENT_PAGE_PRODUCT_TOOL_NAME,
        arguments={},
        status=ToolApprovalStatus.PENDING,
        requested_at=now,
        expires_at=now + timedelta(seconds=expires_in_secs),
    )


def _stub_claim_deps(monkeypatch, row, persisted: list):
    """Patch the accessors claim_tool_approval reaches for."""
    from app.ai.voice.agents.breeze_buddy.chat import approvals as ap

    async def _get(session_id, tool_call_id):
        return row

    async def _decide(session_id, tool_call_id, new_status, reason):
        return row.model_copy(update={"status": new_status, "reason": reason})

    async def _insert(**kwargs):
        persisted.append(kwargs)

    async def _dangling(session_id, only_expired=False):
        return []

    async def _pending(session_id, include_expired=False):
        return []

    monkeypatch.setattr(ap, "get_tool_approval", _get)
    monkeypatch.setattr(ap, "decide_tool_approval", _decide)
    monkeypatch.setattr(ap, "insert_chat_message", _insert)
    monkeypatch.setattr(ap, "resolve_dangling_approvals", _dangling)
    monkeypatch.setattr(ap, "list_pending_tool_approvals", _pending)


async def test_a_browser_result_replaces_execution(monkeypatch):
    """The call already ran — in the browser. The resume turn must NOT execute
    it server-side, and the payload must reach the LLM as the tool result."""
    from app.ai.voice.agents.breeze_buddy.chat.approvals import claim_tool_approval

    persisted: list = []
    _stub_claim_deps(monkeypatch, _pending_row(), persisted)
    payload = {
        "url": "https://shop.example/products/black-shirt",
        "title": "Black Shirt",
    }

    claim = await claim_tool_approval("s1", "tc_1", True, None, result=payload)

    assert claim.outcome == "proceed"
    # The load-bearing bit: approved=True was passed, yet nothing re-executes.
    assert claim.effective_approved is False
    assert claim.synthetic_result == payload
    assert len(persisted) == 1


async def test_a_result_arriving_after_expiry_is_dropped_for_the_timeout(
    monkeypatch,
):
    """The shopper has moved on. The agent must hear that the read failed
    rather than answer about a page they have left."""
    from app.ai.voice.agents.breeze_buddy.chat.approvals import claim_tool_approval

    persisted: list = []
    _stub_claim_deps(monkeypatch, _pending_row(expires_in_secs=-1), persisted)

    claim = await claim_tool_approval(
        "s1", "tc_1", True, None, result={"url": "https://shop.example/products/x"}
    )

    assert claim.wire_status == "timeout"
    assert claim.synthetic_result is not None
    assert "url" not in claim.synthetic_result


async def test_an_approval_still_executes_on_approve(monkeypatch):
    """The path this shares must keep working: with no result, an approved
    call is still the caller's to execute."""
    from app.ai.voice.agents.breeze_buddy.chat.approvals import claim_tool_approval

    row = _pending_row()
    row = row.model_copy(update={"function_name": "issue_refund"})
    _stub_claim_deps(monkeypatch, row, [])

    claim = await claim_tool_approval("s1", "tc_1", True, None)

    assert claim.effective_approved is True
    assert claim.synthetic_result is None


# ---------------------------------------------------------------------------
# the page's product is on screen — no search, no second card
# ---------------------------------------------------------------------------


def _refusal_for(shown: bool):
    from app.ai.voice.agents.breeze_buddy.chat.agent.tooling import (
        ToolDispatchMixin,
    )

    agent = SimpleNamespace(
        session_id="s1",
        _page_product_shown=shown,
        _render_ui_force_after={"search_catalog"},
    )

    def refuse(name: str, args: Optional[Dict[str, Any]] = None):
        call = SimpleNamespace(function_name=name, arguments=args or {})
        return ToolDispatchMixin._page_product_refusal(
            cast(Any, agent), cast(Any, call)
        )

    return refuse


def test_a_shown_product_refuses_the_search_and_a_second_card():
    """Observed live: the card was right, then the model searched by title and
    drew a same-named DIFFERENT product. Once the page names the product, the
    search and any product card are refused — softly, so the model still
    writes its reply and the step rail shows no failure."""
    refuse = _refusal_for(shown=True)

    search = refuse("search_catalog")
    assert search is not None and search["soft"] is True
    assert refuse("render_ui", {"component": "ProductGrid"}) is not None
    assert refuse("render_ui", {"component": "ProductCard"}) is not None
    # Size chips and id reads still run.
    assert refuse("render_ui", {"quick_replies": ["S", "M"]}) is None
    assert refuse("get_product") is None


def test_nothing_is_refused_when_no_product_was_shown():
    refuse = _refusal_for(shown=False)

    assert refuse("search_catalog") is None
    assert refuse("render_ui", {"component": "ProductGrid"}) is None
