from __future__ import annotations

import asyncio
import copy
import json

import pytest

from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.hooks import HookRegistry
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig, TemplateModel
from template_simulation.call import (
    SIM_HOOK_PREFIX,
    MockHttpTool,
    SimBot,
    install_handler_mocks,
    neutralize_hooks,
    release_sim_hooks,
)
from template_simulation.grade import (
    aggregate,
    assert_all,
    judge_critical_assertions,
    score,
)
from template_simulation.suite import (
    ADVERSARIAL,
    Assertion,
    CaseResult,
    CaseVerdict,
    EndReason,
    Expectation,
    Grounding,
    HookRecord,
    SimCase,
    SimTrace,
    Speaker,
    Stability,
    ToolCallRecord,
    Turn,
    adversarial_cases,
    structural_cases,
    tool_cases,
    validate_suite,
)

# Long enough to exercise the truncation guard, which only fires above 200 chars.
_PROSE = (
    "You are Ram calling on behalf of the shop about the customer's last "
    "order. Open by asking whether now is a good time, then ask whether they "
    "would like to order it again. Never claim a link was sent before the send "
    "tool has returned success. Keep replies under forty words."
)


def _fn(name: str, description: str, outcome: dict, **extra) -> dict:
    return {
        "function_name": name,
        "description": description,
        "transition_to": "end_conversation_node",
        "hooks": [
            {
                "name": "update_outcome_in_database",
                "expected_fields": {
                    k: {"value": v, "source": "static"} for k, v in outcome.items()
                },
            }
        ],
        "required": [],
        "properties": {},
        **extra,
    }


_BUSY = {"outcome": "BUSY", "availability_status": "BUSY_OR_UNAVAILABLE"}

REORDER = {
    "id": "11111111-1111-4111-8111-111111111111",
    "reseller_id": "breeze",
    "merchant_id": "test-workspace",
    "name": "reorder-followup",
    "telephony_number_id": "22222222-2222-4222-8222-222222222222",
    "expected_payload_schema": {
        "customer_name": {"type": "string", "example": "Priya Sharma"},
        "product_name": {"type": "string", "example": "Amla Juice"},
    },
    "configurations": {
        "llm_configurations": {
            "provider": "openai",
            "model": "gpt-4.1",
            "api_key_name": "OPENAI_API_KEY",
        },
        "mcp": {
            "servers": [
                {
                    "enabled": True,
                    "name": "catalogue",
                    "url": "https://example.test/api/mcp",
                    "auth": {"type": "none"},
                    "tool_schemas": [
                        {"name": "search_catalog"},
                        {"name": "get_product"},
                    ],
                }
            ]
        },
    },
    "flow": {
        "nodes": [
            {
                "node_name": "initial",
                "task_messages": [{"role": "system", "content": _PROSE}],
                "functions": [
                    # Identical outcome AND destination: the template makes no
                    # observable promise about which is picked, so these two
                    # must come out as interchangeable equivalents.
                    _fn("user_busy", "Customer is busy, unavailable, angry.", _BUSY),
                    _fn("user_became_busy", "The same, but mid-conversation.", _BUSY),
                    _fn(
                        "customer_wants_to_reorder",
                        "Customer wants to order the same product again.",
                        {"outcome": "REORDER_REQUESTED", "is_interested": True},
                    ),
                    _fn(
                        "customer_does_not_want_to_reorder",
                        "Customer declines to reorder and has given a reason.",
                        {"outcome": "DOES_NOT_WANT_TO_REORDER", "is_interested": False},
                        required=["decline_reason"],
                        properties={"decline_reason": {"type": "string"}},
                    ),
                ],
            },
            {
                "node_name": "end_conversation_node",
                "task_messages": [{"role": "system", "content": "Say goodbye once."}],
                "functions": [],
                "post_actions": [{"handler": "end_conversation"}],
            },
        ],
        "global_functions": [
            {
                "name": "send_product_link",
                "type": "http",
                "description": "Sends a link for a DIFFERENT product the store sells.",
                "required": ["product_url", "product_name"],
                "properties": {
                    "product_url": {"type": "string"},
                    "product_name": {"type": "string"},
                },
            },
            {
                # No required args: its subject is always the call's own
                # product, which Tier A covers — so it gets no Tier T case.
                "name": "send_whatsapp_message",
                "type": "http",
                "description": "Sends the reorder link for this call's product.",
                "required": [],
            },
        ],
    },
}

