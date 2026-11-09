"""Segments phase 4 — the SQL parity pin (design/event-catalog.md §SQL
parity: "parity pin test when the segment compiler exists"). The compiled
SQL runs against Postgres BESIDE the Python evaluator under the workflow
door's own lookup (outreach/entry.py, _where_matches) over one grid of
payloads × conditions, and the two must agree on every pair. Then the
rule-level meaning: windows, at_least, not, the universe, the page, the
count.

Runs the shipped builders verbatim against a TEMP crm_event_raw that
shadows the real table for the session (the 069 test's pattern):

  CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
      tests/crm/test_segment_parity.py

Unset, it skips (tests/crm/conftest.py)."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import asyncpg
import pytest

from app.crm.outreach.entry import _where_matches
from app.crm.outreach.schemas import WorkflowEntry
from app.crm.record.contracts import RawEvent
from app.crm.record.db.queries_segments import (
    segment_count_query,
    segment_members_query,
)
from app.crm.record.segments import _narrow
from app.crm.shared.predicate import Condition
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the DB-backed parity test"
)

NOW = datetime.now(timezone.utc)

# Every shape a field can hold: absent, null, text, numbers as numbers and
# as text, booleans, lists (empty, of nulls, of values, nested), objects,
# and paths that cross arrays.
PAYLOADS: List[Dict[str, Any]] = [
    {},
    {"s": None},
    {"s": "7"},
    {"s": 7},
    {"s": 7.0},
    {"s": "7.0"},
    {"s": " 7 "},
    {"s": "abc"},
    {"s": True},
    {"s": 1},
    {"s": []},
    {"s": [None]},
    {"s": ["7"]},
    {"s": {"k": 1}},
    {"s": {}},
    {"n": 1850},
    {"n": 1850.0},
    {"n": "1850.00"},
    {"n": " 1850 "},
    {"n": "+1850"},
    {"n": "1e3"},
    {"n": ".5"},
    {"n": True},
    {"n": None},
    {"n": 4.99},
    {"n": "5"},
    {"n": 5},
    {"n": -3},
    {"n": [5]},
    {"n": "abc"},
    {"n": "\t5\n"},
    {"n": "\u0663"},  # an Arabic-Indic 3: a digit to Python's \\d, not to [0-9]
    {"n": "\u00a05"},  # a no-break space: whitespace to Python's strip()
    {"n": "5\u2003"},  # an em space
    {"tags": ["vip", "new"]},
    {"tags": "vip"},
    {"tags": []},
    {"tags": [None]},
    {"tags": ["VIP"]},
    {"tags": [7, "7"]},
    {"tags": [["vip"]]},
    {"apps": [{"lender": "FINNABLE"}, {"lender": "OTHER"}]},
    {"apps": [{"lender": None}]},
    {"apps": [{}]},
    {"apps": []},
    {"apps": {"lender": "FINNABLE"}},
    {"apps": [{"lender": ["FINNABLE"]}]},
    {"apps": ["x"]},
    {"obj": {"x": "7"}},
    {"obj": {"x": None}},
    {"obj": "flat"},
    {"s": "Galaxy S24 Ultra"},
    {"s": "GALAXY"},
    {"tags": ["Galaxy S24", "iPhone 15"]},
    {"apps": [{"lender": "finnable credit"}, {"lender": 7}]},
]

FIELDS = [
    "payload.s",
    "payload.n",
    "payload.tags",
    "payload.apps.lender",
    "payload.obj.x",
]
_VALUES = ["7", 7, True, "vip", "FINNABLE"]

CONDITIONS: List[Dict[str, Any]] = [
    *({"field": f, "op": op} for f in FIELDS for op in ("exists", "not_exists")),
    *({"field": f, "op": "is", "value": v} for f in FIELDS for v in _VALUES),
    *({"field": f, "op": "is_not", "value": v} for f in FIELDS for v in _VALUES),
    *(
        {"field": f, "op": "in", "value": vs}
        for f in FIELDS
        for vs in (["7", "abc"], [7], [True, "FINNABLE"])
    ),
    *({"field": f, "op": "includes", "value": v} for f in FIELDS for v in _VALUES),
    *(
        {"field": f, "op": op, "value": v}
        for f in FIELDS
        for op, v in (
            ("=", 7),
            ("=", "7.0"),
            ("=", 1850),
            ("=", "1850.00"),
            (">", 5),
            (">", 1849.5),
            (">=", 7),
            ("<", 5),
            ("<=", "1850"),
        )
    ),
    *(
        {"field": f, "op": "contains", "value": pieces}
        for f in FIELDS
        for pieces in (["gal"], ["S24", "zzz"], ["7"], ["VIP"], ["Finn"])
    ),
]


async def _connect() -> asyncpg.Connection:
    conn = await asyncpg.connect(DSN)
    await conn.execute("""
        CREATE TEMP TABLE crm_event_raw (
            merchant_id text NOT NULL,
            customer_id uuid,
            topic       text NOT NULL,
            payload     jsonb NOT NULL,
            occurred_at timestamptz,
            received_at timestamptz NOT NULL DEFAULT now()
        )
        """)
    return conn


async def _insert(
    conn: asyncpg.Connection,
    merchant_id: str,
    customer_id: Optional[str],
    topic: str,
    payload: Dict[str, Any],
    occurred_at: Optional[datetime] = NOW,
    received_at: datetime = NOW,
) -> None:
    await conn.execute(
        "INSERT INTO crm_event_raw VALUES ($1, $2::uuid, $3, $4::jsonb, $5, $6)",
        merchant_id,
        customer_id,
        topic,
        json.dumps(payload),
        occurred_at,
        received_at,
    )


async def _members(
    conn: asyncpg.Connection,
    merchant_id: str,
    rule: Dict[str, Any],
    after: Optional[str] = None,
    limit: int = 1000,
) -> List[str]:
    sql, params = segment_members_query(merchant_id, rule, _narrow(rule), after, limit)
    return [str(r["customer_id"]) for r in await conn.fetch(sql, *params)]


async def _count(
    conn: asyncpg.Connection, merchant_id: str, rule: Dict[str, Any]
) -> int:
    sql, params = segment_count_query(merchant_id, rule, _narrow(rule))
    return int(await conn.fetchval(sql, *params) or 0)


def _door_says(condition: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    door = WorkflowEntry(topic="T", where=[Condition.model_validate(condition)])
    event = RawEvent(
        id="e",
        merchant_id="parity",
        source="parity",
        topic="T",
        schema_version="1",
        external_id="x",
        payload=payload,
        received_at=NOW,
    )
    return _where_matches(door, event)


async def test_the_sql_and_the_door_agree_on_every_payload_and_condition() -> None:
    conn = await _connect()
    try:
        owner: Dict[str, int] = {}
        for index, payload in enumerate(PAYLOADS):
            customer = str(uuid.UUID(int=index + 1))
            owner[customer] = index
            await _insert(conn, "parity", customer, "T", payload)
        disagreements: List[Tuple[Dict[str, Any], Dict[str, Any], bool]] = []
        for condition in CONDITIONS:
            matched: Set[str] = set(
                await _members(conn, "parity", {"event": "T", "where": [condition]})
            )
            for customer, index in owner.items():
                payload = PAYLOADS[index]
                door = _door_says(condition, payload)
                if door != (customer in matched):
                    disagreements.append((condition, payload, door))
        assert not disagreements, "\n".join(
            f"{c} on {p}: door={d}, sql={not d}" for c, p, d in disagreements
        )
    finally:
        await conn.close()


async def test_offered_not_granted_counts_people_not_rows() -> None:
    conn = await _connect()
    try:
        ravi, asha, meera, other = (str(uuid.UUID(int=n)) for n in (1, 2, 3, 4))
        await _insert(conn, "fk", ravi, "OFFERED", {"amount": 60000})
        await _insert(conn, "fk", ravi, "GRANTED", {})
        await _insert(conn, "fk", asha, "OFFERED", {"amount": 60000})
        await _insert(conn, "fk", asha, "OFFERED", {"amount": 70000})
        await _insert(conn, "fk", meera, "GRANTED", {})
        await _insert(conn, "other", other, "OFFERED", {"amount": 60000})
        await _insert(conn, "fk", None, "OFFERED", {"amount": 60000})  # unstamped
        rule = {
            "all": [
                {
                    "event": "OFFERED",
                    "within_days": 7,
                    "where": [{"field": "payload.amount", "op": ">", "value": 50000}],
                },
                {"not": {"event": "GRANTED"}},
            ]
        }
        assert await _members(conn, "fk", rule) == [asha]
        assert await _count(conn, "fk", rule) == 1
    finally:
        await conn.close()


async def test_windows_count_occurred_at_and_fall_back_to_received_at() -> None:
    conn = await _connect()
    try:
        recent, old, late, unclaimed = (str(uuid.UUID(int=n)) for n in (1, 2, 3, 4))
        await _insert(conn, "m", recent, "A", {}, NOW - timedelta(days=3))
        await _insert(conn, "m", old, "A", {}, NOW - timedelta(days=10))
        # Sent today about something ten days old: occurred_at decides.
        await _insert(conn, "m", late, "A", {}, NOW - timedelta(days=10), NOW)
        # No occurred_at: received_at stands in.
        await _insert(conn, "m", unclaimed, "A", {}, None, NOW - timedelta(days=1))
        members = await _members(conn, "m", {"event": "A", "within_days": 7})
        assert sorted(members) == sorted([recent, unclaimed])
    finally:
        await conn.close()


async def test_narrowing_by_topic_and_window_changes_no_answer() -> None:
    """The narrowed read (each condition's topic, inside its window by
    received_at) answers exactly what reading every row answers. Rows keep
    the insert's clamp: occurred_at never later than received_at."""
    conn = await _connect()
    try:
        days = (0, 2, 5, 9, 40)
        for n, (topic, ago) in enumerate(
            (t, d) for t in ("A", "B", "C") for d in days for _ in range(2)
        ):
            person = str(uuid.UUID(int=n % 11 + 1))
            received = NOW - timedelta(days=ago)
            occurred = None if n % 3 == 0 else received - timedelta(days=n % 4)
            await _insert(conn, "m", person, topic, {"i": n}, occurred, received)
        # Two people every rule below can find: a recent A and nothing else;
        # B twice inside ten days and C only long ago.
        recent_a, b_twice = str(uuid.UUID(int=90)), str(uuid.UUID(int=91))
        await _insert(conn, "m", recent_a, "A", {}, None, NOW - timedelta(days=1))
        for _ in range(2):
            await _insert(conn, "m", b_twice, "B", {}, None, NOW - timedelta(days=5))
        await _insert(conn, "m", b_twice, "C", {}, None, NOW - timedelta(days=40))
        rules = [
            {"event": "A", "within_days": 3},
            {"any": [{"event": "A", "within_days": 3}, {"event": "A", "at_least": 4}]},
            {"all": [{"event": "A", "within_days": 7}, {"not": {"event": "B"}}]},
            {
                "all": [
                    {"event": "B", "within_days": 10, "at_least": 2},
                    {"not": {"event": "C", "within_days": 3}},
                ]
            },
            {
                "any": [
                    {"event": "A", "within_days": 1},
                    {"event": "B", "within_days": 30},
                ]
            },
        ]
        for rule in rules:
            assert _narrow(rule), rule
            narrowed = await _members(conn, "m", rule)
            sql, params = segment_members_query("m", rule, False, None, 1000)
            every_row = [str(r["customer_id"]) for r in await conn.fetch(sql, *params)]
            assert narrowed == every_row, rule
            assert narrowed, rule  # a rule that matched nobody proves nothing
    finally:
        await conn.close()


