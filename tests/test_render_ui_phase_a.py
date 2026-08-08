"""RFC-002 Phase A unit tests: render_ui tool, plan enforcer, baseline
search annotation, and the metrics counters."""

from types import SimpleNamespace

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.annotator import (
    annotate_search_result,
)
from app.ai.voice.agents.breeze_buddy.chat.agent import (
    approval as agent_approval,
    context as agent_context,
    core as agent_core,
    cycle as agent_cycle,
    render_ui as agent_render_ui,
    tooling as agent_tooling,
)
from app.ai.voice.agents.breeze_buddy.chat.llm import driver as llm_driver
from app.ai.voice.agents.breeze_buddy.chat.metrics import TurnMetrics
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.chat.steps.enforcer import PlanEnforcer
from app.ai.voice.agents.breeze_buddy.chat.ui.binding import BindingStore
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    execute_render_ui,
    render_ui_components,
)
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    ensure_group_loaded,
    resolve_allowlist,
)

# agent.py is a package of subsystem modules now — a patched seam must
# land on every submodule that calls it (autoflake prunes unused
# imports per module, hence the hasattr guard).
_AGENT_MODULES = (
    agent_core,
    agent_cycle,
    agent_approval,
    agent_render_ui,
    agent_context,
    agent_tooling,
)


def _patch_agent_attr(monkeypatch, name, value):
    for _mod in _AGENT_MODULES:
        if hasattr(_mod, name):
            monkeypatch.setattr(_mod, name, value)


ensure_group_loaded("commerce")
ALLOW = resolve_allowlist(enabled_groups=["core", "commerce"])
COMPS = render_ui_components(ALLOW, True)


def _store(products=None):
    store = BindingStore()
    store.record(
        "search_catalog",
        "t1",
        {
            "products": products
            or [
                {
                    "id": "p1",
                    "title": "CoreFlex Shorts",
                    "price": {"amount": 999.0},
                    "variants": [
                        {
                            "id": "v-black",
                            "title": "S / Black",
                            "price": {"amount": 999.0},
                        },
                        {
                            "id": "v-pink",
                            "title": "S / Pink",
                            "price": {"amount": 1099.0},
                        },
                    ],
                },
                {"id": "p2", "title": "AeroShield Jacket", "price": {"amount": 4999.0}},
            ]
        },
    )
    return store


# ---------------------------------------------------------------- render_ui


def test_components_exclude_server_only_and_include_quick_replies():
    assert "ProductGrid" in COMPS
    assert "QuickReplies" in COMPS
    assert "LinkButton" in COMPS
    assert "ProductDetail" not in COMPS  # server_only stays server-only


def test_link_button_renders_with_config_trusted_url():
    out = execute_render_ui(
        {
            "component": "LinkButton",
            "link": {
                "label": "Review and checkout",
                "url": "https://shop.example/cart",
            },
        },
        store=BindingStore(),  # no tool calls this turn — allowlist carries it
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        trusted_urls={"https://shop.example/cart"},
    )
    assert out.decision == "rendered"
    op = out.ops[0]
    assert op["type"] == "LinkButton" and op["v"] == 2
    assert op["props"] == {
        "label": "Review and checkout",
        "url": "https://shop.example/cart",
    }
    assert out.fn_result["url"] == "https://shop.example/cart"


def test_link_button_accepts_url_from_this_turns_tool_results():
    products = [
        {
            "id": "p1",
            "title": "CoreFlex Shorts",
            "url": "https://shop.example/products/coreflex-shorts",
            "price": {"amount": 999.0},
        }
    ]
    out = execute_render_ui(
        {
            "component": "LinkButton",
            "link": {
                "label": "View product",
                "url": "https://shop.example/products/coreflex-shorts",
            },
        },
        store=_store(products),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        trusted_urls=set(),
    )
    assert out.decision == "rendered"
    assert out.ops[0]["props"]["url"] == "https://shop.example/products/coreflex-shorts"


def test_link_button_accepts_url_from_reducer_state():
    """Reducer state (e.g. policy_links captured from an earlier cart
    call) has tool provenance — a state-sourced URL renders even when no
    tool ran THIS turn."""
    out = execute_render_ui(
        {
            "component": "LinkButton",
            "link": {
                "label": "Refund policy",
                "url": "https://shop.example/policies/refunds",
            },
        },
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        trusted_urls=set(),
        state_values={
            "policy_links": [
                {
                    "type": "refund_policy",
                    "url": "https://shop.example/policies/refunds",
                }
            ]
        },
    )
    assert out.decision == "rendered"
    assert out.ops[0]["props"]["url"] == "https://shop.example/policies/refunds"


def _link_args(url: str) -> dict:
    return {"component": "LinkButton", "link": {"label": "Tap", "url": url}}


def test_link_button_ignores_storefront_pushed_context_state():
    """The ``_client_context`` namespace is written by storefront JS over
    /widget/session/{id}/context — key-allowlisted, values unvalidated. A
    URL that is ONLY there has no tool provenance, so it must not satisfy
    the trust rule; otherwise a compromised page could have the assistant
    render an attacker link as a trusted CTA."""
    evil = "https://evil.example/pay"
    out = execute_render_ui(
        _link_args(evil),
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        trusted_urls=set(),
        state_values={"_client_context": {"page_url": evil}},
    )
    assert out.decision == "error"
    assert "untrusted url" in out.fn_result["error"]


def test_link_button_ignores_client_allowlisted_state_keys():
    """Same for the top-level keys a template lets the storefront push
    (``client_context.state_allowlist``) — the push validates key NAMES,
    never values."""
    evil = "https://evil.example/pay"
    template = SimpleNamespace(
        configurations=SimpleNamespace(
            client_context=SimpleNamespace(state_allowlist=["cart_id"])
        )
    )
    out = execute_render_ui(
        _link_args(evil),
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        trusted_urls=set(),
        state_values={"cart_id": evil},
        template=template,
    )
    assert out.decision == "error"


def test_link_button_still_accepts_reducer_state_on_a_context_template():
    """The strip is by key: a template that allowlists SOME keys keeps
    trusting the reducer-written ones it did not allowlist."""
    url = "https://shop.example/policies/refunds"
    template = SimpleNamespace(
        configurations=SimpleNamespace(
            client_context=SimpleNamespace(state_allowlist=["cart_id"])
        )
    )
    out = execute_render_ui(
        _link_args(url),
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        trusted_urls=set(),
        state_values={"cart_id": "c1", "policy_links": [{"url": url}]},
        template=template,
    )
    assert out.decision == "rendered"
    assert out.ops[0]["props"]["url"] == url