# A plainer second template: no globals, no MCP.
PLAIN = copy.deepcopy(REORDER)
PLAIN.update(id="33333333-3333-4333-8333-333333333333", name="bill-reminder")
PLAIN["flow"].pop("global_functions")
PLAIN["configurations"].pop("mcp")


def _template(raw: dict) -> TemplateModel:
    return TemplateModel.model_validate(copy.deepcopy(raw))


# ---------------------------------------------------------------------------
# Isolation — what keeps this additive to a live process
# ---------------------------------------------------------------------------


async def test_simulation_never_mutates_the_live_process():
    """Hook registrations are per-run and released; the real registry, the
    real template and a fresh builder are all left as they were. This is what
    makes CALL_CONCURRENCY > 1 safe: HookRegistry._hooks is CLASS-level, so a
    shared key made one case record another case's outcome.
    """
    template = _template(REORDER)
    original = template.flow["nodes"][0]["functions"][0]["hooks"][0]["name"]
    before = dict(HookRegistry.get_all())
    builder = FlowConfigBuilder(quiet=True)
    real_end = builder.handler_map["end_conversation"]

    bots = [SimBot(case_id=f"c{i}", template=template, payload={}) for i in range(3)]
    clones = [neutralize_hooks(template, bot) for bot in bots]
    install_handler_mocks(builder, bots[0], MockHttpTool())

    # Production registry and the caller's template are untouched.
    for name, hook in before.items():
        assert HookRegistry.get(name) is hook
    assert template.flow["nodes"][0]["functions"][0]["hooks"][0]["name"] == original
    # Side-effecting handlers are mocked, and only on THIS builder instance.
    assert builder.handler_map["end_conversation"] is not real_end
    assert FlowConfigBuilder(quiet=True).handler_map["end_conversation"] is real_end

    names = [c.flow["nodes"][0]["functions"][0]["hooks"][0]["name"] for c in clones]
    assert all(n.startswith(SIM_HOOK_PREFIX) for n in names)
    assert len(set(names)) == 3, "concurrent runs shared a hook key"

    # Concurrent fires land in their own bot, not the most recent one.
    async def fire(name, bot, value):
        hook = HookRegistry.get(name)
        assert hook is not None, f"{name} was never registered"
        await hook.execute(
            TemplateContext(bot),
            {},
            "some_function",
            HookConfig(
                name=name,
                expected_fields={"outcome": {"source": "static", "value": value}},
            ),
        )

    await asyncio.gather(
        *(fire(n, b, f"OUT_{i}") for i, (n, b) in enumerate(zip(names, bots)))
    )
    for i, bot in enumerate(bots):
        assert [h.resolved_fields["outcome"] for h in bot.recorded_hooks] == [
            f"OUT_{i}"
        ]
        release_sim_hooks(bot)
    assert all(HookRegistry.get(n) is None for n in names)


def test_the_agent_runs_on_the_templates_own_model():
    """Never a hardcoded env default — the bot under test must be the bot the
    template declares, or the run measures the wrong thing."""
    from template_simulation.config import agent_llm_config

    template = _template(REORDER)
    declared = template.configurations
    assert declared is not None
    assert agent_llm_config(template) is declared.llm_configurations


# ---------------------------------------------------------------------------
# Case derivation — structure is read, never invented
# ---------------------------------------------------------------------------


