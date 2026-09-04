"""Engine guarantees of the DIRECT (no-LLM) intent path.

Contracts the flavor drivers cannot enforce for themselves, so the engine
has to:

1. The reducer state delta is persisted on EVERY outcome, not just
   success. Each dispatch's tool rows are already committed by the time
   the outcome is known, so an early return without the merge leaves
   ``agent_session_state`` behind what history says happened.
2. An approval-gated tool is refused. Chat disables the handler-level
   gate (``handles_approval_externally``), so the pre-dispatch check is
   the only one there is — and the no-LLM path has no turn to suspend an
   approval card on.
3. ``function_call_started`` carries the caller's args, never the
   post-injection ones (those pull server-internal identifiers out of
   agent_session_state).
4. A flavor whose module is absent or broken fails OPEN: its intents stay
   unregistered (→ the typed ``unknown_intent`` 422), never an ImportError
   escaping onto a public widget route.
5. DIRECT intents are legal during a live voice attachment ONLY because
   both surfaces serialise on the same per-session Redis lock.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest
from pydantic import BaseModel

import app.ai.voice.agents.breeze_buddy.chat.intents.router as router
from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import is_approval_gated
from app.ai.voice.agents.breeze_buddy.chat.intents.router import (
    IntentApprovalRequired,
    IntentPolicy,
    IntentRoute,
    ParsedIntent,
    UiIntent,
    ensure_flavor_intents,
    parse_ui_intent,
    run_persisted_tool,
)


class _Payload(BaseModel):
    pass


class _Call:
    """Stand-in for FunctionCallFromLLM."""

    def __init__(self, tool_call_id: str, arguments: Dict[str, Any]):
        self.tool_call_id = tool_call_id
        self.arguments = arguments
        self.function_name = "probe_tool"


class _StubAgent:
    """Dispatch chassis stub — only what run_persisted_tool touches."""

    def __init__(self, approval_map: Dict[str, Any] | None = None):
        self.session_id = "sess-1"
        self._approval_map = approval_map or {}
        self.dispatched: List[Tuple[str, Dict[str, Any]]] = []

    async def run_direct_tool(self, *, tool_name, args, node, prep, turn_id):
        self.dispatched.append((tool_name, dict(args)))
        # The real run_direct_tool builds the call with POST-injection args.
        injected = {**args, "cart_id": "SERVER-INTERNAL-ID"}
        return _Call("call-1", injected), {"ok": True}


@pytest.mark.asyncio
async def test_started_event_carries_caller_args_not_injected(monkeypatch):
    async def _noop_persist(*_a, **_k):
        return None

    monkeypatch.setattr(router, "_persist_tool_step", _noop_persist)
    agent = _StubAgent()
    events, _ = await run_persisted_tool(
        agent,  # type: ignore[arg-type]
        tool_name="probe_tool",
        args={"sku": "ABC"},
        node={},
        prep=None,
        turn_id="t1",
    )
    started = next(e for e in events if e.event == "function_call_started")
    assert started.data["args"] == {"sku": "ABC"}
    # The injected identifier reached the tool but NOT the wire.
    assert agent.dispatched == [("probe_tool", {"sku": "ABC"})]
    assert "cart_id" not in started.data["args"]


@pytest.mark.asyncio
async def test_gated_tool_is_refused_before_dispatch(monkeypatch):
    async def _noop_persist(*_a, **_k):
        return None

    monkeypatch.setattr(router, "_persist_tool_step", _noop_persist)
    agent = _StubAgent(approval_map={"probe_tool": object()})
    with pytest.raises(IntentApprovalRequired) as exc:
        await run_persisted_tool(
            agent,  # type: ignore[arg-type]
            tool_name="probe_tool",
            args={},
            node={},
            prep=None,
            turn_id="t1",
        )
    assert exc.value.tool_name == "probe_tool"
    # Fail-closed: the tool never ran.
    assert agent.dispatched == []


def test_gate_honours_per_node_shadowing():
    """Same rule the LLM path uses — a gated global shadowed by a per-node
    function of the same name is ungated in that node."""
    approval_map = {"probe_tool": object()}
    assert is_approval_gated("probe_tool", approval_map, {}) is True
    assert is_approval_gated("other_tool", approval_map, {}) is False

    class _Schema:
        name = "probe_tool"

    # A plain object is not a FlowsFunctionSchema, so it must NOT shadow.
    assert is_approval_gated("probe_tool", approval_map, {"functions": [_Schema()]})


@pytest.mark.asyncio
async def test_state_delta_persists_on_every_outcome(monkeypatch):
    """A driver whose first tool advances state and whose last tool FAILS
    must still persist the delta — history already has both tool rows."""
    merged: List[Dict[str, Any]] = []

    async def _capture_merge(*, chat_session_id, patch):
        merged.append(patch)

    async def _drive(agent, prep, node, parsed_intent, turn_id):
        # First dispatch succeeds and advances reducer state...
        agent.agent_state["cart_id"] = "DISCOVERED-BY-FIRST-TOOL"
        yield (None, "first_tool", {"ok": True})
        # ...second dispatch fails, which used to discard the delta.
        yield (None, "second_tool", {"status": "error"})

    parsed = ParsedIntent(
        intent=UiIntent(intent="probe", component_id="c1"),
        policy=IntentPolicy(
            route=IntentRoute.DIRECT,
            payload_model=_Payload,
            flavor="testflavor",
            drive=_drive,
        ),
        payload=_Payload(),
    )

    _install_direct_intent_stubs(monkeypatch, _capture_merge)

    events = [
        ev async for ev in router.run_direct_intent(session_id="sess-1", parsed=parsed)
    ]

    assert merged == [{"cart_id": "DISCOVERED-BY-FIRST-TOOL"}]
    names = [e.event for e in events]
    assert "intent_failed" in names and names[-1] == "turn_end"


def test_broken_flavor_module_fails_open_to_unknown_intent(monkeypatch):
    """A flavor whose module is missing (its layer hasn't shipped) or raises
    on import must degrade to the typed 422, not surface an ImportError as a
    500 from a public widget route."""
    monkeypatch.setitem(
        router.FLAVOR_INTENT_MODULES, "brokenflavor", "no.such.module.anywhere"
    )
    monkeypatch.setattr(router, "_LOADED_FLAVOR_INTENTS", set())

    ensure_flavor_intents(["brokenflavor"])  # must not raise

    with pytest.raises(router.IntentValidationError) as exc:
        parse_ui_intent(
            {"intent": "whatever", "component_id": "c1"},
            enabled_flavors={"brokenflavor"},
        )
    assert exc.value.detail["code"] == "unknown_intent"


def test_failed_flavor_import_is_not_retried_per_request(monkeypatch):
    """Python drops a failed module from sys.modules, so an unguarded retry
    would re-raise on every request for the life of the process."""
    attempts = {"n": 0}

    def _explode(_path):
        attempts["n"] += 1
        raise RuntimeError("flavor module is broken")

    monkeypatch.setitem(
        router.FLAVOR_INTENT_MODULES, "brokenflavor", "no.such.module.anywhere"
    )
    monkeypatch.setattr(router, "_LOADED_FLAVOR_INTENTS", set())
    monkeypatch.setattr(router.importlib, "import_module", _explode)

    for _ in range(3):
        ensure_flavor_intents(["brokenflavor"])
    assert attempts["n"] == 1


def test_direct_intent_and_voice_turn_share_one_session_lock():
    """DIRECT intents are deliberately allowed while a voice attachment is
    live (widget/handlers.py skips the channel gate). That is only safe
    because the voice bridge and the intent handler contend for the SAME
    Redis key — they serialise rather than interleaving their writes to
    chat_message / agent_session_state. Pin the two key builders together so
    an edit to one can't silently un-serialise them.
    """
    from app.ai.voice.agents.breeze_buddy.chat import voice_bridge
    from app.api.routers.breeze_buddy.chat import handlers as ch

    assert ch._lock_key("sess-abc") == voice_bridge._lock_key("sess-abc")
    assert ch._lock_key("sess-abc") == "chat:session:sess-abc:lock"


def _install_direct_intent_stubs(monkeypatch, capture_merge):
    """Neutralize every DB/network call run_direct_intent makes."""

    class _Session:
        status = "ACTIVE"
        template_id = "tpl-1"
        current_node = "start"
        metadata: Dict[str, Any] = {}

    class _Agent:
        def __init__(self, **kwargs):
            self.session_id = "sess-1"
            self.agent_state = dict(kwargs.get("agent_state") or {})
            self.aiohttp_session = None
            self.mcp_pool = None
            self._approval_map = {}

        async def prepare_direct_dispatch(self, _node):
            return None, {}

    async def _get_session(_sid):
        return _Session()

    async def _get_template(_tid):
        return object()

    async def _get_state(_sid):
        return None

    async def _build_vars(_tpl, _persisted):
        return {}

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(router, "get_chat_session_by_id", _get_session)
    monkeypatch.setattr(router, "get_template_by_id_cached", _get_template)
    monkeypatch.setattr(router, "get_agent_session_state", _get_state)
    monkeypatch.setattr(router, "build_render_template_vars", _build_vars)
    monkeypatch.setattr(router, "resolve_session_catalog_version", lambda _m: "v2")
    monkeypatch.setattr(router, "insert_chat_message", _noop)
    monkeypatch.setattr(router, "update_chat_session_after_turn", _noop)
    monkeypatch.setattr(router, "close_mcp_pool", _noop)
    monkeypatch.setattr(router, "create_aiohttp_session", lambda: None)
    monkeypatch.setattr(router, "upsert_agent_session_state_merge", capture_merge)
    monkeypatch.setattr(router, "ChatAgent", _Agent)


# ---------------------------------------------------------------------------
# Template-intent enrich rules — cross-tool selected-marking
# ---------------------------------------------------------------------------


def test_template_intent_enrich_marks_selected_tier():
    from app.ai.voice.agents.breeze_buddy.chat.intents.template_intents import (
        _apply_enrich,
    )
    from app.ai.voice.agents.breeze_buddy.chat.ui.binding import BindingStore
    from app.ai.voice.agents.breeze_buddy.template.types import (
        CustomUiIntent,
        CustomUiIntentStep,
        UiIntentEnrichRule,
    )

    store = BindingStore()
    store.record(
        "get_journey_details",
        None,
        {"status": "success", "selected_quote_id": "q2"},
    )
    store.record(
        "get_tier_options",
        None,
        {
            "status": "success",
            "tiers": [
                {"quote_id": "q1", "name": "First Class"},
                {"quote_id": "q2", "name": "Second Class"},
            ],
        },
    )

    class _Agent:
        binding_store = store

    cfg = CustomUiIntent(
        name="journey_detail",
        steps=[CustomUiIntentStep(tool="get_journey_details")],
        enrich=[
            UiIntentEnrichRule(
                list_ref="$tool:get_tier_options#/tiers",
                match_field="quote_id",
                equals_ref="$tool:get_journey_details#/selected_quote_id",
                set={"selected": True, "state_label": "Selected"},
                else_set={"unselected": True},
            )
        ],
    )
    _apply_enrich(_Agent(), cfg)
    tiers = store.resolve("get_tier_options")["tiers"]
    assert tiers[1]["selected"] is True and tiers[1]["state_label"] == "Selected"
    assert "selected" not in tiers[0] and tiers[0]["unselected"] is True


def test_template_intent_enrich_fail_open():
    from app.ai.voice.agents.breeze_buddy.chat.intents.template_intents import (
        _apply_enrich,
    )
    from app.ai.voice.agents.breeze_buddy.chat.ui.binding import BindingStore
    from app.ai.voice.agents.breeze_buddy.template.types import (
        CustomUiIntent,
        CustomUiIntentStep,
        UiIntentEnrichRule,
    )

    store = BindingStore()
    store.record("t", None, {"status": "success", "xs": [{"id": "a"}]})

    class _Agent:
        binding_store = store

    # bad ref + missing tool + non-list target: all silently skipped
    cfg = CustomUiIntent(
        name="n",
        steps=[CustomUiIntentStep(tool="t")],
        enrich=[
            UiIntentEnrichRule(
                list_ref="no-prefix#/xs",
                match_field="id",
                equals_ref="$tool:t#/missing",
                set={"s": 1},
            ),
            UiIntentEnrichRule(
                list_ref="$tool:absent#/xs",
                match_field="id",
                equals_ref="$tool:t#/xs",
                set={"s": 1},
            ),
        ],
    )
    _apply_enrich(_Agent(), cfg)
    assert store.resolve("t")["xs"] == [{"id": "a"}]