def test_link_button_trusted_urls_win_over_the_state_strip():
    """The static allowlist is checked first and is unaffected — the
    commerce pilot's links come from there."""
    url = "https://shop.example/cart"
    out = execute_render_ui(
        _link_args(url),
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        trusted_urls={url},
        state_values={"_client_context": {"page_url": url}},
    )
    assert out.decision == "rendered"


def test_link_button_rejects_untrusted_url():
    out = execute_render_ui(
        {
            "component": "LinkButton",
            "link": {"label": "Click me", "url": "https://evil.example/phish"},
        },
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        trusted_urls={"https://shop.example/cart"},
    )
    assert out.decision == "error"
    assert out.ops == []
    assert "untrusted url" in out.fn_result["error"]


def test_link_button_requires_link_payload():
    out = execute_render_ui(
        {"component": "LinkButton"},
        store=BindingStore(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        trusted_urls={"https://shop.example/cart"},
    )
    assert out.decision == "error"
    assert "LinkButton needs link=" in out.fn_result["error"]


def test_render_grid_with_selection_and_hero():
    out = execute_render_ui(
        {
            "component": "ProductGrid",
            "bind": [{"prop": "products", "ref": "$tool:search_catalog#/products"}],
            "items": [{"id": "p1", "feature_variant": "v-pink"}],
        },
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        flavor_groups=["commerce"],
    )
    assert out.decision == "rendered"
    op = out.ops[0]
    assert op["id"] == "root" and op["type"] == "ProductGrid" and op["v"] == 2
    entry = op["props"]["products"][0]
    assert entry["featured_variant_id"] == "v-pink"
    assert entry["price"]["amount"] == 1099.0
    assert "items" not in op["props"]
    # Function response = compact UI memory (the marker replacement).
    assert out.fn_result["rendered"] == "ProductGrid"
    assert out.fn_result["items"][0]["featured_variant"] == "v-pink"


def test_no_ui_is_a_legal_payload():
    out = execute_render_ui(
        {"decision": "no_ui", "reason": "cart already visible"},
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
    )
    assert out.decision == "no_ui"
    assert out.ops == []
    assert out.fn_result == {
        "status": "ok",
        "decision": "no_ui",
        "reason": "cart already visible",
    }


def test_errors_are_structured_never_raised():
    store = _store()
    # missing component AND no decision
    out = execute_render_ui(
        {}, store=store, allowlist=ALLOW, components=COMPS, op_id="root"
    )
    assert out.fn_result["status"] == "error"
    # unknown component
    out = execute_render_ui(
        {"component": "Hologram"},
        store=store,
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
    )
    assert "Hologram" in out.fn_result["error"]
    # data-bound without bind
    out = execute_render_ui(
        {"component": "ProductGrid"},
        store=store,
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
    )
    assert "data-bound" in out.fn_result["error"]
    # malformed ref
    out = execute_render_ui(
        {"component": "ProductGrid", "bind": [{"prop": "products", "ref": "products"}]},
        store=store,
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
    )
    assert "bad bind ref" in out.fn_result["error"]
    # unresolved bind (tool didn't run this turn)
    out = execute_render_ui(
        {
            "component": "ProductGrid",
            "bind": [{"prop": "products", "ref": "$tool:get_cart#/products"}],
        },
        store=store,
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
    )
    assert "bind_unresolved" in out.fn_result["error"]


def test_quick_replies_literal_path_with_parent():
    """Canonical form: one STRING per chip — shown AND sent back."""
    out = execute_render_ui(
        {
            "component": "QuickReplies",
            "quick_replies": ["Show more", "Checkout"],
        },
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="rui1",
        parent="root",
    )
    assert out.decision == "rendered"
    op = out.ops[0]
    assert op["type"] == "QuickReplies" and op["parent"] == "root"
    assert op["props"]["items"] == [{"label": "Show more"}, {"label": "Checkout"}]
    assert out.fn_result["count"] == 2


def test_quick_replies_object_form_tolerated():
    """The retired {label, value} object form still renders (stragglers /
    replayed calls) — just no longer advertised in the schema."""
    out = execute_render_ui(
        {
            "component": "QuickReplies",
            "quick_replies": [
                {"label": "Show more"},
                {"label": "Checkout", "value": "go to checkout"},
            ],
        },
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="rui1",
        parent="root",
    )
    assert out.decision == "rendered"
    items = out.ops[0]["props"]["items"]
    assert [i["label"] for i in items] == ["Show more", "Checkout"]
    assert items[1]["value"] == "go to checkout"


def test_object_form_bind_tolerated():
    out = execute_render_ui(
        {
            "component": "ProductGrid",
            "bind": {"products": "$tool:search_catalog#/products"},
        },
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
    )
    assert out.decision == "rendered"
    assert out.fn_result["count"] == 2


# ---------------------------------------------------------------- enforcer


def test_enforcer_stays_advisory_for_single_step_or_unknown_tools():
    enf = PlanEnforcer()
    assert not enf.start(["search_catalog"], {"search_catalog"})
    assert not enf.start(["search_catalog", "hallucinated"], {"search_catalog"})
    assert not enf.active


def test_enforcer_constrains_advances_and_finishes():
    enf = PlanEnforcer()
    tools = {"search_catalog", "update_cart", "revise_plan"}
    assert enf.start(["search_catalog", "update_cart"], tools)
    assert enf.constraining
    assert enf.allowed_names("revise_plan") == ["search_catalog", "revise_plan"]
    enf.on_tool_result("update_cart", True)  # off-step result: ignored
    assert enf.cursor == 0
    enf.on_tool_result("search_catalog", True)
    assert enf.allowed_names("revise_plan") == ["update_cart", "revise_plan"]
    enf.on_tool_result("update_cart", True)
    assert not enf.constraining  # plan exhausted → cycles are free again


def test_enforcer_retry_then_require_revise():
    enf = PlanEnforcer()
    tools = {"search_catalog", "update_cart"}
    enf.start(["search_catalog", "update_cart"], tools)
    enf.on_tool_result("search_catalog", False)  # fail #1 → retry same step
    assert enf.allowed_names("revise_plan") == ["search_catalog", "revise_plan"]
    assert not enf.revise_required
    enf.on_tool_result("search_catalog", False)  # fail #2 → revise required
    assert enf.revise_required
    assert enf.allowed_names("revise_plan") == ["revise_plan"]
    effective = enf.revise(["update_cart"], tools)
    assert effective == ["update_cart"]
    assert not enf.revise_required
    assert enf.allowed_names("revise_plan") == ["update_cart", "revise_plan"]


def test_enforcer_revise_drops_unknown_and_can_empty():
    enf = PlanEnforcer()
    tools = {"a", "b"}
    enf.start(["a", "b"], tools)
    enf.on_tool_result("a", True)
    effective = enf.revise(["nope"], tools)  # unknown dropped → plan ends
    assert effective == ["a"]
    assert not enf.constraining


# ---------------------------------------------------------------- annotator


def _search_result():
    return {
        "products": [
            {
                "id": "p1",
                "title": "CoreFlex Shorts",
                "tags": ["gym"],
                "variants": [
                    {
                        "id": "v-b",
                        "title": "S / Black",
                        "options": [{"value": "Black"}],
                    },
                    {"id": "v-p", "title": "S / Pink", "options": [{"value": "Pink"}]},
                ],
            },
            {"id": "p2", "title": "AeroShield Jacket", "tags": [], "variants": []},
        ]
    }


def test_annotator_marks_product_and_variant():
    out = annotate_search_result({"query": "pink shorts"}, _search_result())
    p1, p2 = out["products"]
    assert p1["matched_via"] == "exact"
    assert p1["matched_variant"]["id"] == "v-p"
    assert "matched_via" not in p2
    assert out["match"]["matched_ids"] == ["p1"]


def test_annotator_ignores_shared_variant_tokens():
    """'shorts' rides every variant title — it must not pick a variant."""
    out = annotate_search_result({"query": "shorts"}, _search_result())
    assert "matched_variant" not in out["products"][0]
    assert out["products"][0]["matched_via"] == "exact"


def test_annotator_noops_without_signal():
    result = _search_result()
    assert annotate_search_result({"query": "   "}, result) is result
    assert annotate_search_result({}, result) is result
    assert (
        annotate_search_result({"query": "show me"}, result) is result
    )  # stopwords only
    weird = {"products": "not-a-list"}
    assert annotate_search_result({"query": "pink"}, weird) is weird


# ---------------------------------------------------------------- metrics


def test_metrics_counts_render_ui_signals():
    m = TurnMetrics(session_id="s", template_id="t", t0=0.0)
    m.observe(SSEEvent(event="function_call_started", data={"name": "render_ui"}))
    m.observe(SSEEvent(event="function_call_started", data={"name": "search_catalog"}))
    m.observe(SSEEvent(event="ui_decision", data={"decision": "no_ui", "reason": "x"}))
    m.observe(SSEEvent(event="ui_decision", data={"decision": "rendered"}))
    m.observe(SSEEvent(event="force_fallback", data={"allowed": ["render_ui"]}))
    assert m.render_ui_calls == 1
    assert m.tool_calls == 2
    assert m.ui_no_ui == 1
    assert m.force_fallbacks == 1


# ----------------------------------------------------------- prompt splice


def test_render_ui_section_carries_the_plan_instruction():
    """The render_ui section REPLACES the data-bound subsection that
    normally carries the <plan> instruction — if it doesn't bring the
    instruction along, the model never emits plans in render_ui mode and
    enforcement stays silently dormant (caught live in A7)."""
    from app.ai.voice.agents.breeze_buddy.template.ui_prompt import (
        render_render_ui_section,
    )

    section = render_render_ui_section()
    assert "<plan>" in section
    assert "render_ui" in section  # still the render_ui contract


def test_link_button_rejected_on_the_text_channel():
    """text_channel=False wire gate: the URL trust check lives ONLY in the
    render_ui path, so a hand-typed <ui_stream> add of LinkButton must be
    rejected at parse — otherwise the dual-read window is an
    arbitrary-URL injection surface."""
    from app.ai.voice.agents.breeze_buddy.chat.ui.stream import parse_op_line

    result = parse_op_line(
        '{"op":"add","id":"root","type":"LinkButton",'
        '"props":{"label":"Checkout","url":"https://evil.example/phish"}}',
        allowlist=ALLOW,
    )
    assert result.op is None
    assert result.error == "render_ui_only:LinkButton"


def test_render_ui_sessions_never_write_the_legacy_marker():
    """Phase B: the ``[ui rendered: …]`` marker in replayed history is what
    bred the F1 mimicry bug — render_ui sessions get their UI memory from
    the function response instead, so the marker write-path is off for
    them. Fleet text-channel sessions keep it (their only UI memory)."""
    from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent
    from app.ai.voice.agents.breeze_buddy.template.types import (
        ConfigurationModel,
        RenderUiConfig,
        TemplateModel,
    )

    ops = [
        {"op": "add", "id": "root", "type": "Tile", "props": {"title": "Mug"}, "v": 2}
    ]

    def agent_with(render_ui: bool) -> ChatAgent:
        cfg = ConfigurationModel.model_construct(
            render_ui=RenderUiConfig(enabled=render_ui)
        )
        template = TemplateModel.model_construct(
            id="tpl-b", name="t", flow={}, configurations=cfg
        )
        return ChatAgent(
            session_id="s-b",
            template=template,
            llm=object(),
            template_vars={},
            catalog_version="v2",
        )

    assert agent_with(True)._ui_summary(ops) == ""
    legacy = agent_with(False)._ui_summary(ops)
    assert legacy.startswith("[ui rendered:")


async def test_text_channel_ops_dropped_on_render_ui_sessions(monkeypatch):
    """Phase D hard cutover: a render_ui session's <ui_stream> text op is
    dropped observably (ui_op_dropped reason=text_channel_retired), never
    rendered — the function call is the ONLY UI channel. Fleet text-channel
    sessions (render_ui_tool off) keep the dual path untouched."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent, _PreparedTools
    from app.ai.voice.agents.breeze_buddy.template.types import (
        ConfigurationModel,
        RenderUiConfig,
        TemplateModel,
    )

    op_line = (
        '<ui_stream>{"op":"add","id":"root","type":"QuickReplies",'
        '"props":{"items":[{"label":"A"},{"label":"B"}]}}</ui_stream>'
    )

    async def _stream(_llm, _context, **_kwargs):
        yield ("text", op_line)

    monkeypatch.setattr(llm_driver, "stream", _stream)

    async def _noop(**_kwargs):
        return None

    _patch_agent_attr(monkeypatch, "insert_chat_message", _noop)
    _patch_agent_attr(monkeypatch, "update_chat_session_after_turn", _noop)
    _patch_agent_attr(monkeypatch, "upsert_agent_session_state_merge", _noop)

    cfg = ConfigurationModel.model_construct(render_ui=RenderUiConfig(enabled=True))
    template = TemplateModel.model_construct(
        id="tpl-cut", name="t", flow={}, configurations=cfg
    )
    agent = ChatAgent(
        session_id="s-cut",
        template=template,
        llm=object(),
        template_vars={},
        catalog_version="v2",
    )
    agent._turn_id = "turn-cut"
    prep = _PreparedTools(
        flow_config={}, global_funcs=[], tool_retention=None, tool_projection=None
    )
    context = LLMContext(messages=[{"role": "user", "content": "hi"}])
    node = {"name": "start", "functions": []}

    events = [ev async for ev in agent._cycle_loop(context, node, prep)]
    kinds = [ev.event for ev in events]
    assert "ui_op" not in kinds
    dropped = [ev for ev in events if ev.event == "ui_op_dropped"]
    assert len(dropped) == 1
    assert dropped[0].data["reason"] == "text_channel_retired"


# ---------------------------------------------------------------------------
# Forced final chips cycle (template render_ui.quick_replies='forced_final')
# ---------------------------------------------------------------------------


def _chips_agent(monkeypatch, mode, responses):
    """A render_ui ChatAgent wired to a scripted llm_driver.stream.

    ``responses[i]`` is the list of driver events the i-th LLM call yields.
    Returns (agent, prep, calls, inserted): ``calls`` records each call's
    forcing/thinking kwargs + the context tail it saw; ``inserted`` records
    every insert_chat_message row in write order.
    """
    from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent, _PreparedTools
    from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
        build_render_ui_schema,
    )
    from app.ai.voice.agents.breeze_buddy.template.types import (
        ConfigurationModel,
        RenderUiConfig,
        TemplateModel,
    )

    calls: list = []

    async def _stream(_llm, context, **kwargs):
        tail = [
            {"role": m.get("role"), "content": m.get("content")}
            for m in context.get_messages()[-2:]
            if isinstance(m, dict)
        ]
        idx = len(calls)
        calls.append(
            {
                "allowed": kwargs.get("allowed_function_names"),
                "thinking": kwargs.get("thinking_level_override"),
                "tail": tail,
            }
        )
        for event in responses[idx] if idx < len(responses) else []:
            yield event

    monkeypatch.setattr(llm_driver, "stream", _stream)

    inserted: list = []

    async def _insert(**kwargs):
        inserted.append(kwargs)
        return None

    _patch_agent_attr(monkeypatch, "insert_chat_message", _insert)

    async def _noop(**_kwargs):
        return None

    _patch_agent_attr(monkeypatch, "update_chat_session_after_turn", _noop)
    _patch_agent_attr(monkeypatch, "upsert_agent_session_state_merge", _noop)

    cfg = ConfigurationModel.model_construct(
        render_ui=RenderUiConfig(enabled=True, quick_replies=mode)
    )
    template = TemplateModel.model_construct(
        id="tpl-chips", name="t", flow={}, configurations=cfg
    )
    agent = ChatAgent(
        session_id="s-chips",
        template=template,
        llm=object(),
        template_vars={},
        catalog_version="v2",
    )
    agent._turn_id = "turn-chips"
    agent._ui_allowlist = set(ALLOW)
    prep = _PreparedTools(
        flow_config={},
        global_funcs=[build_render_ui_schema(list(COMPS), agent._render_ui_handler)],
        tool_retention=None,
        tool_projection=None,
    )
    return agent, prep, calls, inserted


async def _run_chips_turn(agent, prep):
    from pipecat.processors.aggregators.llm_context import LLMContext

    context = LLMContext(messages=[{"role": "user", "content": "hi"}])
    node = {"name": "start", "functions": []}
    return [ev async for ev in agent._cycle_loop(context, node, prep)]


def _rui_call(args, call_id="t-chips"):
    from pipecat.frames.frames import FunctionCallFromLLM

    return (
        "tool_call",
        FunctionCallFromLLM(
            function_name="render_ui",
            tool_call_id=call_id,
            arguments=args,
            context=None,
        ),
    )


def _chips_args():
    return {
        "component": "QuickReplies",
        "quick_replies": ["Best Sellers", "Leggings"],
    }


async def test_forced_final_chips_paint_below_the_prose(monkeypatch):
    """The happy path: prose cycle → forced chips cycle. The bubble anchors
    (assistant_message) BEFORE the chips ui_op — the exact inversion of the
    live-observed chips-above-prose bug — and the chips cycle runs forced
    (allowed=[render_ui]) on MINIMAL thinking. Cycle 1 is minimal too
    (cycle-graded thinking: the routing cycle of a fresh turn)."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [("text", "Here you go!")],
            [_rui_call(_chips_args())],
        ],
    )
    events = await _run_chips_turn(agent, prep)
    kinds = [ev.event for ev in events]

    assert len(calls) == 2
    assert calls[0]["allowed"] is None and calls[0]["thinking"] == "minimal"
    assert calls[1]["allowed"] == ["render_ui"]
    assert calls[1]["thinking"] == "minimal"
    # The chips call saw: its own prose, then the user-role nudge
    # (alternation-safe for Vertex; identical on next-turn replay).
    assert calls[1]["tail"][0] == {"role": "assistant", "content": "Here you go!"}
    assert calls[1]["tail"][1]["role"] == "user"
    assert "final check" in calls[1]["tail"][1]["content"]

    assert kinds.index("assistant_message") < kinds.index("ui_op")
    op = next(ev.data["op"] for ev in events if ev.event == "ui_op")
    assert op["type"] == "QuickReplies"
    end = next(ev.data for ev in events if ev.event == "turn_end")
    assert end["session_status"] == "ACTIVE"

    # Persist order mirrors the wire: prose row → internal nudge row →
    # chips tool rows → chips-only ui row (resume repaints below the prose).
    prose_row = inserted[0]
    assert prose_row["content"] == "Here you go!"
    nudge_row = inserted[1]
    assert nudge_row["content"] is None
    assert nudge_row["content_blocks"][0]["visibility"] == "internal"
    assert "final check" in nudge_row["content_blocks"][0]["text"]
    last_row = inserted[-1]
    assert last_row["ui_blocks"] and last_row["ui_blocks"][0]["type"] == "QuickReplies"


