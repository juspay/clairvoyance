"""Repeat entries — debounce + refresh (modules/05-outreach §Repeat entries,
sealed 31 Aug 2026): the vocabulary, the pure decide, the one UPDATE's
laws, and the validator's two entry rules."""

import asyncio
import json
from typing import Any, Dict, List

import pytest

import app.crm.outreach.entry as entry
from app.crm.outreach import repeat
from app.crm.outreach.db.queries import patch_open_run_query
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.repeat import parse_repeat_policy, repeat_plan
from app.crm.outreach.schemas import WorkflowDefinition


def _definition(**entry_words: Any) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "entry": {"topic": "checkouts/create", "reenter": True, **entry_words},
            "nodes": [
                {"id": "wait_10m", "type": "wait", "minutes": 10},
                {"id": "call", "type": "call", "template_id": "tpl-1"},
            ],
            "edges": [["wait_10m", "call"]],
            "goal": {"topics": ["orders/create"]},
        }
    )


# --- the words ---


def test_vocabulary_is_the_corpus_four() -> None:
    assert parse_repeat_policy("ignore") == ("ignore", None)
    assert parse_repeat_policy("refresh_latest") == ("refresh_latest", None)
    assert parse_repeat_policy("accumulate") == ("accumulate", None)
    assert parse_repeat_policy("refresh_max(cart_value)") == (
        "refresh_max",
        "cart_value",
    )
    assert parse_repeat_policy("refresh_max( total )") == ("refresh_max", "total")
    for bad in ("refresh_max", "refresh_max()", "latest", "", "refresh_max(a b)"):
        assert parse_repeat_policy(bad) is None, bad


def test_defaults_are_todays_behaviour() -> None:
    plan = repeat_plan(_definition(), {"cart_value": 800})
    assert plan.is_noop  # ignore + debounce 0 -> no statement at all


# --- the pure decide ---


def test_refresh_latest_always_takes_the_new_facts() -> None:
    plan = repeat_plan(_definition(on_repeat="refresh_latest"), {"cart_value": 300})
    assert plan.patch == {"cart_value": 300} and plan.max_field is None
    assert not plan.accumulate and not plan.is_noop


def test_refresh_max_names_the_field_and_the_number() -> None:
    plan = repeat_plan(
        _definition(on_repeat="refresh_max(cart_value)"), {"cart_value": "4500"}
    )
    assert plan.max_field == "cart_value" and plan.max_value == 4500.0
    assert plan.patch == {"cart_value": "4500"}


def test_refresh_max_with_a_non_numeric_value_never_wins() -> None:
    plan = repeat_plan(
        _definition(on_repeat="refresh_max(cart_value)", debounce_minutes=10),
        {"cart_value": "n/a"},
    )
    assert plan.patch == {} and plan.max_field is None
    assert plan.debounce_minutes == 10 and not plan.is_noop  # alarm still slides


def test_accumulate_appends() -> None:
    plan = repeat_plan(_definition(on_repeat="accumulate"), {"order_id": "B"})
    assert plan.accumulate and plan.patch == {"order_id": "B"}


def test_ignore_with_debounce_slides_without_touching_facts() -> None:
    plan = repeat_plan(_definition(debounce_minutes=5), {"cart_value": 1})
    assert plan.patch == {} and plan.debounce_minutes == 5 and not plan.is_noop


# --- the one UPDATE ---


def test_patch_touches_only_an_open_run_on_the_entry_square_by_key() -> None:
    sql, params = patch_open_run_query(
        "m1",
        "wf-1",
        "ORD-1",
        "wait_10m",
        "ev-1",
        {"cart_value": 4500},
        False,
        "cart_value",
        4500.0,
        10.0,
    )
    assert "status = 'waiting' AND current_node = $4" in sql
    assert "enrollment_key = $3" in sql and "customer_id" not in sql
    assert "NOT (COALESCE(context->'repeat_event_ids', '[]'::jsonb) ? $5::text)" in sql
    assert "make_interval(secs => $10::float8 * 60)" in sql
    assert params[0] == "m1" and json.loads(params[5]) == {"cart_value": 4500}
    assert params[4] == "ev-1" and params[7] == "cart_value" and params[9] == 10.0


def test_patch_marks_the_event_used_and_compares_in_the_statement() -> None:
    sql, _ = patch_open_run_query(
        "m1", "wf-1", "c-1", "w", "ev-1", {}, False, "cart_value", 1.0, 0.0
    )
    assert (
        "'repeat_event_ids'" in sql and "jsonb_build_array(to_jsonb($5::text))" in sql
    )
    assert "$9::float8 > COALESCE(" in sql and "'-Infinity'::float8" in sql
    assert "WHEN $7::boolean THEN" in sql and "'repeat_items'" in sql


# --- the validator ---


def _raw(**entry_words: Any) -> Dict[str, Any]:
    return _definition(**entry_words).model_dump()


def test_validator_refuses_a_word_outside_the_vocabulary() -> None:
    problems = validate_definition(_raw(on_repeat="latest"))
    assert problems and "not a policy" in problems[0]


def test_validator_accepts_every_corpus_word() -> None:
    for word in ("ignore", "refresh_latest", "refresh_max(cart_value)", "accumulate"):
        assert validate_definition(_raw(on_repeat=word)) == [], word


def test_debounce_needs_a_wait_as_the_first_node() -> None:
    raw = _raw(debounce_minutes=10)
    raw["nodes"] = [raw["nodes"][1], raw["nodes"][0]]
    raw["edges"] = [["call", "wait_10m"]]
    problems = validate_definition(raw)
    assert any("entry alarm to slide" in p for p in problems)
    assert validate_definition(_raw(debounce_minutes=10)) == []


def test_changing_repeat_words_mid_flight_is_blocked_by_the_entry_guard() -> None:
    live = _raw()["entry"]
    problems = validate_definition(
        _raw(on_repeat="refresh_latest"), occupied_nodes=["wait_10m"], live_entry=live
    )
    assert any("entry rule changed" in p for p in problems)


# --- the processor hook ---


def test_a_refused_enrol_hands_the_repeat_to_apply_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []

    async def refused_enrol(**kwargs: Any) -> None:
        return None

    async def fake_apply(*args: Any) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(entry, "enrol", refused_enrol)
    monkeypatch.setattr(entry, "apply_repeat", fake_apply)
    flow = type("F", (), {"id": "wf-1"})()
    definition = _definition(on_repeat="refresh_latest", key="order_id")
    event = type(
        "E",
        (),
        {
            "id": "ev-9",
            "merchant_id": "m1",
            "payload": {
                "order_id": "ORD-1",
                "cart_value": 900,
                "customer_mobile_number": "+91",
            },
        },
    )()
    asyncio.run(entry._try_enrol(flow, definition, event, "cust-1"))
    ((merchant, wf, key, defn, event_id, facts),) = calls
    assert (merchant, wf, key, event_id) == ("m1", "wf-1", "ORD-1", "ev-9")
    assert facts["cart_value"] == 900 and defn is definition


def test_a_successful_enrol_never_calls_apply_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Any] = []

    async def new_run(**kwargs: Any) -> object:
        return object()

    async def fake_apply(*args: Any) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(entry, "enrol", new_run)
    monkeypatch.setattr(entry, "apply_repeat", fake_apply)
    flow = type("F", (), {"id": "wf-1"})()
    event = type("E", (), {"id": "ev-1", "merchant_id": "m1", "payload": {}})()
    asyncio.run(entry._try_enrol(flow, _definition(on_repeat="accumulate"), event, "c"))
    assert calls == []
