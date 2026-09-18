"""The playbook: the plan chooses the agent's words
(modules/05-outreach §The playbook; canon T19 col 6).

The agent is an actor whose template is a script with holes. What fills them
is decided here and handed over FINISHED — the template loader substitutes in
one pass over a flat dict, so a hole still open when the text leaves is a hole
the agent reads aloud on a live call. It cannot catch that; publish and the
walker must.

Every law below is proved by injecting exactly what it forbids.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import app.crm.outreach.nodes.call as call_node
from app.crm.outreach import playbook
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "crm"
    / "plans"
    / "line-nudge-playbook.json"
)

_DOC: Dict[str, Any] = {
    "entry": {"topic": "OFFERED"},
    "nodes": [
        {
            "id": "call-1",
            "type": "call",
            "template_id": "tpl-1",
            "blocks": ["hook_line", "walk"],
        }
    ],
    "edges": [],
    "goals": [{"topics": ["ACTIVE"]}],
    "playbook": {
        "lines": {
            "hook_free": "aapne {product_name} ke liye checkout shuru kiya tha",
            "hook_plain": "aapne checkout shuru kiya tha",
            "step_open": "pehle app kholiye",
            "step_cart": "ab cart me {product_name} hoga",
        },
        "blocks": {
            "hook_line": [
                {
                    "when": [{"field": "context.no_cost", "op": "is", "value": "yes"}],
                    "say": "hook_free",
                },
                {"say": "hook_plain"},
            ],
            "walk": [
                {
                    "when": [
                        {"field": "context.lender_name", "op": "is", "value": "Fibe"}
                    ],
                    "say": ["step_open", "step_cart"],
                },
                {"say": ["step_open"]},
            ],
        },
    },
}


def _doc(**over: Any) -> Dict[str, Any]:
    return {**json.loads(json.dumps(_DOC)), **over}


def _definition(doc: Optional[Dict[str, Any]] = None) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(doc or _doc())


def _run(context: Optional[Dict[str, Any]] = None) -> EnrollmentRun:
    return EnrollmentRun(
        id="6f6603bf-1bf5-4f46-b242-58e9f40833d2",
        merchant_id="m1",
        workflow_id="11111111-1111-1111-1111-111111111111",
        workflow_version=1,
        customer_id="22222222-2222-2222-2222-222222222222",
        status="waiting",
        current_node="call-1",
        wake_at=None,
        entered_at="2026-09-09T11:26:00Z",
        exited_at=None,
        exit_reason=None,
        enrollment_key="k",
        attempts=0,
        last_error=None,
        context={"phone": "+919110752252", **(context or {})},
    )


# --- the pick and the render --------------------------------------------------


def test_the_first_row_that_holds_wins_and_the_last_is_the_default() -> None:
    d = _definition()
    free, _ = playbook.resolve(
        d, ["hook_line"], {"no_cost": "yes", "product_name": "S24"}, {}, None
    )
    plain, _ = playbook.resolve(
        d, ["hook_line"], {"no_cost": "no", "product_name": "S24"}, {}, None
    )
    assert free["hook_line"] == "aapne S24 ke liye checkout shuru kiya tha"
    assert plain["hook_line"] == "aapne checkout shuru kiya tha"


def test_a_list_say_renders_one_step_per_line_in_order() -> None:
    """The ORDER section of the prompt, generated: a call agent reads them as
    steps and the name rides along, so it can say which step is done."""
    out, _ = playbook.resolve(
        _definition(),
        ["walk"],
        {"lender_name": "Fibe", "product_name": "S24"},
        {},
        None,
    )
    assert out["walk"] == (
        '- step_open: "pehle app kholiye"\n- step_cart: "ab cart me S24 hoga"'
    )


def test_only_the_blocks_asked_for_are_rendered() -> None:
    """A block nobody asks for is never evaluated, never in a payload, never
    in a log — the send node's own philosophy."""
    out, _ = playbook.resolve(_definition(), ["hook_line"], {"no_cost": "no"}, {}, None)
    assert set(out) == {"hook_line"}