async def test_forced_final_no_ui_is_a_legal_chips_outcome(monkeypatch):
    """no_ui resolves the chips slot: no ui_op, no third cycle, healthy end
    — forcing mandates the DECISION, never the display."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [("text", "Anything else?")],
            [_rui_call({"decision": "no_ui", "reason": "nothing to suggest"})],
        ],
    )
    events = await _run_chips_turn(agent, prep)
    kinds = [ev.event for ev in events]
    assert len(calls) == 2
    assert "ui_op" not in kinds
    decisions = [ev.data for ev in events if ev.event == "ui_decision"]
    assert decisions and decisions[-1]["decision"] == "no_ui"
    assert (
        next(ev.data for ev in events if ev.event == "turn_end")["session_status"]
        == "ACTIVE"
    )


async def test_chips_cycle_rejects_other_components_once_then_corrects(monkeypatch):
    """The chips slot is QuickReplies-or-no_ui: a grid in the tail gets a
    structured error and ONE corrective forced cycle — bounded, and the
    turn can never end with a second grid below the reply."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [("text", "Done!")],
            [_rui_call({"component": "ProductGrid"}, call_id="bad")],
            [_rui_call(_chips_args(), call_id="good")],
        ],
    )
    events = await _run_chips_turn(agent, prep)
    kinds = [ev.event for ev in events]
    assert len(calls) == 3
    assert calls[1]["allowed"] == calls[2]["allowed"] == ["render_ui"]
    assert kinds.count("ui_op") == 1
    op = next(ev.data["op"] for ev in events if ev.event == "ui_op")
    assert op["type"] == "QuickReplies"
    # The rejected call's function response carries the slot contract.
    error_results = [
        row
        for row in inserted
        if row.get("content_blocks")
        and any(
            b.get("type") == "tool_result" and "end-of-turn" in str(b.get("content"))
            for b in row["content_blocks"]
        )
    ]
    assert error_results