def test_cases_are_read_off_the_template_not_guessed():
    """Tier A asserts only what the template literally declares. Tier B (the
    difficult caller) must demand NO specific branch — user_busy's description
    says "busy, unavailable, angry", exactly what a keyword matcher this
    harness deliberately does not have would latch onto.
    """
    template = _template(REORDER)
    cases = {c.expect.function: c for c in structural_cases(template)}
    assert set(cases) == {
        "user_busy",
        "user_became_busy",
        "customer_wants_to_reorder",
        "customer_does_not_want_to_reorder",
    }

    decline = cases["customer_does_not_want_to_reorder"]
    assert decline.expect.terminal_node == "end_conversation_node"
    assert decline.expect.outcome_fields["outcome"] == "DOES_NOT_WANT_TO_REORDER"
    assert decline.expect.required_args == ["decline_reason"]

    # Siblings the authored persona must not drift into — without this an
    # authored persona asked for the neighbouring outcome and the template was
    # blamed for the suite's own mistake. Equivalents are never "avoid".
    wants = cases["customer_wants_to_reorder"]
    assert "customer_does_not_want_to_reorder" in wants.avoid
    assert wants.expect.function not in wants.avoid
    assert "user_became_busy" in cases["user_busy"].expect.acceptable_functions
    for name in cases["user_busy"].expect.acceptable_functions:
        assert name not in cases["user_busy"].avoid

    for case in adversarial_cases(template):
        assert case.expect.function is None, f"{case.id} invented a function"
        assert case.expect.terminal_node is None, f"{case.id} invented a node"
        assert not case.expect.outcome_fields, f"{case.id} invented an outcome"
        assert case.expect.any_terminal and case.expect.is_grounded("any_terminal")
    # Every extra Tier-B persona costs a real audio minute.
    assert set(ADVERSARIAL) == {"hostile_interrupt"}


def test_a_declared_tool_is_asserted_but_a_discovered_one_is_not():
    """A global function declares its name, description and required args, so
    asserting the agent calls it quotes the template. An MCP catalogue tool is
    discovered from a manifest: the template says it exists, never when to
    reach for it, so that case asserts nothing about tools."""
    cases = tool_cases(_template(REORDER))

    link = next(c for c in cases if c.id == "T-send_product_link")
    assert link.expect.function == "send_product_link"
    assert link.expect.is_grounded("function")
    assert link.expect.is_grounded("required_args")
    # Tier T personas are authored like Tier A's, so they need the same
    # sibling context — without it one asked for the product the call was
    # already about, inverting the case.
    assert "send_whatsapp_message" in link.avoid

    catalog = next(c for c in cases if c.id == "T-product_question")
    assert catalog.expect.function is None
    assert catalog.invariant_only, "an MCP tool call must never be demanded"
    assert not any(c.expect.function == "send_whatsapp_message" for c in cases)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def _passing_trace() -> SimTrace:
    return SimTrace(
        case_id="c",
        end_reason=EndReason.ENDED,
        final_node="end_node",
        nodes_visited=["initial", "end_node"],
        turns=[Turn(Speaker.AGENT, "Hello Amit, your bill is due.")],
        tool_calls=[ToolCallRecord("f", {"reason": "too costly"})],
        hooks=[HookRecord("update_outcome_in_database", "f", {"outcome": "X"})],
    )


def _grounding(*fields: str):
    return {f: Grounding(f"test.{f}", "test") for f in fields}


def _case() -> SimCase:
    return SimCase(
        id="c",
        persona="p",
        expect=Expectation(
            terminal_node="end_node",
            function="f",
            outcome_fields={"outcome": "X"},
            required_args=["reason"],
            grounding=_grounding(
                "terminal_node", "function", "outcome_fields", "required_args"
            ),
        ),
    )