def test_a_leftover_hole_parks_the_run_naming_it() -> None:
    """The agent's substitution is ONE pass, so a hole left here is spoken
    aloud. The run parks instead, with the hole named."""
    with pytest.raises(NodeParked, match=r"product_name"):
        playbook.resolve(_definition(), ["hook_line"], {"no_cost": "yes"}, {}, None)


def test_a_line_fills_from_facts_never_from_another_line() -> None:
    """No chains, no cycles, always total: a line whose text happens to name
    another line's key gets the FACT of that name, or parks."""
    doc = _doc()
    doc["playbook"]["lines"]["hook_plain"] = "see {hook_free}"
    with pytest.raises(NodeParked, match=r"hook_free"):
        playbook.resolve(_definition(doc), ["hook_line"], {"no_cost": "no"}, {}, None)


# --- the publish laws, each proved by injecting what it forbids ---------------


def test_a_block_without_a_default_row_is_refused() -> None:
    doc = _doc()
    doc["playbook"]["blocks"]["hook_line"] = [
        {
            "when": [{"field": "context.no_cost", "op": "is", "value": "yes"}],
            "say": "hook_free",
        }
    ]
    assert any(
        "the last row must have no `when`" in p for p in validate_definition(doc)
    ), validate_definition(doc)


def test_a_default_before_the_last_row_is_refused() -> None:
    """An early default makes every row under it unreachable — dead words an
    author would never see fire."""
    doc = _doc()
    doc["playbook"]["blocks"]["hook_line"] = [
        {"say": "hook_plain"},
        {
            "when": [{"field": "context.no_cost", "op": "is", "value": "yes"}],
            "say": "hook_free",
        },
        {"say": "hook_plain"},
    ]
    assert any("needs a `when`" in p for p in validate_definition(doc))


def test_a_say_naming_no_line_is_refused() -> None:
    doc = _doc()
    doc["playbook"]["blocks"]["hook_line"][0]["say"] = "hook_freee"
    assert any("'hook_freee' is not in lines" in p for p in validate_definition(doc))


def test_a_when_field_the_grammar_refuses_is_refused() -> None:
    """A block reads what a condition square can read, and nothing more."""
    doc = _doc()
    doc["playbook"]["blocks"]["hook_line"][0]["when"] = [
        {"field": "nonsense.field", "op": "exists"}
    ]
    assert any("is not a condition field" in p for p in validate_definition(doc))


def test_a_when_may_not_read_a_handle() -> None:
    doc = _doc()
    doc["playbook"]["blocks"]["hook_line"][0]["when"] = [
        {"field": "customer.attributes.phone", "op": "exists"}
    ]
    assert any(
        "a handle is never readable by a predicate" in p
        for p in validate_definition(doc)
    )


def test_a_line_carrying_a_line_break_is_refused() -> None:
    """One line is one line; a list block is what renders many."""
    doc = _doc()
    doc["playbook"]["lines"]["hook_plain"] = "first\nsecond"
    assert any("carries a line break" in p for p in validate_definition(doc))


def test_a_square_may_only_ask_for_a_block_the_playbook_declares() -> None:
    doc = _doc()
    doc["nodes"][0]["blocks"] = ["hook_line", "nope"]
    assert any("'nope' is not a playbook block" in p for p in validate_definition(doc))


def test_a_many_line_block_may_not_fill_a_whatsapp_blank() -> None:
    """The existing newline law, reached at publish: `walk` to a call is
    fine, `walk` mapped to a template parameter is refused."""
    doc = _doc()
    doc["nodes"].append(
        {
            "id": "nudge",
            "type": "send",
            "channel": "whatsapp",
            "template": "t",
            "variables": {"1": "walk"},
        }
    )
    assert any(
        "renders many lines — a template blank is one line" in p
        for p in validate_definition(doc)
    ), validate_definition(doc)