async def test_chips_give_up_after_two_bad_cycles(monkeypatch):
    """Two invalid chips cycles → give up: chips skipped, turn still ends
    ACTIVE with the reply intact. Hard bound — no drift toward the old
    unbounded-loop failure class."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [("text", "Done!")],
            [_rui_call({"component": "ProductGrid"}, call_id="bad1")],
            [_rui_call({"component": "ProductGrid"}, call_id="bad2")],
        ],
    )
    events = await _run_chips_turn(agent, prep)
    kinds = [ev.event for ev in events]
    assert len(calls) == 3
    assert "ui_op" not in kinds
    assert (
        next(ev.data for ev in events if ev.event == "turn_end")["session_status"]
        == "ACTIVE"
    )


async def test_chips_malformed_skips_chips_never_retries_unforced(monkeypatch):
    """A chips cycle that produces NO call skips chips (observable via
    force_fallback context=final_quick_replies) — never the generic
    unforced retry, which would stream a SECOND bubble after the reply."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [("text", "All set.")],
            [("finish_reason", "MALFORMED_FUNCTION_CALL")],
        ],
    )
    events = await _run_chips_turn(agent, prep)
    kinds = [ev.event for ev in events]
    assert len(calls) == 2  # no third (unforced) call
    assert "ui_op" not in kinds
    fallback = next(ev.data for ev in events if ev.event == "force_fallback")
    assert fallback["context"] == "final_quick_replies"
    assert kinds.count("assistant_message") == 1
    assert (
        next(ev.data for ev in events if ev.event == "turn_end")["session_status"]
        == "ACTIVE"
    )


