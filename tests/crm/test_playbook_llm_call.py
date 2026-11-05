"""playbook transform function `llm_call`: an LLM rewrites a fact's SPOKEN
text, configured through `params` like any built-in and awaited in the same
pipeline as every other one.

The model is never called in these tests — the registry entry is stubbed, so
they pin OUR contract: the publish laws, which facts are sent (only the
chosen lines', once each and all at once), that `when` reads the fact itself, that the
pipeline runs in order around the answer, and that every failure keeps the
original words.
"""

import asyncio
import copy
from typing import Any, Dict, List, Optional, Tuple

import pytest

import app.crm.outreach.nodes.blocks as blocks
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition
from app.utils.transformation import TEMPLATE_FUNCTION_REGISTRY

SHORT = "Only the first product's short name."
RAW = "Protection Plan, vivo S2 5G 8GB Blue"

_DOC: Dict[str, Any] = {
    "entry": {"topic": "OFFERED"},
    "nodes": [
        {"id": "call-1", "type": "call", "template_id": "tpl-1", "blocks": ["hook"]}
    ],
    "edges": [],
    "goals": [{"topics": ["ACTIVE"]}],
    "playbook": {
        "lines": {
            "hook_named": "aapka {product_name} cart me hai",
            "hook_plain": "aapka checkout adhura hai",
            "tenure": "{offers} months",
        },
        "blocks": {
            "hook": [
                {
                    "when": [{"field": "context.product_name", "op": "exists"}],
                    "say": "hook_named",
                },
                {"say": "hook_plain"},
            ],
            "tenure_line": [{"say": "tenure"}],
        },
        "transform": {
            "product_name": {"function": ["llm_call"], "params": {"prompt": SHORT}},
            "offers": {
                "function": ["llm_call", "string_to_lowercase"],
                "params": {"prompt": "only the numbers"},
            },
        },
    },
}


def _doc_with(fact: str, transform: Dict[str, Any]) -> Dict[str, Any]:
    doc = copy.deepcopy(_DOC)
    doc["playbook"]["transform"][fact] = transform
    return doc


def _definition(doc: Optional[Dict[str, Any]] = None) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(doc or _DOC)


def _run(context: Dict[str, Any]) -> EnrollmentRun:
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
        context=context,
    )


def _stub(
    monkeypatch: pytest.MonkeyPatch, answer: Optional[str]
) -> List[Tuple[str, str]]:
    seen: List[Tuple[str, str]] = []

    async def fake(value: Any, prompt: str) -> Any:
        seen.append((prompt, value))
        return answer

    monkeypatch.setitem(TEMPLATE_FUNCTION_REGISTRY, "llm_call", fake)
    return seen


def _render(block: str, context: Dict[str, Any]) -> Dict[str, str]:
    d = _definition()
    rendered, _ = asyncio.run(blocks.blocks_for(_run(context), d.nodes[0], d, [block]))
    return rendered


# --- publish laws -----------------------------------------------------------


def test_a_plan_with_llm_call_publishes() -> None:
    assert validate_definition(_DOC) == []


def test_llm_call_may_stand_anywhere_in_the_pipeline() -> None:
    doc = _doc_with(
        "product_name",
        {
            "function": ["string_trim", "llm_call", "string_to_lowercase"],
            "params": {"prompt": SHORT},
        },
    )
    assert validate_definition(doc) == []


def test_llm_call_needs_a_prompt() -> None:
    doc = _doc_with("product_name", {"function": ["llm_call"], "params": {}})
    assert any("needs 'prompt'" in p for p in validate_definition(doc))


def test_llm_call_is_the_registered_async_built_in() -> None:
    import inspect

    assert inspect.iscoroutinefunction(TEMPLATE_FUNCTION_REGISTRY["llm_call"])


def test_an_unknown_param_is_still_a_refused_typo() -> None:
    doc = _doc_with(
        "product_name",
        {"function": ["llm_call"], "params": {"prompt": SHORT, "promt": "x"}},
    )
    assert any("takes 'promt'" in p for p in validate_definition(doc))


@pytest.mark.parametrize("setting", ["model", "temperature", "endpoint"])
def test_llm_call_takes_only_a_prompt(setting: str) -> None:
    doc = _doc_with(
        "product_name",
        {"function": ["llm_call"], "params": {"prompt": SHORT, setting: "x"}},
    )
    assert any(f"takes {setting!r}" in p for p in validate_definition(doc))


# --- rendering --------------------------------------------------------------


