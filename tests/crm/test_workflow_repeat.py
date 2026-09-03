"""Repeat entries — debounce + refresh (modules/05-outreach §Repeat entries,
sealed 31 Aug 2026): the vocabulary, the pure decide, the one UPDATE's
laws, and the validator's two entry rules."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import pytest

import app.crm.outreach.entry as entry
from app.crm.outreach import repeat
from app.crm.outreach.db.queries import patch_open_run_query
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.repeat import parse_repeat_policy, repeat_plan
from app.crm.outreach.schemas import WorkflowDefinition, WorkflowEntryAt

_NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)


def _door(**entry_words: Any) -> Any:
    """The plan's one door (phase 15): its entry words + start square."""
    return _definition(**entry_words).entries[0]


def _with_door(
    definition: WorkflowDefinition,
) -> Tuple[WorkflowDefinition, WorkflowEntryAt]:
    return definition, definition.entries[0]


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
    plan = repeat_plan(_door(), {"cart_value": 800})
    assert plan.is_noop  # ignore + debounce 0 -> no statement at all


# --- the pure decide ---


def test_refresh_latest_always_takes_the_new_facts() -> None:
    plan = repeat_plan(_door(on_repeat="refresh_latest"), {"cart_value": 300})
    assert plan.patch == {"cart_value": 300} and plan.max_field is None
    assert not plan.accumulate and not plan.is_noop


def test_refresh_max_names_the_field_and_the_number() -> None:
    plan = repeat_plan(
        _door(on_repeat="refresh_max(cart_value)"), {"cart_value": "4500"}
    )
    assert plan.max_field == "cart_value" and plan.max_value == 4500.0
    assert plan.patch == {"cart_value": "4500"}


def test_refresh_max_with_a_non_numeric_value_never_wins() -> None:
    plan = repeat_plan(
        _door(on_repeat="refresh_max(cart_value)", debounce_minutes=10),
        {"cart_value": "n/a"},
    )
    assert plan.patch == {} and plan.max_field is None
    assert plan.debounce_minutes == 10 and not plan.is_noop  # alarm still slides


def test_accumulate_appends() -> None:
    plan = repeat_plan(_door(on_repeat="accumulate"), {"order_id": "B"})
    assert plan.accumulate and plan.patch == {"order_id": "B"}


def test_ignore_with_debounce_slides_without_touching_facts() -> None:
    plan = repeat_plan(_door(debounce_minutes=5), {"cart_value": 1})
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
    """In migrate mode (ADR 0023): under pin the open runs keep their own
    entry words and the guard does not apply."""
    live = _raw()["entry"]
    migrating = {**_raw(on_repeat="refresh_latest"), "on_publish": "migrate"}
    problems = validate_definition(
        migrating, occupied_nodes=["wait_10m"], live_entry=live
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
            "received_at": _NOW,
            "occurred_at": None,
            "payload": {
                "order_id": "ORD-1",
                "cart_value": 900,
                "customer_mobile_number": "+91",
            },
        },
    )()
    asyncio.run(entry._try_enrol(flow, *_with_door(definition), event, "cust-1"))
    ((merchant, wf, key, door, event_id, facts),) = calls
    assert (merchant, wf, key, event_id) == ("m1", "wf-1", "ORD-1", "ev-9")
    assert facts["cart_value"] == 900
    assert (door.on_repeat, door.start) == ("refresh_latest", "wait_10m")


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
    event = type(
        "E",
        (),
        {
            "id": "ev-1",
            "merchant_id": "m1",
            "received_at": _NOW,
            "occurred_at": None,
            "payload": {},
        },
    )()
    asyncio.run(
        entry._try_enrol(
            flow, *_with_door(_definition(on_repeat="accumulate")), event, "c"
        )
    )
    assert calls == []


# --- the five guards folded in when #1041 was carried (rollout phase 00:
# P9 founding-event redelivery, P10 debounce only extends, N18 non-finite
# refresh_max, N19 bookkeeping keys never planted from a payload, N20 the
# refreshed phone reaches the run) ---


def test_patch_never_takes_the_runs_own_founding_event() -> None:
    """P9: a redelivered copy of the founding event is refused by
    source_event_used, falls into apply_repeat, and is NOT yet in
    repeat_event_ids — without this predicate it would overwrite newer
    facts with the first snapshot and restart the alarm."""
    sql, params = patch_open_run_query(
        "m1", "wf-1", "c-1", "w", "ev-1", {"cart_value": 1}, False, None, None, 10.0
    )
    assert "context->>'source_event_id' IS DISTINCT FROM $5::text" in sql
    assert params[4] == "ev-1"


def test_debounce_only_ever_extends_the_alarm() -> None:
    """P10: now() + N pulls the alarm EARLIER when the debounce is shorter
    than the remaining entry wait; a debounce may only extend the window."""
    sql, _ = patch_open_run_query(
        "m1", "wf-1", "c-1", "w", "ev-1", {}, False, None, None, 10.0
    )
    assert "GREATEST(wake_at, now() + make_interval(secs => $10::float8 * 60))" in sql
    assert "THEN now() + make_interval" not in sql


def test_refresh_max_ignores_non_finite_numbers() -> None:
    """N18: Postgres orders NaN above everything, so a junk "nan"/"inf"
    value would always win refresh_max."""
    for junk in ("nan", "inf", "-inf", "NaN", "Infinity", float("nan"), float("inf")):
        assert repeat._as_number(junk) is None, junk
    assert repeat._as_number("4500") == 4500.0 and repeat._as_number(12) == 12.0
    plan = repeat_plan(
        _door(on_repeat="refresh_max(cart_value)"), {"cart_value": "inf"}
    )
    assert plan.patch == {} and plan.max_field is None


def test_a_payload_can_never_plant_bookkeeping_keys_in_context() -> None:
    """N19: repeat_items, source_event_id, lead_*/message_*/reply_* are the
    walker's own; a producer key with one of those names would corrupt the
    accumulate branch (jsonb_array_length on a scalar) or the founding-event
    dedupe. The filter is nodes.py's one definition, not a second list."""
    context = entry._context_from_payload(
        {
            "item": "tv",
            "repeat_items": "not-a-list",
            "repeat_event_ids": "ev-0",
            "source_event_id": "forged",
            "lead_call": "l-1",
            "message_wa": "m-1",
            "reply_ask": "YES",
        }
    )
    assert context == {"item": "tv"}


def test_the_repeat_carries_the_refreshed_phone_but_never_the_founding_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N20: the repeat gets the same context enrol() was offered — the
    normalized phone included, so a corrected number reaches the run —
    minus source_event_id, which P9 needs to stay the founding event's."""
    calls: List[Any] = []

    async def refused_enrol(**kwargs: Any) -> None:
        return None

    async def fake_apply(*args: Any) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(entry, "enrol", refused_enrol)
    monkeypatch.setattr(entry, "apply_repeat", fake_apply)
    flow = type("F", (), {"id": "wf-1"})()
    event = type(
        "E",
        (),
        {
            "id": "ev-9",
            "merchant_id": "m1",
            "received_at": _NOW,
            "occurred_at": None,
            "payload": {"customer_mobile_number": "9876543210", "cart_value": 900},
        },
    )()
    asyncio.run(
        entry._try_enrol(
            flow, *_with_door(_definition(on_repeat="refresh_latest")), event, "c"
        )
    )
    ((_, _, _, _, _, facts),) = calls
    assert facts["phone"] == "+919876543210"
    assert facts["cart_value"] == 900
    assert "source_event_id" not in facts