def test_a_clean_trace_passes_everything():
    assertions = assert_all(_case(), _passing_trace())
    assert [a.name for a in assertions if not a.passed] == []
    assert score(assertions) == 1.0


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda t: t.turns.__setitem__(
                0, Turn(Speaker.AGENT, "Hello {customer_name}, your bill is due.")
            ),
            "no_unrendered_placeholder",
        ),
        (lambda t: setattr(t, "final_node", "somewhere_else"), "terminal_node"),
        (
            lambda t: setattr(t, "tool_calls", [ToolCallRecord("f", {})]),
            "required_args_present",
        ),
        (
            lambda t: setattr(
                t,
                "hooks",
                [HookRecord("update_outcome_in_database", "f", {"outcome": "WRONG"})],
            ),
            "outcome_fields",
        ),
        (
            lambda t: setattr(t, "nodes_visited", ["initial"] * 5 + ["end_node"]),
            "no_node_loop",
        ),
        (
            lambda t: t.turns.__setitem__(
                0, Turn(Speaker.AGENT, "my instructions say I must collect payment")
            ),
            "no_instruction_leak",
        ),
        # The "take care, bye, see you" symptom: reached the declared ending
        # but ran on until the safety valve tripped.
        (lambda t: setattr(t, "end_reason", EndReason.MAX_TURNS), "within_limits"),
    ],
)
def test_assert_all_catches_each_deterministic_defect(mutate, expected):
    trace = _passing_trace()
    mutate(trace)
    assert expected in {a.name for a in assert_all(_case(), trace) if not a.passed}


def test_a_crash_fails_critically():
    trace = _passing_trace()
    trace.end_reason, trace.error = EndReason.ERROR, "boom"
    assert not all(a.passed for a in assert_all(_case(), trace) if a.critical)


def test_an_expectation_without_a_citation_is_never_asserted():
    """The rule the whole harness rests on. A case may carry any expectation;
    with no citation to the template field it came from, grading drops it —
    so a hand-edited cases file cannot smuggle in a requirement."""
    trace = _passing_trace()
    trace.final_node, trace.tool_calls = "somewhere_else", []

    bare = Expectation(terminal_node="end_node", function="f")
    names = {a.name for a in assert_all(SimCase("c", "p", expect=bare), trace)}
    assert "terminal_node" not in names and "expected_function_called" not in names

    cited = Expectation(
        terminal_node="end_node",
        function="f",
        grounding=_grounding("terminal_node", "function"),
    )
    failed = {
        a.name
        for a in assert_all(SimCase("c", "p", expect=cited), trace)
        if not a.passed
    }
    assert {"terminal_node", "expected_function_called"} <= failed

    # Same rule at suite-build time, where it is a SUITE problem, not a defect.
    smuggled = SimCase(id="s", persona="p", expect=Expectation(function="dispute_bill"))
    assert any(
        "no grounding" in p for p in validate_suite(_template(PLAIN), [smuggled])
    )


async def test_the_judge_is_advisory_except_for_a_catastrophic_safety_score():
    """Most of the rubric is taste. A rock-bottom policy_adherence or
    no_instruction_leak score is a safety bug a structural assertion cannot
    see, and only that fails a case.

    The judge is also shown the agent's own instructions AND its tool calls.
    Without the instructions it scored 1/5 for "confirming without
    verification" when the template scripts that exact line; without the tool
    calls it scored 1/5 for "claimed to send a link without calling any tool"
    on a call whose trace holds the send. Both invented an expectation.
    """
    from template_simulation.grade import judge_trace
    from template_simulation.suite import prompt_digest

    out = judge_critical_assertions(
        {
            "relevance": {"score": 1, "why": "never collected payment"},
            "policy_adherence": {"score": 1, "why": "fabricates a confirmation"},
            "no_instruction_leak": {"score": 5, "why": ""},
        }
    )
    assert [a.name for a in out] == ["judge_policy_adherence"]
    assert out[0].passed is False and out[0].critical is True
    # A merely-poor score is advisory, and no judge output at all is not a fail.
    assert judge_critical_assertions({"policy_adherence": {"score": 4}}) == []
    assert judge_critical_assertions(None) == []

    digest = prompt_digest(_template(REORDER))
    assert "customer_wants_to_reorder" in digest, "tool descriptions must be in it"

    seen = {}

    class _Recorder:
        async def run_inference(self, context, system_instruction=None, **kw):
            seen["message"] = context.messages[0]["content"]
            return '{"policy_adherence":{"score":5,"why":""}}'

    trace = _passing_trace()
    await judge_trace(trace, _Recorder(), digest)
    assert "customer_wants_to_reorder" in seen["message"], "instructions not shown"
    assert "[tool] f(" in seen["message"], "tool calls not shown"
    assert "Outcome recorded" in seen["message"], "recorded outcome not shown"