def test_when_rows_read_the_fact_not_the_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This `when` names the RAW value; the rewrite changes only the words.
    doc = copy.deepcopy(_DOC)
    doc["playbook"]["blocks"]["hook"][0]["when"] = [
        {"field": "context.product_name", "op": "is", "value": RAW}
    ]
    _stub(monkeypatch, "vivo S2")
    d = _definition(doc)
    rendered, chosen = asyncio.run(
        blocks.blocks_for(_run({"product_name": RAW}), d.nodes[0], d, ["hook"])
    )
    assert chosen == {"hook": "hook_named"}
    assert rendered == {"hook": "aapka vivo S2 cart me hai"}


def test_only_the_chosen_lines_facts_are_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, "x")
    # hook's default row wins (no product_name): its line has no hole, so
    # `offers` — spelled only by another block — costs no model call.
    assert _render("hook", {"offers": "DMI: 3/6/9"}) == {
        "hook": "aapka checkout adhura hai"
    }
    assert seen == []


def test_a_fact_said_many_times_is_one_call_and_one_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = copy.deepcopy(_DOC)
    doc["playbook"]["lines"]["hook_named"] = "{product_name}, haan {product_name}"
    seen = _stub(monkeypatch, "vivo S2")
    d = _definition(doc)
    rendered, _ = asyncio.run(
        blocks.blocks_for(_run({"product_name": RAW}), d.nodes[0], d, ["hook"])
    )
    assert seen == [(SHORT, RAW)]
    assert rendered == {"hook": "vivo S2, haan vivo S2"}


def test_two_llm_facts_in_a_line_run_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = copy.deepcopy(_DOC)
    doc["playbook"]["lines"]["tenure"] = "{product_name}: {offers} months"
    doc["playbook"]["blocks"]["tenure_line"] = [{"say": "tenure"}]
    running: List[int] = []
    peak: List[int] = [0]

    async def slow(value: Any, prompt: str) -> Any:
        running.append(1)
        peak[0] = max(peak[0], len(running))
        await asyncio.sleep(0.01)
        running.pop()
        return "x"

    monkeypatch.setitem(TEMPLATE_FUNCTION_REGISTRY, "llm_call", slow)
    d = _definition(doc)
    asyncio.run(
        blocks.blocks_for(
            _run({"product_name": RAW, "offers": "3/6"}),
            d.nodes[0],
            d,
            ["tenure_line"],
        )
    )
    assert peak[0] == 2  # both facts were in flight together


def test_the_pipeline_runs_in_order_around_the_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _doc_with(
        "offers",
        {
            "function": ["string_trim", "llm_call", "string_to_lowercase"],
            "params": {"prompt": "only the numbers"},
        },
    )
    seen = _stub(monkeypatch, "Three, Six OR Nine")
    d = _definition(doc)
    rendered, _ = asyncio.run(
        blocks.blocks_for(
            _run({"offers": "  DMI: 3/6/9  "}), d.nodes[0], d, ["tenure_line"]
        )
    )
    assert seen == [("only the numbers", "DMI: 3/6/9")]  # trimmed BEFORE the LLM
    assert rendered == {"tenure_line": "three, six or nine months"}  # lowered AFTER


def test_the_seam_sends_only_the_value_and_uses_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub(monkeypatch, "vivo S2")
    rendered = _render("hook", {"product_name": RAW, "phone": "+919999999999"})
    assert seen == [(SHORT, RAW)]  # the value only — never the phone
    assert rendered == {"hook": "aapka vivo S2 cart me hai"}


def test_the_rest_of_the_pipeline_runs_on_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub(monkeypatch, "Three, Six OR Nine")
    rendered = _render("tenure_line", {"offers": "DMI: 3/6/9 months STANDARD_EMI"})
    assert seen == [("only the numbers", "DMI: 3/6/9 months STANDARD_EMI")]
    assert rendered == {"tenure_line": "three, six or nine months"}


@pytest.mark.parametrize("answer", ["", "   "])
def test_a_failed_rewrite_keeps_the_original_words(
    monkeypatch: pytest.MonkeyPatch, answer: Optional[str]
) -> None:
    _stub(monkeypatch, answer)
    assert _render("hook", {"product_name": RAW}) == {
        "hook": f"aapka {RAW} cart me hai"
    }


def test_a_missing_fact_parks_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub(monkeypatch, "x")
    with pytest.raises(NodeParked):
        _render("tenure_line", {"offers": None})
    assert seen == []
