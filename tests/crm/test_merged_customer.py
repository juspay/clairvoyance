"""A merged customer is read as ONE person (canon/01-identity.md: "staple,
never melt" — reads across a pair, `id = $2 OR merged_into_id = $2`).

The story every test uses: Arjun's record c-11 (the older, the survivor)
and Dev's record c-22 (merged into c-11). Everything stamped before the
merge — runs, letters — still carries c-22. What is proven here:
- identity: the staple path-compresses (what the loser absorbed follows it
  to the survivor), the person's records are read across the pair, and a
  customer never merged is just herself;
- outreach: the admission guards judge every record of the person — her
  history, her cooldown, a run still open on her other record — and the
  repeat of an unkeyed plan follows the run, not the letter;
- record: the walker's goal check and the timeline read every record, and a
  segment groups a merged-away record's letters under the survivor;
- a merchant nobody was merged in gets exactly what it got before.

No database: tests/crm/test_segment_parity.py runs the merged segment SQL
on Postgres, and the PR's end-to-end run proves the rest on a real one.
"""

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, cast
from uuid import uuid4

import pytest

import app.crm.identity.db.accessor as identity_accessor
import app.crm.outreach.enrol as enrol_mod
import app.crm.outreach.entry as entry_mod
import app.crm.outreach.runs as runs_mod
import app.crm.record.db.accessor as record_accessor
import app.crm.record.segments as record_segments
from app.crm.identity.contracts import merged_pairs, person_records
from app.crm.identity.db.queries import (
    merge_customer_query,
    merged_pairs_query,
    person_records_query,
)
from app.crm.outreach.entry import _open_run_key
from app.crm.outreach.schemas import Workflow, WorkflowDefinition
from app.crm.record.contracts import (
    RawEvent,
    customer_has_event,
    get_customer_journey,
)
from app.crm.record.db.queries import customer_has_event_query
from app.crm.record.db.queries_segments import (
    segment_count_query,
    segment_members_query,
)

SURVIVOR, MERGED = "c-11", "c-22"  # Arjun (older), Dev (merged into Arjun)
NOW = datetime(2026, 10, 18, 10, 20, tzinfo=timezone.utc)


def _placeholders(sql: str) -> set:
    return {int(n) for n in re.findall(r"\$(\d+)", sql)}


@pytest.fixture
def one_person(monkeypatch: pytest.MonkeyPatch) -> None:
    """identity: c-11 and c-22 are one person, c-11 the survivor."""

    async def records(merchant_id: str, customer_id: str) -> List[str]:
        return [SURVIVOR, MERGED] if customer_id in (SURVIVOR, MERGED) else []

    async def pairs(merchant_id: str) -> Dict[str, str]:
        return {MERGED: SURVIVOR}

    monkeypatch.setattr(identity_accessor, "person_records", records)
    monkeypatch.setattr(identity_accessor, "merged_pairs", pairs)


# --- identity ---


def test_the_staple_re_points_what_the_loser_had_absorbed() -> None:
    """One hop (canon T05 col 20): L stapled into c-22 earlier, c-22 into
    c-11 now -> L points at c-11 in the same statement, never L -> c-22 ->
    c-11. Only when the staple really happened: a racing no-op re-points
    nothing."""
    sql, params = merge_customer_query(MERGED, SURVIVOR)
    assert "SET status = 'merged_away', merged_into_id = $2, merged_at = now()" in sql
    assert "WHERE id = $1 AND status = 'active'" in sql and "RETURNING id" in sql
    assert re.search(r"SET merged_into_id = \$2\s+FROM stapled", sql)
    assert "WHERE c.merged_into_id = stapled.id" in sql
    assert params == [MERGED, SURVIVOR]


def test_the_person_is_read_across_the_pair_merchant_first() -> None:
    sql, params = person_records_query("m1", MERGED)
    assert "WHERE me.merchant_id = $1 AND me.id = $2" in sql
    assert "c.merchant_id = me.merchant_id" in sql
    assert "c.id = COALESCE(me.merged_into_id, me.id)" in sql
    assert "c.merged_into_id = COALESCE(me.merged_into_id, me.id)" in sql
    assert "ORDER BY c.merged_into_id IS NOT NULL, c.id" in sql  # survivor first
    assert params == ["m1", MERGED]


def test_merged_pairs_read_only_the_merged_rows() -> None:
    sql, params = merged_pairs_query("m1")
    assert "WHERE merchant_id = $1 AND merged_into_id IS NOT NULL" in sql
    assert params == ["m1"]