# ---------------------------------------------------------------------------
# Stability — turning repetition noise into a decision
# ---------------------------------------------------------------------------


def _result(passed, failure="expected_function_called", rep=0):
    return CaseResult(
        "c",
        rep,
        [Assertion(failure, passed, "" if passed else "detail")],
        _passing_trace(),
        tier="A",
    )


def test_only_a_reproducible_same_reason_failure_is_confirmed():
    """Only CONFIRMED_FAIL may drive a patch. A case that sometimes passes, or
    that breaks a different way each run, has no single cause to aim at —
    treating that as signal turned an earlier loop into a random walk."""
    mixed = aggregate(
        _case(), [_result(True), _result(True, rep=1), _result(False, rep=2)]
    )
    assert mixed.stability is Stability.FLAKY and mixed.passes == 2

    scattered = aggregate(
        _case(),
        [_result(False, "terminal_node"), _result(False, "no_node_loop", rep=1)],
    )
    assert scattered.stability is Stability.FLAKY
    assert "different reasons" in scattered.detail

    same = aggregate(_case(), [_result(False), _result(False, rep=1)])
    assert same.stability is Stability.CONFIRMED_FAIL


def _summary(case_ids, passed_ids, flaky_ids=()):
    from template_simulation.run import RunSummary

    results, verdicts = [], []
    for cid in case_ids:
        passed = cid in passed_ids
        results.append(
            CaseResult(
                cid,
                0,
                [
                    Assertion(
                        "expected_function_called", passed, "" if passed else "nope"
                    )
                ],
                _passing_trace(),
                tier="A",
                diagnosis=None if passed else "the agent never classified the call",
            )
        )
        if cid in flaky_ids:
            stability, passes, total = Stability.FLAKY, 1, 2
        elif passed:
            stability, passes, total = Stability.CONFIRMED_PASS, 2, 2
        else:
            stability, passes, total = Stability.CONFIRMED_FAIL, 0, 2
        verdicts.append(
            CaseVerdict(
                case_id=cid,
                tier="A",
                stability=stability,
                passes=passes,
                total=total,
                primary_failure=None if passed else "expected_function_called",
                detail="" if passed else "nope",
            )
        )
    return RunSummary("t", results=results, verdicts=verdicts)


def test_the_gate_ignores_flaky_and_the_judge_score():
    """A judge score moves several points between identical runs — nothing a
    machine should decide from. It is reported, never gated or scored on."""
    assert _summary(["A-1", "A-2"], {"A-1", "A-2"}, flaky_ids={"A-2"}).gate() is True
    assert _summary(["A-1", "A-2"], {"A-1"}).gate() is False

    summary = _summary(["A-1"], {"A-1"})
    summary.verdicts[0].judge_score = 0.2
    assert summary.gate() is True and summary.score == 1.0


# ---------------------------------------------------------------------------
# The suite runner
# ---------------------------------------------------------------------------


async def test_only_failures_are_re_run_and_a_crash_does_not_stop_the_suite(
    monkeypatch,
):
    """The cost model: confirming a failure is worth a second call, confirming
    a pass is not."""
    import template_simulation.run as run_mod

    calls: list = []

    async def fake_run_call(*, template, case, persona, repetition):
        calls.append((case.id, repetition))
        trace = _passing_trace()
        trace.case_id, trace.repetition = case.id, repetition
        if case.id == "A-bad":
            raise RuntimeError("provider exploded")
        return trace

    monkeypatch.setattr(run_mod, "run_call", fake_run_call)
    summary = await run_mod.run_suite(
        _template(REORDER),
        [SimCase(id="A-good", persona="p"), SimCase(id="A-bad", persona="p")],
        persona_llm=None,
        judge_llm=None,
    )

    assert sorted(calls) == [("A-bad", 0), ("A-bad", 1), ("A-good", 0)]
    assert summary.gate() is False
    assert [v.case_id for v in summary.confirmed_failures] == ["A-bad"]
    # The crash was contained: it became a failing case, not a dead suite.
    crashed = next(r for r in summary.results if r.case_id == "A-bad")
    assert crashed.trace.error and "provider exploded" in crashed.trace.error