def test_a_single_line_block_may_fill_a_whatsapp_blank() -> None:
    """The law is about the SHAPE of the block, not about blocks at all."""
    doc = _doc()
    doc["nodes"].append(
        {
            "id": "nudge",
            "type": "send",
            "channel": "whatsapp",
            "template": "t",
            "variables": {"1": "hook_line"},
        }
    )
    assert not any("renders many lines" in p for p in validate_definition(doc))


def test_a_plan_with_no_playbook_is_judged_exactly_as_before() -> None:
    doc = _doc()
    doc.pop("playbook")
    doc["nodes"][0].pop("blocks")
    assert playbook.laws(_definition(doc)) == []


# --- the merge sites ----------------------------------------------------------


def _install(monkeypatch: pytest.MonkeyPatch, captured: Dict[str, Any]) -> None:
    async def fake_create(**kw: Any) -> Any:
        captured.update(kw)
        return type("L", (), {"id": kw["id"]})()

    async def fake_template(_id: str) -> Any:
        return type(
            "T",
            (),
            {"id": "tpl-1", "name": "n", "reseller_id": "r", "merchant_id": None},
        )()

    async def fake_config(_id: str) -> Any:
        return type("C", (), {"initial_offset": 0})()

    async def fake_stamp(_a: str, _b: str) -> None:
        return None

    monkeypatch.setattr(call_node, "create_lead_call_tracker", fake_create)
    monkeypatch.setattr(call_node, "get_template_by_id", fake_template)
    monkeypatch.setattr(
        call_node, "get_call_execution_config_by_template_id", fake_config
    )
    monkeypatch.setattr(call_node, "update_lead_enrollment_id", fake_stamp)


def test_a_call_square_carries_its_blocks_in_the_lead_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}
    _install(monkeypatch, captured)
    d = _definition()
    run = _run({"product_name": "S24", "lender_name": "Fibe", "no_cost": "yes"})

    asyncio.run(call_node.execute(run, d.nodes[0], d))

    payload = captured["payload"]
    assert payload["hook_line"] == "aapne S24 ke liye checkout shuru kiya tha"
    assert payload["walk"].startswith('- step_open: "pehle app kholiye"')