async def test_forced_final_harvests_mid_turn_quick_replies(monkeypatch):
    """Rider design (2026-08-03): a mid-turn chips call is HARVESTED —
    the call returns ok+deferred, the held chips flush BELOW the final
    prose, and the forced end-of-turn cycle never runs (one LLM cycle
    saved on every turn where the model attached chips itself)."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [_rui_call(_chips_args(), call_id="early")],
            [("text", "Here's the rundown.")],
            [_rui_call(_chips_args(), call_id="never-runs")],
        ],
    )
    events = await _run_chips_turn(agent, prep)
    assert len(calls) == 2  # NO forced chips cycle
    ops = [ev.data["op"] for ev in events if ev.event == "ui_op"]
    assert len(ops) == 1 and ops[0]["type"] == "QuickReplies"
    kinds = [ev.event for ev in events]
    # chips paint AFTER the final prose bubble (placement is server-owned)
    assert kinds.index("assistant_message") < kinds.index("ui_op")
    deferred = [
        row
        for row in inserted
        if row.get("content_blocks")
        and any(
            b.get("type") == "tool_result"
            and "follow-ups saved" in str(b.get("content"))
            for b in row["content_blocks"]
        )
    ]
    assert deferred  # positive deferral result, never a ban error


async def test_harvested_chips_after_prose_never_double_bubbles(monkeypatch):
    """Live 2026-07-31 regression, rider era: the model writes its
    greeting AND attaches chips in one cycle. The harvest returns a
    positive deferral (an error here bred a rephrased second greeting),
    any post-harvest ramble is suppressed, and the held chips flush after
    the single final bubble — no forced cycle."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            # Cycle 1: prose + a chips call in one breath (harvested).
            [("text", "Hey! Welcome."), _rui_call(_chips_args(), call_id="early")],
            # Cycle 2: the model rambles a rephrased greeting anyway —
            # suppression must drop it.
            [("text", "Hey there! Welcome again.")],
            # Would-be forced chips cycle — must never run.
            [_rui_call(_chips_args(), call_id="never-runs")],
        ],
    )
    events = await _run_chips_turn(agent, prep)
    assert len(calls) == 2  # ramble cycle ran; forced chips cycle did NOT
    tokens = "".join(ev.data["delta"] for ev in events if ev.event == "assistant_token")
    assert "Welcome again" not in tokens  # duplicate prose dropped
    bubbles = [ev for ev in events if ev.event == "assistant_message"]
    assert len(bubbles) == 1
    assert bubbles[0].data["content"] == "Hey! Welcome."
    ops = [ev.data["op"] for ev in events if ev.event == "ui_op"]
    assert len(ops) == 1 and ops[0]["type"] == "QuickReplies"


