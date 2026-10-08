"""An agent's outcome words, read from its template: the options any eval
about the call's outcome chooses from."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.services.evals.shared.utils.extract_agent_outcomes import (
    extract_agent_outcomes,
)

EXAMPLES = Path("app/ai/voice/agents/breeze_buddy/examples/templates")


def _template(flow: Dict[str, Any], observers: Optional[List[Any]] = None) -> Any:
    return SimpleNamespace(
        flow=flow, configurations=SimpleNamespace(observers=observers)
    )


def _example(path: Path) -> Any:
    data = json.loads(path.read_text())
    return _template(data["flow"])


def _hook(outcome: Optional[Dict[str, Any]], **fields: Any) -> Dict[str, Any]:
    expected = {"outcome": outcome} if outcome is not None else {}
    return {
        "name": "update_outcome_in_database",
        "expected_fields": {**expected, **fields},
    }


def _function(name: str, hooks: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    return {"name": name, "description": f"when {name}", "hooks": hooks, **extra}


def _static(word: str) -> Dict[str, Any]:
    return {"source": "static", "value": word}


def _flow(*functions: Dict[str, Any]) -> Dict[str, Any]:
    return {"nodes": [{"node_name": "main", "functions": list(functions)}]}


# ---------------------------------------------------------------------------
# the agent's outcome words
# ---------------------------------------------------------------------------


def test_words_of_the_example_templates():
    assert list(
        extract_agent_outcomes(_example(EXAMPLES / "order-confirmation.json")) or {}
    ) == [
        "BUSY",
        "CONFIRM",
        "CANCEL",
        "ADDRESS_UPDATED",
    ]
    ivr = _example(Path("examples/templates/order-confirmation-ivr.json"))
    assert list(extract_agent_outcomes(ivr) or {}) == [
        "CONFIRMED",
        "CANCEL_STARTED",
        "ADDRESS_CHANGE_REQUESTED",
        "NO_RESPONSE",
        "CANCELLED",
    ]


def test_fixed_words_with_their_functions_descriptions():
    flow = _flow(
        _function("confirm", [_hook(_static("CONFIRM"))]),
        _function("yes", [_hook(_static("confirm"))]),  # the same word
        _function("cancel", [_hook(_static("CANCEL"), reason={"source": "llm"})]),
        _function("note", [_hook(None, reason={"source": "llm"})]),  # no outcome
        _function("lookup", [{"name": "send_http_request"}]),
    )

    assert extract_agent_outcomes(_template(flow)) == {
        "CONFIRM": "when confirm; when yes",
        "CANCEL": "when cancel",
    }


@pytest.mark.parametrize(
    "outcome, properties, words",
    [
        # the LLM picks from declared values
        (
            {"source": "llm"},
            {"outcome": {"type": "string", "enum": ["PAID", "LATER"]}},
            ["PAID", "LATER"],
        ),
        # ... or writes it freely: the template is unreadable
        ({"source": "llm"}, {"outcome": {"type": "string"}}, None),
        ({"source": "computed", "value": "now"}, {}, None),
    ],
)
def test_words_the_llm_fills_in(outcome, properties, words):
    flow = _flow(_function("set", [_hook(outcome)], properties=properties))

    found = extract_agent_outcomes(_template(flow))

    assert (None if found is None else list(found)) == words


@pytest.mark.parametrize(
    "properties, words",
    [
        ({"outcome": {"enum": ["DONE"]}}, ["DONE"]),
        ({"outcome": {"type": "string"}}, None),
        ({"reason": {"type": "string"}}, []),  # the function takes no outcome
    ],
)
def test_a_hook_without_expected_fields_writes_the_arguments(properties, words):
    hook = {"name": "update_outcome_in_database"}
    flow = _flow(_function("set", [hook], properties=properties))

    found = extract_agent_outcomes(_template(flow))

    assert (None if found is None else list(found)) == words


@pytest.mark.parametrize(
    "outcome, words",
    [
        ({"type": "string", "enum": ["A", "B"]}, ["A", "B"]),
        ({"type": "string"}, None),
    ],
)
def test_the_update_outcome_builtin(outcome, words):
    flow = {
        "nodes": [],
        "global_functions": [
            {
                "type": "builtin",
                "handler": "update_outcome",
                "name": "set_outcome",
                "description": "d",
                "properties": {"outcome": outcome},
            },
            {"type": "builtin", "handler": "end_conversation", "name": "end"},
        ],
    }

    found = extract_agent_outcomes(_template(flow))

    assert (None if found is None else list(found)) == words


def test_direct_mode_functions():
    flow = {
        "functions": [
            _function("done", [_hook(_static("DONE"))]),
            {"type": "builtin", "handler": "end_conversation", "name": "end"},
        ]
    }

    assert list(extract_agent_outcomes(_template(flow)) or {}) == ["DONE"]


def test_ivr_options_timeouts_and_option_hooks():
    flow = {
        "mode": "ivr",
        "initial_node": "menu",
        "nodes": {
            "menu": {
                "options": [
                    {"digit": "1", "action": "end", "outcome": "YES", "label": "Yes"},
                    {
                        "digit": "2",
                        "action": "end",
                        "label": "No",
                        "hooks": [_hook(_static("NO"))],
                    },
                ],
                "on_timeout_outcome": "SILENT",
            }
        },
    }

    assert extract_agent_outcomes(_template(flow)) == {
        "YES": "Yes",
        "NO": "No",
        "SILENT": "No answer at menu.",
    }


def test_enabled_observers_only():
    def observer(name: str, outcome: str, enabled: bool) -> Any:
        action = SimpleNamespace(args={"outcome": outcome})
        return SimpleNamespace(name=name, enabled=enabled, action=action)

    template = _template(
        {"nodes": []},
        observers=[
            observer("voicemail", "VOICEMAIL_DETECTED", True),
            observer("off", "NEVER", False),
        ],
    )

    assert list(extract_agent_outcomes(template) or {}) == ["VOICEMAIL_DETECTED"]


def test_a_template_without_outcome_words():
    assert extract_agent_outcomes(_template({"nodes": []})) == {}
    assert extract_agent_outcomes(SimpleNamespace(flow=None, configurations=None)) == {}