async def test_a_customer_never_merged_is_herself() -> None:
    # conftest: identity knows no merges — the accessor finds nothing.
    assert await person_records("m1", "c-5") == ["c-5"]
    assert await merged_pairs("m1") == {}


async def test_a_merged_customer_is_every_record_survivor_first(one_person) -> None:
    assert await person_records("m1", MERGED) == [SURVIVOR, MERGED]
    assert await person_records("m1", SURVIVOR) == [SURVIVOR, MERGED]


# --- outreach: the admission guards ---


def _workflow(reenter: bool) -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="Festive offer call",
        status="live",
        version=1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition={
            "entry": {"topic": "OFFERED", "reenter": reenter},
            "nodes": [{"id": "rest", "type": "wait", "minutes": 30}],
            "edges": [],
            "goal": {"topics": ["ORDER_PLACED"]},
        },
        draft=None,
    )


class _Runs:
    """The enrollment accessor slice _enrol_in_txn touches."""

    def __init__(self, facts: Dict[str, Any]) -> None:
        self.facts = facts
        self.asked: Dict[str, List[str]] = {}
        self.inserted: List[str] = []

    async def source_event_used(
        self, conn: Any, merchant_id: str, workflow_id: str, ids: List[str], sid: str
    ) -> bool:
        self.asked["source_event_used"] = ids
        return False

    async def admission_facts(
        self,
        conn: Any,
        merchant_id: str,
        workflow_id: str,
        ids: List[str],
        enrollment_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.asked["admission_facts"] = ids
        return self.facts

    async def lock_templates_shared(self, *args: Any) -> None:
        return None

    async def insert_enrollment(self, conn: Any, merchant_id: str, *args: Any) -> Any:
        self.inserted.append(args[2])  # the customer the run is for
        return SimpleNamespace(id=uuid4(), current_node="rest")


@pytest.fixture
def guards(monkeypatch: pytest.MonkeyPatch):
    def _install(facts: Dict[str, Any]) -> SimpleNamespace:
        runs = _Runs(facts)
        refused: List[str] = []

        async def _atomically(fn: Any, *args: Any) -> Any:
            return await fn(object(), *args)

        monkeypatch.setattr(enrol_mod, "atomically", _atomically)
        monkeypatch.setattr(enrol_mod, "enrollment_accessor", runs)
        monkeypatch.setattr(enrol_mod, "version_accessor", runs)
        monkeypatch.setattr(
            enrol_mod,
            "_log_skipped",
            lambda merchant_id, workflow_id, reason, **kw: refused.append(reason),
        )
        return SimpleNamespace(runs=runs, refused=refused)

    return _install


async def _enrol(customer_id: str, reenter: bool, **kw: Any) -> Any:
    return await enrol_mod.enrol(
        merchant_id="m1",
        workflow=_workflow(reenter),
        customer_id=customer_id,
        context={"source_event_id": "e-9"},
        **kw,
    )


async def test_the_guards_judge_every_record_of_the_person(guards, one_person) -> None:
    """Gap 1: c-22 ran this plan yesterday; after the merge c-11 is offered
    it again. Her history is on c-22 — the guard must read it."""
    g = guards({"runs": 1, "latest_entered_at": NOW, "open_runs": 0})
    assert await _enrol(SURVIVOR, reenter=False) is None
    assert g.runs.asked["admission_facts"] == [SURVIVOR, MERGED]
    assert g.runs.asked["source_event_used"] == [SURVIVOR, MERGED]  # replays too
    assert g.refused == ["reenter_disabled"] and g.runs.inserted == []


async def test_a_run_open_on_her_other_record_refuses_a_second(
    guards, one_person
) -> None:
    """Gap 2: re-entry allowed and the cooldown long past, but c-22's run is
    still open — the per-key unique cannot see it (the key is the record's
    id), so the guard does: no second live run for one person."""
    g = guards(
        {"runs": 1, "latest_entered_at": NOW - timedelta(days=3), "open_runs": 1}
    )
    assert await _enrol(SURVIVOR, reenter=True) is None
    assert g.refused == ["open_run_exists"] and g.runs.inserted == []


async def test_a_customer_never_merged_is_judged_exactly_as_before(guards) -> None:
    g = guards({"runs": 0, "latest_entered_at": None, "open_runs": 0})
    run = await _enrol("c-5", reenter=False)
    assert run is not None and g.runs.inserted == ["c-5"]
    assert g.runs.asked["admission_facts"] == ["c-5"]  # her one record


async def test_the_callers_records_are_used_without_asking_again(
    guards, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event door already read the person for her open runs: enrol()
    takes them, one identity read per letter."""

    async def never(*args: Any) -> List[str]:
        raise AssertionError("identity asked twice")

    monkeypatch.setattr(identity_accessor, "person_records", never)
    g = guards({"runs": 0, "latest_entered_at": None, "open_runs": 0})
    await _enrol(SURVIVOR, reenter=False, person=[SURVIVOR, MERGED])
    assert g.runs.asked["admission_facts"] == [SURVIVOR, MERGED]


def test_an_unkeyed_repeat_follows_her_open_run_not_the_letter() -> None:
    """The event door's repeat: the open run's key is the record it started
    on (c-22); the letter carries c-11. The repeat must patch c-22's
    run. Never merged, the key is her own id — exactly as before."""
    flow = cast(Any, SimpleNamespace(id="wf-1"))
    person = [SURVIVOR, MERGED]
    runs = cast(
        Any,
        [
            SimpleNamespace(workflow_id="wf-0", enrollment_key="x"),
            SimpleNamespace(workflow_id="wf-1", enrollment_key=MERGED),
        ],
    )
    assert _open_run_key(runs, flow, person) == MERGED
    assert _open_run_key([], flow, person) is None


def test_a_run_keyed_by_an_order_is_never_taken_for_an_unkeyed_repeat() -> None:
    """Zero merges, unchanged prod: a plan keyed in an older version still has
    a run open under key ORD-1. An unkeyed letter's repeat must not patch it —
    exactly what happened before this change (it looked for her id, found
    nothing)."""
    flow = cast(Any, SimpleNamespace(id="wf-1"))
    keyed_run = cast(Any, [SimpleNamespace(workflow_id="wf-1", enrollment_key="ORD-1")])
    assert _open_run_key(keyed_run, flow, ["c-5"]) is None


async def test_the_event_door_reads_her_open_runs_on_every_record(
    one_person, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 6: her order arrives stamped c-11; c-22's run is waiting for it.
    The door must read the open runs of both records, or the goal never
    ends that run and it keeps calling someone who already ordered."""
    asked: List[Any] = []

    async def open_runs(merchant_id: str, ids: List[str]) -> list:
        asked.append(ids)
        return []

    async def no_flows(merchant_id: str) -> list:
        return []

    monkeypatch.setattr(
        entry_mod,
        "enrollment_accessor",
        SimpleNamespace(open_runs_for_customer=open_runs),
    )
    monkeypatch.setattr(
        entry_mod, "workflow_accessor", SimpleNamespace(live_workflows=no_flows)
    )
    event = RawEvent(
        id="e-6",
        merchant_id="m1",
        source="e2e",
        topic="ORDER_PLACED",
        schema_version="1",
        external_id="o-6",
        payload={},
        received_at=NOW,
    )
    await entry_mod.consume_attributed_event(event, SURVIVOR)
    assert asked == [[SURVIVOR, MERGED]]


async def test_a_refused_unkeyed_enrol_repeats_onto_her_other_records_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repeat of an unkeyed plan is found by key, and unkeyed the key is
    the record a run started on. A letter for c-11 refused because c-22's
    run is open must patch THAT run (key c-22), not look for a c-11 one."""
    repeated: List[Any] = []

    async def refused(**kwargs: Any) -> None:
        return None

    async def apply(*args: Any) -> bool:
        repeated.append(args[2])  # the key the patch looks the run up by
        return True

    async def no_pin(run: Any) -> None:
        return None

    monkeypatch.setattr(entry_mod, "enrol", refused)
    monkeypatch.setattr(entry_mod, "apply_repeat", apply)
    monkeypatch.setattr(entry_mod, "definition_for", no_pin)
    monkeypatch.setattr(entry_mod, "CRM_CONTEXT_VALUE_MAX_CHARS", _ceiling)
    definition = WorkflowDefinition.model_validate(
        {
            "entry": {"topic": "REMINDER", "reenter": True},
            "nodes": [{"id": "rest", "type": "wait", "minutes": 30}],
            "edges": [],
            "goal": {"topics": ["ORDER_PLACED"]},
        }
    )
    flow = SimpleNamespace(id="wf-1")
    event = RawEvent(
        id="e-4",
        merchant_id="m1",
        source="e2e",
        topic="REMINDER",
        schema_version="1",
        external_id="r-4",
        payload={},
        received_at=NOW,
    )
    her_open_run = SimpleNamespace(
        id=uuid4(), workflow_id="wf-1", enrollment_key=MERGED, current_node="rest"
    )
    await entry_mod._try_enrol(
        flow,  # type: ignore[arg-type]
        definition,
        definition.entries[0],
        event,
        SURVIVOR,
        open_runs=[her_open_run],  # type: ignore[list-item]
        person=[SURVIVOR, MERGED],
    )
    assert repeated == [MERGED]


async def _ceiling() -> int:
    return 1000


# --- record: the goal check, the timeline, the segment ---


async def test_the_goal_check_hears_a_letter_on_any_of_her_records(
    one_person, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 3: c-22's run waits for ORDER_PLACED; the order is stamped c-11.
    The walker asks about the run's customer (c-22); record reads both."""
    asked: List[Any] = []

    async def has_event(merchant_id: str, ids: List[str], *args: Any) -> bool:
        asked.append(ids)
        return True

    monkeypatch.setattr(record_accessor, "customer_has_event", has_event)
    assert await customer_has_event("m1", MERGED, ["ORDER_PLACED"], NOW)
    assert asked == [[SURVIVOR, MERGED]]
    sql, params = customer_has_event_query("m1", [SURVIVOR, MERGED], ["X"], NOW)
    assert "AND customer_id = ANY($2::uuid[])" in sql
    assert params[1] == [SURVIVOR, MERGED]


async def test_her_timeline_reads_every_record(
    one_person, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: List[Any] = []

    async def journey(merchant_id: str, ids: List[str], *args: Any) -> list:
        asked.append(ids)
        return []

    monkeypatch.setattr(record_accessor, "get_customer_journey", journey)
    await get_customer_journey("m1", SURVIVOR)
    assert asked == [[SURVIVOR, MERGED]]


async def test_her_runs_in_the_console_read_every_record(
    one_person, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: List[Any] = []

    async def customer_runs(merchant_id: str, ids: List[str], limit: int) -> list:
        asked.append(ids)
        return []

    monkeypatch.setattr(
        runs_mod, "enrollment_accessor", SimpleNamespace(customer_runs=customer_runs)
    )
    await runs_mod.customer_runs("m1", SURVIVOR, 50)
    assert asked == [[SURVIVOR, MERGED]]


OFFERED_NOT_GRANTED = {"all": [{"event": "OFFERED"}, {"not": {"event": "GRANTED"}}]}


def test_a_segment_groups_a_merged_records_letters_under_the_survivor() -> None:
    """Gap 4: OFFERED on c-22 before the merge, GRANTED on c-11 after. Grouped
    by record, c-22 is "offered, not granted"; grouped by person, nobody is."""
    merged = {MERGED: SURVIVOR}
    sql, params = segment_members_query(
        "m1", OFFERED_NOT_GRANTED, True, "c-0", 10, merged
    )
    assert (
        "LEFT JOIN unnest($2::uuid[], $3::uuid[]) AS merged(loser, survivor) "
        "ON merged.loser = customer_id" in sql
    )
    assert params[1:3] == [[MERGED], [SURVIVOR]]  # in step
    assert "SELECT COALESCE(merged.survivor, customer_id) AS customer_id" in sql
    assert "GROUP BY COALESCE(merged.survivor, customer_id)" in sql
    assert "AND COALESCE(merged.survivor, customer_id) > $4::uuid" in sql  # keyset
    assert _placeholders(sql) == set(range(1, len(params) + 1))
    count, cparams = segment_count_query("m1", OFFERED_NOT_GRANTED, True, merged)
    assert "GROUP BY COALESCE(merged.survivor, customer_id)" in count
    assert _placeholders(count) == set(range(1, len(cparams) + 1))


def test_a_merchant_with_no_merges_gets_the_same_sql_as_before() -> None:
    for merged in (None, {}):
        sql, params = segment_members_query(
            "m1", OFFERED_NOT_GRANTED, True, "c-0", 10, merged
        )
        assert "unnest(" not in sql and "merged." not in sql
        assert "SELECT customer_id\n" in sql and "GROUP BY customer_id" in sql
        assert "AND customer_id > $2::uuid" in sql and params[1] == "c-0"


async def test_records_segment_answer_brings_identitys_merges(
    one_person, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: List[Any] = []

    async def members(*args: Any) -> List[str]:
        seen.append(args[-1])
        return []

    async def count(*args: Any) -> int:
        seen.append(args[-1])
        return 0

    monkeypatch.setattr(record_accessor, "customers_matching", members)
    monkeypatch.setattr(record_accessor, "count_matching", count)
    await record_segments.customers_matching("m1", OFFERED_NOT_GRANTED)
    await record_segments.count_matching("m1", OFFERED_NOT_GRANTED)
    assert seen == [{MERGED: SURVIVOR}, {MERGED: SURVIVOR}]