async def test_componentless_chips_call_harvests_and_single_visible_row(monkeypatch):
    """The bare quick_replies-with-no-component shape (Flash's natural
    emission, live 2026-07-31) is a first-class rider now: harvested,
    suppression armed, chips flushed from the held labels. Also pins the
    double-persist fix: the prose is VISIBLE on exactly one row (the
    pre-chips row); the gate row demotes it to internal."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [
                ("text", "Hey! Welcome."),
                _rui_call({"quick_replies": ["A", "B"]}, call_id="bare"),
            ],
            [("text", "Hey there! Welcome again.")],
            [_rui_call(_chips_args(), call_id="never-runs")],
        ],
    )
    events = await _run_chips_turn(agent, prep)
    assert len(calls) == 2  # no forced chips cycle
    tokens = "".join(ev.data["delta"] for ev in events if ev.event == "assistant_token")
    assert "Welcome again" not in tokens  # suppression armed by the harvest
    bubbles = [ev for ev in events if ev.event == "assistant_message"]
    assert len(bubbles) == 1
    ops = [ev.data["op"] for ev in events if ev.event == "ui_op"]
    assert len(ops) == 1 and ops[0]["type"] == "QuickReplies"
    labels = [i.get("label") for i in (ops[0].get("props") or {}).get("items", [])]
    assert labels == ["A", "B"]  # the model's own harvested labels
    visible = [row["content"] for row in inserted if row.get("content")]
    assert visible == ["Hey! Welcome."]  # ONE visible copy — resume shows it once


async def test_model_choice_default_runs_no_extra_cycle(monkeypatch):
    """Absent config = today's behavior exactly: one cycle, no forcing, no
    nudge rows, fleet untouched."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        None,
        responses=[[("text", "Hello!")]],
    )
    events = await _run_chips_turn(agent, prep)
    kinds = [ev.event for ev in events]
    assert len(calls) == 1
    assert calls[0]["allowed"] is None
    assert kinds.count("assistant_message") == 1
    assert "force_fallback" not in kinds


async def test_quick_replies_off_removes_the_component(monkeypatch):
    """mode='off': the handler rejects QuickReplies as unknown — the
    component is gone from this session's render_ui surface."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "off",
        responses=[[("text", "unused")]],
    )
    result = await agent._render_ui_handler(_chips_args())
    assert result["status"] == "error"
    assert "unknown component" in result["error"]


def test_quick_replies_mode_validates():
    import pytest
    from pydantic import ValidationError

    from app.ai.voice.agents.breeze_buddy.template.types import RenderUiConfig

    assert RenderUiConfig(quick_replies="forced_final").quick_replies == "forced_final"
    # Any-typed payload keeps the deliberately-invalid literal out of the
    # static type check (pyrefly) while pydantic still rejects it.
    bad: dict = {"quick_replies": "always"}
    with pytest.raises(ValidationError):
        RenderUiConfig(**bad)


def test_render_ui_section_documents_the_chips_contract():
    from app.ai.voice.agents.breeze_buddy.template.ui_prompt import (
        render_render_ui_section,
    )

    forced = render_render_ui_section("forced_final")
    assert "END of your turn" in forced
    assert "never render" in forced
    default = render_render_ui_section()
    assert "END of your turn" not in default


# ---------------------------------------------------------------------------
# Server-derived layout (2026-07-30): never a model choice
# ---------------------------------------------------------------------------


def test_layout_is_server_policy_not_model_choice():
    """``layout`` is gone from the render_ui schema; the server stamps it
    from the FINAL hydrated count — 1-2 products sit side by side, 3+
    scroll as a carousel. A model-passed layout arg is ignored."""
    from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
        build_render_ui_schema,
    )

    schema = build_render_ui_schema(list(COMPS), handler=None)
    assert "layout" not in schema.properties

    out = execute_render_ui(
        {
            "component": "ProductGrid",
            "bind": [{"prop": "products", "ref": "$tool:search_catalog#/products"}],
            "layout": "carousel",  # ignored — 2 products stay side by side
        },
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        flavor_groups=["commerce"],
    )
    assert out.decision == "rendered"
    assert out.ops[0]["props"]["layout"] == "grid"

    three = [
        {"id": f"p{i}", "title": f"P{i}", "price": {"amount": 100.0 + i}}
        for i in range(3)
    ]
    out = execute_render_ui(
        {
            "component": "ProductGrid",
            "bind": [{"prop": "products", "ref": "$tool:search_catalog#/products"}],
        },
        store=_store(three),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        flavor_groups=["commerce"],
    )
    assert out.decision == "rendered"
    assert out.ops[0]["props"]["layout"] == "carousel"


def test_layout_stamp_respects_items_selection():
    """The stamp uses the POST-selection count: picking 2 of 3 bound
    products yields a side-by-side grid, not a carousel."""
    three = [
        {"id": f"p{i}", "title": f"P{i}", "price": {"amount": 100.0 + i}}
        for i in range(3)
    ]
    out = execute_render_ui(
        {
            "component": "ProductGrid",
            "bind": [{"prop": "products", "ref": "$tool:search_catalog#/products"}],
            "items": [{"id": "p0"}, {"id": "p2"}],
        },
        store=_store(three),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
        flavor_groups=["commerce"],
    )
    assert out.decision == "rendered"
    assert [p["id"] for p in out.ops[0]["props"]["products"]] == ["p0", "p2"]
    assert out.ops[0]["props"]["layout"] == "grid"


# ---------------------------------------------------------------------------
# ProductCard retirement (2026-07-30): one product component, count decides
# ---------------------------------------------------------------------------


def test_product_card_retired_from_llm_surface():
    """ProductCard is server_only now: gone from render_ui's component
    enum and from every prompt section; the model gets exactly one product
    component and the count decides presentation."""
    assert "ProductCard" not in COMPS
    out = execute_render_ui(
        {
            "component": "ProductCard",
            "bind": [{"prop": "product", "ref": "$tool:get_product#/product"}],
        },
        store=_store(),
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
    )
    assert out.fn_result["status"] == "error"
    assert "unknown component" in out.fn_result["error"]


def test_single_object_bind_hydrates_as_one_element_grid():
    """get_product's singular ``product`` feeds ProductGrid.products via
    the singleton→list coercion: one-element grid, layout 'grid' (the
    widget renders it as a full-width card)."""
    store = BindingStore()
    store.record(
        "get_product",
        "t9",
        {"product": {"id": "p9", "title": "BB Bottle", "price": {"amount": 799.0}}},
    )
    out = execute_render_ui(
        {
            "component": "ProductGrid",
            "bind": [{"prop": "products", "ref": "$tool:get_product#/product"}],
        },
        store=store,
        allowlist=ALLOW,
        components=COMPS,
        op_id="root",
    )
    assert out.decision == "rendered"
    props = out.ops[0]["props"]
    assert [p["id"] for p in props["products"]] == ["p9"]
    assert props["layout"] == "grid"
    assert out.fn_result["count"] == 1


async def test_rider_on_component_call_is_stripped_and_held(monkeypatch):
    """quick_replies attached to a REAL component render (the model's
    natural grid+chips-in-one-call shape): the rider is harvested for the
    end-of-turn flush and the component call proceeds without it — NOT
    the chips-only deferral path."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch, "forced_final", responses=[]
    )
    result = await agent._render_ui_handler(
        {"component": "ProductGrid", "quick_replies": ["Sizing help"], "bind": []}
    )
    assert agent._held_chips == ["Sizing help"]
    # The component path ran (bind fails in this dataless fixture — that's
    # fine); the chips-only deferral did not.
    assert result.get("deferred") is None