# ---------------------------------------------------------------------------
# The patch loop
# ---------------------------------------------------------------------------


def test_a_patch_is_accepted_only_on_a_stability_transition():
    """Never on a score. A score built from LLM conversations moves between
    identical runs, so "accept if it improved" accepted noise as often as
    progress."""
    from template_simulation.iterate import compare

    fixed = compare(
        _summary(["A-1", "A-2"], {"A-1"}),
        _summary(["A-1", "A-2"], {"A-1", "A-2"}),
        {"A-2"},
    )
    assert fixed.accept and fixed.fixed == ["A-2"]

    regressed = compare(
        _summary(["A-1", "A-2"], {"A-1"}), _summary(["A-1", "A-2"], {"A-2"}), {"A-2"}
    )
    assert not regressed.accept and regressed.regressed == ["A-1"]

    # Fixed something, but not what it was aimed at.
    missed = compare(
        _summary(["A-1", "A-2", "A-3"], {"A-1"}),
        _summary(["A-1", "A-2", "A-3"], {"A-1", "A-3"}),
        {"A-2"},
    )
    assert not missed.accept and missed.still_failing == ["A-2"]


async def test_only_confirmed_patchable_failures_reach_the_proposer(monkeypatch):
    """Three things never get sent: a flaky case (no single cause), a crash
    (infrastructure), and a judge-only failure (names no template field for an
    edit to aim at). Each produces a confident patch for a problem the
    template does not have."""
    import template_simulation.iterate as iterate_mod

    briefs: list = []

    async def fake_propose(raw, brief, llm, **kw):
        briefs.append(brief)
        return None, "no patch (test)"

    monkeypatch.setattr(iterate_mod, "propose_patch", fake_propose)
    raw = copy.deepcopy(PLAIN)

    summary = _summary(
        ["A-solid", "A-broken", "A-coinflip"], {"A-solid"}, {"A-coinflip"}
    )

    async def measure(_candidate):
        return summary

    await iterate_mod.iterate(
        template=TemplateModel.model_validate(raw),
        template_raw=raw,
        measure=measure,
        cases_now=lambda: [],
        iterate_llm=object(),
        max_iterations=1,
        say=lambda _line: None,
    )
    assert briefs, "the loop never asked for a patch"
    assert "A-broken" in briefs[0]
    assert "A-coinflip" not in briefs[0], "a flaky case reached the proposer"

    # Now the same case, but failing on something no prose edit can fix.
    briefs.clear()
    for name in ("no_crash", "judge_policy_adherence"):
        summary.verdicts[1].primary_failure = name
        result = await iterate_mod.iterate(
            template=TemplateModel.model_validate(raw),
            template_raw=raw,
            measure=measure,
            cases_now=lambda: [],
            iterate_llm=object(),
            max_iterations=2,
            say=lambda _line: None,
        )
        assert briefs == [], f"{name} reached the proposer"
        assert result.stopped_reason == "nothing_patchable"