async def test_at_least_counts_matching_events_only() -> None:
    conn = await _connect()
    try:
        twice, once = (str(uuid.UUID(int=n)) for n in (1, 2))
        await _insert(conn, "m", twice, "A", {"ok": True})
        await _insert(conn, "m", twice, "A", {"ok": True})
        await _insert(conn, "m", once, "A", {"ok": True})
        await _insert(conn, "m", once, "A", {"ok": False})
        rule = {
            "event": "A",
            "at_least": 2,
            "where": [{"field": "payload.ok", "op": "is", "value": True}],
        }
        assert await _members(conn, "m", rule) == [twice]
    finally:
        await conn.close()


async def test_a_rule_that_needs_no_event_reads_everyone_the_merchant_knows() -> None:
    conn = await _connect()
    try:
        ordered, browsed, other = (str(uuid.UUID(int=n)) for n in (1, 2, 3))
        await _insert(conn, "m", ordered, "orders/create", {})
        await _insert(conn, "m", browsed, "checkouts/create", {})
        await _insert(conn, "x", other, "checkouts/create", {})
        never = {"not": {"event": "orders/create"}}
        assert await _members(conn, "m", never) == [browsed]
        either = {"any": [{"event": "orders/create"}, never]}
        assert sorted(await _members(conn, "m", either)) == sorted([ordered, browsed])
    finally:
        await conn.close()


async def test_pages_walk_the_whole_answer_once() -> None:
    conn = await _connect()
    try:
        people = [str(uuid.UUID(int=n)) for n in range(1, 8)]
        for person in people:
            await _insert(conn, "m", person, "A", {})
        seen: List[str] = []
        after: Optional[str] = None
        while True:
            page = await _members(conn, "m", {"event": "A"}, after, 3)
            seen.extend(page)
            if len(page) < 3:
                break
            after = page[-1]
        assert seen == sorted(people)
        assert await _count(conn, "m", {"event": "A"}) == len(people)
    finally:
        await conn.close()