def _tool_call(name, args, call_id):
    from pipecat.frames.frames import FunctionCallFromLLM

    return (
        "tool_call",
        FunctionCallFromLLM(
            function_name=name, tool_call_id=call_id, arguments=args, context=None
        ),
    )


async def test_parallel_mutations_run_solo(monkeypatch):
    """Mutations-run-solo guard (2026-08-03): two state mutations in one
    parallel batch were both authored from the same pre-batch snapshot —
    for UCP carts the second silently REVERTS the first. The first
    dispatches; the second is deferred with a structured soft error and
    never reaches dispatch."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        None,
        responses=[
            [
                _tool_call("update_cart", {}, "m1"),
                _tool_call("update_cart", {}, "m2"),
            ],
            [("text", "Done!")],
        ],
    )
    dispatched = []

    async def _fake_dispatch(call, node, global_funcs, injected_args=None):
        dispatched.append(call.function_name)
        return {"status": "ok"}, None

    monkeypatch.setattr(agent, "_dispatch_tool_call", _fake_dispatch)
    events = await _run_chips_turn(agent, prep)
    assert dispatched == ["update_cart"]  # second mutation never dispatched
    deferred = [
        row
        for row in inserted
        if row.get("content_blocks")
        and any(
            b.get("type") == "tool_result" and "not executed" in str(b.get("content"))
            for b in row["content_blocks"]
        )
    ]
    assert deferred
    # Wire pairing stays balanced: both calls got function_call_completed.
    completed = [ev for ev in events if ev.event == "function_call_completed"]
    assert len(completed) == 2


async def test_single_label_rider_still_flushes(monkeypatch):
    """Live 2026-08-03: Flash attached exactly ONE decisive chip ("Add BB
    Bottle to cart") and the old QuickReplies min_length=2 silently
    discarded it, ending the turn chipless. A single follow-up is
    legitimate — it must render."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [
                ("text", "The BB Bottle is ₹1,189."),
                _rui_call({"quick_replies": ["Add BB Bottle to cart"]}, call_id="one"),
            ],
            # post-tool cycle produces nothing further
            [],
        ],
    )
    events = await _run_chips_turn(agent, prep)
    assert len(calls) == 2  # rider flushed at turn end; no forced cycle
    ops = [ev.data["op"] for ev in events if ev.event == "ui_op"]
    assert len(ops) == 1 and ops[0]["type"] == "QuickReplies"
    labels = [i.get("label") for i in (ops[0].get("props") or {}).get("items", [])]
    assert labels == ["Add BB Bottle to cart"]


# ---------------------------------------------------------------------------
# Narration is not an answer (2026-08-09)
#
# A template may have the model announce work before it calls a tool
# ("Let me find those for you"). That prose streams ahead of the search,
# which is the point — but it is NOT a reply, and the turn-completion
# test used to accept any prose as one. Live consequence: a search that
# matched nothing ended the turn on the acknowledgement plus four quick
# replies, with no answer and no products (metrics: prose_chars=41,
# ui_no_ui=2, render_ui_calls=3).
# ---------------------------------------------------------------------------


def _search_call(query="x", call_id="s-1"):
    from pipecat.frames.frames import FunctionCallFromLLM

    return (
        "tool_call",
        FunctionCallFromLLM(
            function_name="search_catalog",
            tool_call_id=call_id,
            arguments={"catalog": {"query": query}},
            context=None,
        ),
    )


async def test_narration_then_silence_gets_one_recovery_cycle(monkeypatch):
    """Prose + a DATA tool call is narration. If the model then stops
    without replying, the turn must not end on the announcement — it gets
    exactly one more unforced cycle to answer."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            # cycle 1: announces, then searches → narration, not a reply
            [("text", "Let me check our store for those colors."), _search_call()],
            # cycle 2: model goes quiet without answering
            [],
            # cycle 3: nudged — now it actually answers
            [("text", "We don't carry those, but we do have light grey.")],
            # cycle 4: forced chips
            [_rui_call(_chips_args(), call_id="c-1")],
        ],
    )
    events = await _run_chips_turn(agent, prep)

    visible = [m for m in inserted if m.get("role") == "assistant" and m.get("content")]
    assert visible, "the turn must persist an answer, not end on the announcement"
    # Announcement and answer are separate rows, in the order they
    # happened — the announcement rides the row carrying the tool_use it
    # preceded. Writing either text twice is what taught the model to stop
    # announcing on turn 2 (measured 3/3 sessions, 2026-08-09).
    assert len(visible) == 2
    assert visible[0]["content"] == "Let me check our store for those colors."
    assert "light grey" in visible[1]["content"]
    assert "Let me check our store" not in visible[1]["content"]


async def test_narration_alone_never_arms_the_chips_cycle(monkeypatch):
    """Chips follow a REPLY. A turn whose only prose was the announcement
    must not end as acknowledgement + suggestions."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [("text", "Let me check our store for those colors."), _search_call()],
            [],  # silent
            [],  # silent again after the one nudge — give up, don't loop
        ],
    )
    events = await _run_chips_turn(agent, prep)

    chips = [e for e in events if e.event == "ui_op"]
    assert not chips, "no reply was produced, so nothing to suggest follow-ups to"
    # Bounded: the nudge fires once, never in a loop.
    assert len(calls) == 3