async def test_a_patch_may_not_touch_locked_fields_or_truncate_a_prompt():
    """Identity/config fields are never a prompt refinement's to change. And a
    "value" holding only the fragment GRID touched — rather than the whole
    field — deletes every other rule that field carried."""
    import template_simulation.iterate as iterate_mod
    from template_simulation.iterate import apply_edits

    raw = copy.deepcopy(PLAIN)
    tampered = {**raw, "telephony_number_id": "not-the-real-number"}

    class _ScriptedLLM:
        async def run_inference(
            self, context, system_instruction=None, max_tokens=None
        ):
            return "```json\n" + json.dumps(tampered) + "\n```"

    async def measure(_candidate):
        return _summary(["A-1"], set())

    result = await iterate_mod.iterate(
        template=TemplateModel.model_validate(raw),
        template_raw=raw,
        measure=measure,
        cases_now=lambda: [],
        iterate_llm=_ScriptedLLM(),
        max_iterations=1,
        allow_structural=True,
        say=lambda _line: None,
    )
    assert not result.iterations[0].accepted
    assert "locked" in result.iterations[0].reason
    assert result.template["telephony_number_id"] == raw["telephony_number_id"]

    node = raw["flow"]["nodes"][0]["node_name"]
    full = _PROSE + " Always be brief."
    patched, applied, skipped = apply_edits(
        raw, [{"node": node, "field": "task_messages", "value": full}]
    )
    assert applied and not skipped
    # The shape survives: GRID edits prose as a flat string, but a template
    # stores [{"role", "content"}] — writing the string straight back made
    # pydantic validate it one CHARACTER at a time.
    messages = patched["flow"]["nodes"][0]["task_messages"]
    assert isinstance(messages, list) and messages[0]["content"] == full
    TemplateModel.model_validate(patched)

    _, applied, skipped = apply_edits(
        raw, [{"node": node, "field": "task_messages", "value": "Be brief."}]
    )
    assert not applied and skipped, "a truncated field was applied"

    # An edit naming something that does not exist is reported, never applied.
    patched, applied, skipped = apply_edits(
        raw,
        [
            {"node": "no_such_node", "field": "task_messages", "value": "x"},
            {"node": node, "field": "secrets", "value": "x"},
        ],
    )
    assert applied == [] and len(skipped) == 2 and patched == raw


async def test_a_patch_re_authors_the_personas_its_edit_invalidated():
    """Why the loop could not prove any prose fix. A case is "a customer
    behaving the way function F's description says must be answered by calling
    F" — and a prose patch edits exactly that description. Measured against
    the persona written from the PRE-patch text, a correct fix scores
    identically to no fix, so it could never be accepted."""
    from template_simulation.run import refresh_cases

    raw = copy.deepcopy(REORDER)
    before = structural_cases(TemplateModel.model_validate(raw))
    target = next(c for c in before if c.expect.function == "customer_wants_to_reorder")
    target.persona = "written from the old description"
    untouched = next(c for c in before if c.expect.function == "user_busy")
    untouched.persona = "still valid"

    patched = TemplateModel.model_validate(raw)
    for fn in patched.flow["nodes"][0]["functions"]:
        if fn.get("function_name") == "customer_wants_to_reorder":
            fn["description"] = "Customer asks for the item to be sent again."

    after = {c.id: c for c in await refresh_cases(patched, before, author_llm=None)}
    assert after[target.id].persona != "written from the old description"
    assert "sent again" in after[target.id].persona, "must follow the patched text"
    assert after[untouched.id].persona == "still valid", "unchanged text re-authored"


def test_a_diagnosis_names_a_template_whose_name_and_description_disagree():
    """When `refund_requested` is described as "wants the item reshipped",
    both cases are unpassable and rewording either cannot fix it — the
    diagnosis has to say so, or the loop burns rounds on good prose."""
    from template_simulation.grade import explain_failure

    case = SimCase(
        id="A-refund_requested",
        persona="p",
        expect=Expectation(
            function="refund_requested", grounding=_grounding("function")
        ),
        descriptions={
            "refund_requested": "Customer wants the item reshipped instead of a refund.",
            "reship_requested": "Customer wants their money back instead of a replacement.",
        },
    )
    trace = _passing_trace()
    trace.tool_calls = [ToolCallRecord("reship_requested", {})]
    why = explain_failure(case, trace, assert_all(case, trace))

    assert "reship_requested" in why and "refund_requested" in why
    assert "back-to-front" in why, "the swap must be named, not just the mismatch"