def test_the_text_never_touches_the_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context scalars are capped and canon keeps the row small, so a
    rendered walk rides the payload and nothing else."""
    captured: Dict[str, Any] = {}
    _install(monkeypatch, captured)
    d = _definition()
    run = _run({"product_name": "S24", "lender_name": "Fibe", "no_cost": "yes"})

    written = asyncio.run(call_node.execute(run, d.nodes[0], d))

    assert "hook_line" not in run.context and "walk" not in run.context
    assert not any(key in written for key in ("hook_line", "walk"))


def test_playbook_is_a_bookkeeping_prefix() -> None:
    """playbook_<node> may record WHICH row was chosen without the name ever
    reaching a template variable or a lead payload key."""
    from app.crm.outreach.nodes.context import is_bookkeeping, run_facts

    assert is_bookkeeping("playbook_call-1")
    assert "playbook_call-1" not in run_facts({"playbook_call-1": "hook_free", "a": 1})


# --- the shipped fixture ------------------------------------------------------


def test_the_playbook_fixture_publishes_and_renders() -> None:
    assert FIXTURE.is_file(), FIXTURE
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    assert validate_definition(raw) == []

    d = WorkflowDefinition.model_validate(raw)
    asked = [node.blocks for node in d.nodes if node.type == "call"]
    assert asked and all(a for a in asked), "every call square lists what it takes"

    out, _ = playbook.resolve(
        d,
        ["hook_line", "walk", "lender_notes"],
        {"sub_category": "Mobile", "offers": "Finnable: 9 months"},
        {},
        None,
    )
    assert "Mobile" in out["hook_line"]
    assert out["walk"].count("\n") == 3, "four steps, one per line"
    assert "{" not in "".join(out.values()), "no hole reaches the agent"


# --- the three merge sites, pinned by ONE test (the #1143 finding) -------------


def _send_doc() -> Dict[str, Any]:
    doc = _doc()
    doc["purpose_key"] = "utility.loan.checkout_nudge"
    doc["nodes"] = [
        {
            "id": "nudge",
            "type": "send",
            "channel": "whatsapp",
            "template": "t",
            "variables": {"1": "hook_line"},
        }
    ]
    return doc


def _action_doc() -> Dict[str, Any]:
    doc = _doc()
    doc["nodes"] = [
        {
            "id": "ping",
            "type": "action",
            "connector": "webhook",
            "action": "post",
            "args": {"message": "{hook_line}"},
        }
    ]
    return doc


@pytest.mark.parametrize("square", ["call", "send", "action"])
def test_every_square_that_asks_gets_its_blocks(
    square: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A precondition honoured at one call site and not the others is the
    #1143 finding, so all three are executed here: a call's `blocks`, a
    send's `variables` right-hand side, an action's `args`."""
    facts = {"product_name": "S24", "no_cost": "yes", "lender_name": "Fibe"}
    expected = "aapne S24 ke liye checkout shuru kiya tha"

    if square == "call":
        captured: Dict[str, Any] = {}
        _install(monkeypatch, captured)
        d = _definition()
        written = asyncio.run(call_node.execute(_run(facts), d.nodes[0], d))
        assert captured["payload"]["hook_line"] == expected
        assert written["playbook_call-1"] == {
            "hook_line": "hook_free",
            "walk": "step_open,step_cart",
        }
        return

    if square == "send":
        import app.crm.outreach.nodes.send as send_node

        sent: Dict[str, Any] = {}

        async def fake_queue(**kw: Any) -> Any:
            sent.update(kw)
            return type("M", (), {"id": "m-1"})()

        monkeypatch.setattr(send_node, "queue_message", fake_queue)
        d = _definition(_send_doc())
        written = asyncio.run(send_node.execute(_run(facts), d.nodes[0], d))
        assert sent["variables"]["1"] == expected
        assert written["playbook_nudge"] == {"hook_line": "hook_free"}
        return

    import app.crm.outreach.nodes.action as action_node

    done: Dict[str, Any] = {}

    async def fake_do(
        merchant_id: str, connector: str, action: str, args: Any, meta: Any
    ) -> Any:
        done["args"] = args
        return {"ok": True}

    monkeypatch.setattr(action_node, "perform_action", fake_do)
    d = _definition(_action_doc())
    written = asyncio.run(action_node.execute(_run(facts), d.nodes[0], d))
    assert done["args"]["message"] == expected
    assert written["playbook_ping"] == {"hook_line": "hook_free"}


# --- the block-name laws, each proved by injecting what it forbids -------------


def test_a_block_may_not_shadow_a_bookkeeping_name() -> None:
    """run_facts filters lead_*/reply_* out of a payload on purpose, and a
    block is merged AFTER the filter — so the name would slip past it."""
    doc = _doc()
    doc["playbook"]["blocks"]["lead_walk"] = [{"say": "hook_plain"}]
    assert any(
        "is a walker bookkeeping name" in p for p in validate_definition(doc)
    ), validate_definition(doc)


def test_a_block_may_not_shadow_a_reserved_payload_key() -> None:
    doc = _doc()
    doc["playbook"]["blocks"]["customer_mobile_number"] = [{"say": "hook_plain"}]
    assert any(
        "is a reserved lead-payload key" in p for p in validate_definition(doc)
    ), validate_definition(doc)


def test_blocks_on_a_square_that_is_not_a_call_is_refused() -> None:
    """The same shape as `match` on a non-listening square and `window` on a
    non-wait: a word that belongs to one square, carried by another."""
    doc = _send_doc()
    doc["nodes"][0]["blocks"] = ["hook_line"]
    assert any(
        "blocks belongs to a call" in p for p in validate_definition(doc)
    ), validate_definition(doc)