async def test_prose_beside_render_ui_is_still_a_reply(monkeypatch):
    """render_ui presents, it never fetches — so prose in the same cycle
    is the answer, and must not be mistaken for narration (which would
    cost every normal turn an extra LLM call)."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [
                ("text", "The BB Bottle is Rs 1,189."),
                _rui_call({"quick_replies": ["Add to cart"]}, call_id="r-1"),
            ],
            [],
        ],
    )
    await _run_chips_turn(agent, prep)
    assert len(calls) == 2, "a normal reply turn must not gain a recovery cycle"


async def test_a_tool_less_template_never_triggers_recovery(monkeypatch):
    """Scaling check: a client with no tools at all (plain conversational
    UI). Every prose cycle is an answer by construction, so the recovery
    path is inert — it can never add a cycle for them."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [("text", "We're open 9 to 6, Monday through Saturday.")],
            [_rui_call(_chips_args(), call_id="c-2")],
        ],
    )
    await _run_chips_turn(agent, prep)
    assert len(calls) == 2
    visible = [m for m in inserted if m.get("role") == "assistant" and m.get("content")]
    assert visible and "9 to 6" in visible[-1]["content"]


# ---------------------------------------------------------------------------
# Narration is not a reply — the four places that conflated the two.
#
# The turn loop asks "has the model replied yet?" in four spots. Each one
# had been reading a proxy for that fact rather than the fact itself, and
# each proxy is true for an OPENER, which is prose but not a reply. Every
# test below fails on the pre-fix code.
# ---------------------------------------------------------------------------


async def test_chips_only_call_after_narration_does_not_kill_the_turn(monkeypatch):
    """The dead-turn regression.

    A chips-only render_ui is the ONLY way a model can author chips in
    forced_final (QuickReplies leaves the component enum), and it can land
    mid-turn — after the opener, before the answer. Suppression armed on
    'any prose streamed' fired on the opener and then silenced the real
    answer, which had not been written yet: no reply row, no
    assistant_message, and the harvested chips discarded with it.

    Suppression must key on a REPLY having been delivered.
    """
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            # cycle 1: opener + a DATA tool → narration
            [("text", "Let me check our store for those."), _search_call()],
            # cycle 2: chips-only rider, no component — the arming shape
            [_rui_call({"quick_replies": ["Best Sellers", "Leggings"]}, call_id="r-1")],
            # cycle 3: the actual answer
            [("text", "We have it in light grey.")],
        ],
    )
    events = await _run_chips_turn(agent, prep)

    visible = [m for m in inserted if m.get("role") == "assistant" and m.get("content")]
    assert any(
        "light grey" in m["content"] for m in visible
    ), "the answer must survive a mid-turn chips-only call"
    kinds = [e.event for e in events]
    assert "assistant_message" in kinds, "the turn must announce its reply"
    assert "ui_op" in kinds, "harvested chips must still paint, not vanish"


async def test_recovery_cycle_can_actually_speak(monkeypatch):
    """The nudge buys one cycle; suppression would make that cycle mute by
    construction (it drops text before streaming, accumulation AND
    persistence). Un-arming is what makes the recovery cycle worth its
    LLM call."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [("text", "One moment."), _search_call()],
            # chips-only call arms suppression under the old rule
            [_rui_call({"quick_replies": ["Best Sellers"]}, call_id="r-1")],
            [],  # silent → nudge fires here
            [("text", "Found three in stock.")],  # the recovered answer
        ],
    )
    await _run_chips_turn(agent, prep)

    visible = [m for m in inserted if m.get("role") == "assistant" and m.get("content")]
    assert any("Found three" in m["content"] for m in visible)


async def test_whitespace_only_cycle_is_not_an_answer(monkeypatch):
    """``turn_text`` holds every non-empty delta, so a lone newline is a
    non-empty list carrying no prose. Counting it as a reply skipped the
    recovery nudge and persisted an empty bubble."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "forced_final",
        responses=[
            [("text", "Let me look that up."), _search_call()],
            [("text", "\n\n")],  # degenerate: no prose at all
            [("text", "Here are three options.")],  # nudged into replying
            [_rui_call(_chips_args(), call_id="c-1")],
        ],
    )
    await _run_chips_turn(agent, prep)

    rows = [m for m in inserted if m.get("role") == "assistant"]
    assert not any(
        (m.get("content") or "").strip() == "" and m.get("content") is not None
        for m in rows
    ), "no assistant row may carry blank content"
    visible = [m for m in rows if m.get("content")]
    assert any("three options" in m["content"] for m in visible)


async def test_narration_row_is_announced_on_the_wire(monkeypatch):
    """The narration row persists VISIBLE, so it is a bubble of its own on
    resume. Without a matching assistant_message the live stream and the
    resume disagree — one bubble live, two after a reload — and a client
    that commits its bubble from assistant_message.content drops the
    narration text entirely."""
    agent, prep, calls, inserted = _chips_agent(
        monkeypatch,
        "model_choice",
        responses=[
            [("text", "Let me check our store."), _search_call()],
            [("text", "We have three in light grey.")],
        ],
    )
    events = await _run_chips_turn(agent, prep)

    announced = [
        e.data.get("content") for e in events if e.event == "assistant_message"
    ]
    visible = [m for m in inserted if m.get("role") == "assistant" and m.get("content")]
    assert len(visible) == 2, "narration and answer are separate rows"
    # Every visible row has an assistant_message carrying its text.
    assert "Let me check our store." in announced
    assert any("light grey" in (c or "") for c in announced)
    assert len(announced) == len(visible)
