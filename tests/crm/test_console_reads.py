"""The console's reads (18 Sep 2026): the runs page with its filters and
"Run 3 of 3", the summary's per-day series and open squares, a run's events
and calls, a plan's call totals, a version's document, the publish dry-run,
and the catalog's daily counts and latest letter. Pure builders and folds
here; the routes are exercised through a TestClient with the accessors
faked, so nothing reaches a database."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.auth import MERCHANT_SCOPE_MARK
from app.crm.outreach import analytics, api as outreach_api, plans, runs
from app.crm.outreach.db.decoders.enrollment import (
    decode_day_counts,
    decode_node_counts,
    decode_run_row,
    decode_run_summary,
)
from app.crm.outreach.db.queries.enrollment import (
    count_runs_query,
    list_runs_query,
    open_by_node_query,
    run_endings_in_window_query,
)
from app.crm.outreach.schemas import (
    EnrollmentRun,
    RunEnding,
    RunRow,
    RunStep,
)
from app.crm.record.db.queries import event_topics_query
from app.database.queries.breeze_buddy.lead_call_tracker import (
    get_call_facts_by_runs_query,
    get_call_stats_by_runs_query,
    get_leads_by_enrollment_id_query,
)

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


def _run(**over: Any) -> EnrollmentRun:
    base = dict(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=3,
        customer_id=uuid4(),
        status="waiting",
        current_node="listen",
        wake_at=NOW,
        entered_at=NOW,
        exited_at=None,
        exit_reason=None,
        context={},
        enrollment_key="k1",
        attempts=0,
        last_error=None,
    )
    base.update(over)
    return EnrollmentRun(**base)


# --- the runs page ------------------------------------------------------------


def test_run_numbers_are_counted_before_any_filter_narrows_the_page() -> None:
    """ "Run 3 of 3" is over every run the plan holds for the key: the
    numbering CTE is bound only by merchant and plan, and every filter
    applies after it — so filtering never renumbers a run."""
    sql, _ = list_runs_query("m1", "wf", "exited", 10, 0, node="listen", version=2)
    page, numbering = sql.split("SELECT p.*", 1)
    for bound in ("status = $3", "current_node = $4", "workflow_version = $5"):
        assert bound in page and bound not in numbering
    # the two counts are keyed by merchant, plan and the row's own key, and
    # run over the page's rows only — never a window over the whole plan
    assert numbering.count("k.enrollment_key = p.enrollment_key") == 2
    assert "(k.entered_at, k.id) <= (p.entered_at, p.id)" in numbering
    assert "OVER (PARTITION BY" not in sql
    assert "count(*) OVER () AS total" in page
    assert "LIMIT $13 OFFSET $14" in page


def test_pages_anchor_to_the_newest_row_the_reader_first_saw() -> None:
    """Keyset, not offset alone: with an anchor every page reads only rows
    at or before (entered_at, id) — new runs entering meanwhile cannot
    shift the pages underneath the reader. Without one, no bound."""
    sql, params = list_runs_query(
        "m1", "wf", None, 10, 20, anchor_entered_at=T0, anchor_id="a-id"
    )
    assert "(entered_at, id) <= ($11::timestamptz, $12::uuid)" in sql
    assert params[10:] == [T0, "a-id", 10, 20]
    _, bare = list_runs_query("m1", "wf", None, 10, 0)
    assert bare[10:] == [None, None, 10, 0]


def test_an_empty_page_is_counted_rather_than_reported_as_nothing() -> None:
    """count(*) OVER () rides on rows; an offset past the last match has
    none, so the accessor counts with the SAME filters and no page."""
    page_sql, page_params = list_runs_query("m1", "wf", "parked", 10, 90, node="x")
    count_sql, count_params = count_runs_query("m1", "wf", "parked", node="x")
    assert "LIMIT" not in count_sql and "count(*)::int AS total" in count_sql
    for bound in ("status = $3", "current_node = $4", "$11::timestamptz IS NULL"):
        assert bound in page_sql and bound in count_sql
    # the count binds exactly the page's first twelve values: no padding
    assert count_params == page_params[:12] and len(page_params) == 14
    assert "LIMIT $13 OFFSET $14" in page_sql


@pytest.mark.asyncio
async def test_the_accessor_counts_when_the_page_is_empty(monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from app.crm.outreach.db.accessors import enrollment as acc

    class Conn:
        async def fetch(self, query, *values):
            return []

        async def fetchval(self, query, *values):
            assert "count(*)::int AS total" in query
            return 46

    @asynccontextmanager
    async def fake_connection():
        yield Conn()

    monkeypatch.setattr(acc, "crm_connection", fake_connection)
    rows, total = await acc.list_runs("m1", "wf", None, 10, 900)
    assert rows == [] and total == 46


def test_the_search_is_literal_and_phone_digits_need_four() -> None:
    _, params = list_runs_query("m1", "wf", None, 10, 0, search="50%_off")
    assert params[6] == "%50\\%\\_off%"  # LIKE metacharacters escaped
    assert params[7] is None  # "50" is two digits: no phone probe
    _, params = list_runs_query("m1", "wf", None, 10, 0, search="+91 98765")
    assert params[7] == "%9198765%"


def test_open_squares_count_waiting_and_parked_only() -> None:
    """Open = not exited (the CHECK's other two values), spelled exactly as
    the open-runs partial index (075) so the planner can use it."""
    sql, _ = open_by_node_query("m1", "wf")
    assert "status <> 'exited'" in sql and "GROUP BY current_node" in sql


def test_the_run_row_carries_its_place_among_its_keys_runs() -> None:
    run = _run()
    row = {**run.model_dump(), "run_number": 2, "runs_for_key": 3}
    decoded = decode_run_row(row)
    assert (decoded.run_number, decoded.runs_for_key) == (2, 3)
    assert decode_day_counts([{"day": date(2026, 9, 17), "runs": 4}])[0].runs == 4
    assert decode_node_counts([{"current_node": "listen", "runs": 7}]) == {"listen": 7}


# --- a run's calls --------------------------------------------------


def test_lead_reads_are_scoped_to_the_merchant_and_production_calls() -> None:
    sql, params = get_leads_by_enrollment_id_query("m1", "run-1", T0, None)
    assert '"merchant_id" = $2' in sql and params == ["run-1", "m1", T0, None]
    # a retry lead is what the lead store minted it as: unstamped, a re-dial
    # (attempt_count > 0), carrying a request_id one of the run's OWN stamped
    # leads carries, inside the run's lifetime — nothing re-derived at read
    # time, so an unkeyed plan's "wf-<run>" request_id is found, and a
    # merchant's /push/lead/v2 row (attempt 0) with the same order id is not
    assert 'l."merchant_id" = $2 AND l."enrollment_id" = $1' in sql
    assert 'l."enrollment_id" IS NULL AND l."attempt_count" > 0' in sql
    # an EQUALITY on the run's own stamped request_ids, never a correlated
    # subquery (quadratic in runs on prod, 21 Sep 2026)
    assert 'SELECT DISTINCT "request_id" FROM "lead_call_tracker"' in sql
    assert 'WHERE "merchant_id" = $2 AND "enrollment_id" = $1) p' in sql
    assert 'l."request_id" = p.request_id' in sql
    assert (
        'l."created_at" >= $3 AND l."created_at" <= COALESCE($4::timestamptz, now())'
        in sql
    )
    assert "request_id = $" not in sql  # no caller-supplied request id at all


def test_the_report_and_the_calls_summary_fold_the_same_lead_set() -> None:
    """Both reads start from the one CTE: each run's stamped leads UNION ALL
    its retries, production modes only, merchant-scoped on both halves —
    so a retry answered after a NO_ANSWER counts as reached in BOTH."""
    facts_sql, facts_params = get_call_facts_by_runs_query("m1", ["a"], [T0], [None])
    stats_sql, stats_params = get_call_stats_by_runs_query("m1", ["a"], [T0], [None])
    assert facts_params == stats_params == ["m1", ["a"], [T0], [None]]
    for sql in (facts_sql, stats_sql):
        assert 'JOIN "lead_call_tracker" l ON l."enrollment_id" = r.id' in sql
        assert 'l."enrollment_id" IS NULL AND l."attempt_count" > 0' in sql
        # equality on the stamped lead's request_id carried through `s`
        # (one row per run × request_id); the correlated IN it replaces
        # was quadratic in runs and held prod's CPU on 21 Sep 2026
        assert 'l."request_id" = s.request_id' in sql
        assert 'SELECT DISTINCT run_id, entered_at, exited_at, "request_id"' in sql
        assert "s2.run_id = s.run_id" not in sql
        assert 'l."created_at" <= COALESCE(s.exited_at, now())' in sql
        assert sql.count('l."merchant_id" = $1') == 2
        assert sql.count("'TELEPHONY', 'HOLD_TRANSFER'") == 2
        assert "UNION ALL" in sql and "FROM mine" in sql
    # the one answered definition: the count, the moment, and the stats
    # rows' own `spoke` column
    answered = "'NO_ANSWER', 'NUMBER_UNAVAILABLE', 'FAILED'"
    assert facts_sql.count(answered) == 2 and stats_sql.count(answered) == 1
    assert ") AS spoke" in stats_sql and "GROUP BY 1, 2, 3" in stats_sql


def test_the_call_summary_counts_spoken_and_answered_apart() -> None:
    summary = analytics.summarize_calls(
        {
            "outcomes": [
                {
                    "outcome": "INTERESTED",
                    "spoke": True,
                    "calls": 3,
                    "runs": 3,
                    "talk_seconds": 300,
                    "timed_calls": 3,
                    "attempts": 3,
                    "cost": 6.0,
                },
                {
                    "outcome": "BUSY",
                    "spoke": True,  # a picked-up line (24 Sep 2026)
                    "calls": 2,
                    "runs": 2,
                    "talk_seconds": 20,
                    "timed_calls": 1,
                    "attempts": 4,
                    "cost": 1.0,
                },
                {
                    "outcome": "NO_ANSWER",
                    "calls": 5,
                    "runs": 4,
                    "talk_seconds": None,
                    "timed_calls": 0,
                    "attempts": 5,
                    "cost": None,
                },
            ],
            "contacted": 6,
            "reached": 5,
        }
    )
    assert (summary.placed, summary.connected, summary.answered) == (10, 5, 5)
    assert summary.by_outcome == {"INTERESTED": 3, "BUSY": 2, "NO_ANSWER": 5}
    assert summary.talk_seconds_avg == 80.0  # 320 s over 4 timed calls
    assert summary.attempts_avg == 1.2
    assert (summary.contacted_runs, summary.reached_runs, summary.cost_total) == (
        6,
        5,
        7.0,
    )
    empty = analytics.summarize_calls({"outcomes": [], "contacted": 0, "reached": 0})
    assert (
        empty.placed == 0
        and empty.talk_seconds_avg is None
        and empty.cost_total is None
    )


# --- the catalog ------------------------------------------------------------------


def _client(merchants=("m1",)) -> TestClient:
    app = FastAPI()
    app.include_router(outreach_api.router, prefix="/workflows")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="user",
        username="u",
        merchant_ids=list(merchants),
        reseller_ids=[],
        email=None,
    )
    return TestClient(app)


def test_every_workflow_route_still_declares_the_tenancy_door() -> None:
    routes = [r for r in outreach_api.router.routes if isinstance(r, APIRoute)]
    missing = [
        r.path
        for r in routes
        if not any(
            getattr(d.call, MERCHANT_SCOPE_MARK, False)
            for d in r.dependant.dependencies
        )
    ]
    assert missing == []


def test_the_runs_page_is_an_envelope_with_its_total_and_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The total and the anchor travel in the BODY, typed: page 1 gets the
    anchor the server took from its newest row, later pages echo it back
    and get it returned unchanged — no client ever reads the sort order."""
    seen: Dict[str, Any] = {}
    newest = RunRow(
        id="00000000-0000-0000-0000-00000000aaaa",
        merchant_id="m1",
        workflow_id="00000000-0000-0000-0000-0000000000f1",
        workflow_version=1,
        customer_id="00000000-0000-0000-0000-0000000000c1",
        status="waiting",
        current_node="listen",
        wake_at=None,
        entered_at=T0,
        exited_at=None,
        exit_reason=None,
        context={},
        enrollment_key="c1",
        attempts=0,
        last_error=None,
        node_arrived_at=None,
    )

    async def fake_list(merchant_id, workflow_id, status, limit, offset, *filters):
        seen["args"] = (merchant_id, status, limit, offset, filters)
        return ([newest] if offset == 0 else []), 46

    monkeypatch.setattr(outreach_api.runs, "list_runs", fake_list)
    r = _client().get(
        "/workflows/wf/runs",
        params={
            "merchant_id": "m1",
            "status": "exited",
            "node": "listen",
            "version": 2,
            "q": "ravi",
            "limit": 10,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 46 and [x["id"] for x in body["items"]] == [str(newest.id)]
    assert body["anchor"] == {
        "entered_at": "2026-09-19T12:00:00Z",
        "id": str(newest.id),
    }
    assert "X-Total-Count" not in r.headers
    assert seen["args"][:4] == ("m1", "exited", 10, 0)
    assert seen["args"][4][:4] == ("listen", 2, None, "ravi")
    # page 2 echoes the anchor and gets it back, with the true total
    r2 = _client().get(
        "/workflows/wf/runs",
        params={
            "merchant_id": "m1",
            "limit": 10,
            "offset": 10,
            "anchor_entered_at": body["anchor"]["entered_at"],
            "anchor_id": body["anchor"]["id"],
        },
    )
    assert r2.status_code == 200
    assert r2.json() == {"items": [], "total": 46, "anchor": body["anchor"]}
    assert seen["args"][4][-1] == (T0, str(newest.id))


def test_a_foreign_run_is_a_404_for_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def none(*args):
        return None

    monkeypatch.setattr(outreach_api.runs, "run_calls", none)
    client = _client()
    assert (
        client.get(
            "/workflows/wf/runs/r1/calls", params={"merchant_id": "m1"}
        ).status_code
        == 404
    )


def test_the_summary_refuses_an_unknown_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(*args):
        raise AssertionError("summary read with a bad tz")

    monkeypatch.setattr(outreach_api.analytics, "workflow_summary", never)
    r = _client().get(
        "/workflows/wf/summary", params={"merchant_id": "m1", "tz": "Mars/Base"}
    )
    assert r.status_code == 422 and "Mars/Base" in r.text


def test_the_publish_check_says_404_for_an_unknown_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(merchant_id: str, workflow_id: str) -> Optional[Any]:
        return None

    monkeypatch.setattr(outreach_api.plans, "check_draft", missing)
    assert (
        _client()
        .post("/workflows/wf/validate", params={"merchant_id": "m1"})
        .status_code
        == 404
    )


@pytest.mark.asyncio
async def test_check_draft_never_opens_an_atom_for_an_unknown_plan(monkeypatch) -> None:
    async def no_plan(merchant_id: str, workflow_id: str):
        return None

    async def no_atom(*args, **kwargs):
        raise AssertionError("atom opened for a plan that does not exist")

    monkeypatch.setattr(plans.workflow_accessor, "get_workflow", no_plan)
    monkeypatch.setattr(plans, "atomically", no_atom)
    assert await plans.check_draft("m1", "wf") is None


@pytest.mark.asyncio
async def test_a_plan_with_no_draft_has_nothing_to_fix(monkeypatch) -> None:
    async def published(merchant_id: str, workflow_id: str):
        return SimpleNamespace(draft=None, definition={"entry": {"topic": "A"}})

    async def no_atom(*args, **kwargs):
        raise AssertionError("no draft: nothing to judge")

    monkeypatch.setattr(plans.workflow_accessor, "get_workflow", published)
    monkeypatch.setattr(plans, "atomically", no_atom)
    assert await plans.check_draft("m1", "wf") == (False, [])


@pytest.mark.asyncio
async def test_check_draft_reports_what_the_atom_saw_not_the_first_read(
    monkeypatch,
) -> None:
    """Between the pre-read and the atom another request may publish (the
    draft is gone) or delete the plan: the atom's snapshot is the answer,
    never coalesced into "has a clean draft"."""

    async def drafted(merchant_id: str, workflow_id: str):
        return SimpleNamespace(draft={"entry": {"topic": "A"}}, definition=None)

    async def no_catalogs(merchant_id: str, draft):
        return None

    monkeypatch.setattr(plans.workflow_accessor, "get_workflow", drafted)
    monkeypatch.setattr(plans, "_gather_catalogs", no_catalogs)
    for seen in (None, (False, []), (True, ["entry changed with open runs"])):

        async def atom(fn, *args, _seen=seen, **kwargs):
            return _seen

        monkeypatch.setattr(plans, "atomically", atom)
        assert await plans.check_draft("m1", "wf") == seen


# --- the report: the two tables Flipkart was shown on 19 Sep 2026 -----------
# 4,765 runs entered 09:00–21:00; 8,484 calls placed, 706 answered; 221 got
# the loan before we spoke to them, 11 after; 4,533 still open. The numbers
# below are that day scaled down, so the arithmetic is pinned to the post.

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _ending(
    i: int,
    status: str = "waiting",
    reason: Optional[str] = None,
    exited=None,
    node: Optional[str] = None,
):
    return RunEnding(f"r{i}", f"c{i}", status, reason, exited, T0, node)


def _fact(placed: int, answered: int = 0, first=None, template="nudge", **more):
    return [
        {
            "template": template,
            "leads": placed + more.pop("queued", 0),
            "placed": placed,
            "answered": answered,
            "no_answer": more.pop("no_answer", 0),
            "busy": more.pop("busy", 0),
            "in_progress": 0,
            "first_answered_at": first,
        }
    ]


def test_the_report_tells_before_from_after_by_time_alone() -> None:
    endings = [
        _ending(1, "exited", "goal_met", T0),  # never dialled
        _ending(2, "exited", "goal_met", T0),  # dialled, nobody spoke
        _ending(3, "exited", "goal_met", T0),  # spoke at T0-1h → after
        _ending(4, "exited", "goal_met", T0),  # spoke at T0+1h → before
        _ending(5, "exited", "withdrawn", T0),  # spoke first → after
        _ending(6, "exited", "ejected", T0),
        _ending(7, "exited", "timed_out", T0),
        _ending(8, node="gap-30m"),  # still walking, dialled three times
    ]
    facts = {
        "r2": _fact(2, no_answer=2),
        "r3": _fact(1, 1, T0 - timedelta(hours=1)),
        "r4": _fact(1, 1, T0 + timedelta(hours=1)),
        "r5": _fact(2, 1, T0 - timedelta(minutes=5), busy=1),
        "r8": _fact(3, 2, T0, queued=1, no_answer=1),
    }
    r = analytics.build_report(endings, facts)
    c, k = r.customers, r.calls
    assert (c.runs, c.unique_customers) == (8, 8)
    assert (c.goal_met_before_reach, c.goal_met_after_reach) == (3, 1)
    assert (c.withdrawn_before_reach, c.withdrawn_after_reach) == (0, 1)
    assert (c.ejected, c.other_ended, c.open) == (1, 1, 1)
    # the seven buckets partition the runs
    assert (
        c.goal_met_before_reach
        + c.goal_met_after_reach
        + c.withdrawn_before_reach
        + c.withdrawn_after_reach
        + c.ejected
        + c.other_ended
        + c.open
    ) == c.runs
    # never dialled ×3 (r1, r6, r7), once ×2 (r3, r4), twice ×2 (r2, r5), thrice ×1 (r8)
    assert c.calls_per_customer == {"0": 3, "1": 2, "2": 2, "3": 1}
    assert c.reached == 4
    assert (k.leads, k.placed, k.answered, k.no_answer, k.busy) == (10, 9, 5, 3, 1)
    assert k.repeat_calls == 9 - 5 and k.repeat_answered == 5 - 4
    # one agent rang everyone, so its card IS the plan-wide table
    assert [t.template for t in r.by_template] == ["nudge"]
    assert r.by_template[0].calls == k and r.by_template[0].reached == c.reached
    # the same runs by stage: never dialled r1 r6 r7 · rung, nobody spoke
    # r2 r4 (r4 spoke only AFTER it ended) · spoke r3 r5 r8
    by = c.by_reach
    assert list(by) == ["never_dialled", "dialled_no_answer", "spoke"]
    assert [by[s].runs for s in by] == [3, 2, 3]
    assert (by["never_dialled"].goal_met, by["dialled_no_answer"].goal_met) == (1, 2)
    assert by["spoke"].goal_met == c.goal_met_after_reach == 1
    assert by["spoke"].withdrawn == c.withdrawn_after_reach == 1
    assert (by["never_dialled"].ejected, by["never_dialled"].other_ended) == (1, 1)
    assert by["spoke"].open == 1 and c.open_by_square == {"gap-30m": 1}
    # each stage partitions into its five endings, and the stages into runs
    for stage in by.values():
        assert (
            stage.goal_met
            + stage.withdrawn
            + stage.ejected
            + stage.other_ended
            + stage.open
        ) == stage.runs
    assert sum(stage.runs for stage in by.values()) == c.runs


def test_two_agents_fold_into_the_plan_wide_table() -> None:
    endings = [_ending(1), _ending(2)]
    facts = {
        "r1": _fact(2, 1, T0, template="hindi") + _fact(1, 0, template="english"),
        "r2": _fact(1, 1, T0, template="english"),
    }
    r = analytics.build_report(endings, facts)
    assert r.calls.placed == 4 and r.calls.answered == 2
    assert r.customers.calls_per_customer == {"1": 1, "3": 1}
    by = {t.template: t for t in r.by_template}
    assert by["hindi"].calls.placed == 2 and by["hindi"].reached == 1
    assert by["english"].calls.placed == 2 and by["english"].reached == 1
    assert by["english"].calls_per_customer == {"1": 2}
    assert r.by_template[0].template == "english"  # busiest first, tie by name
    assert "GROUP BY 1, 2" in get_call_facts_by_runs_query("m1", ["a"], [T0], [None])[0]


def test_a_run_that_re_entered_is_one_customer_twice() -> None:
    endings = [
        RunEnding("r1", "same", "waiting", None, None),
        RunEnding("r2", "same", "waiting", None, None),
    ]
    r = analytics.build_report(endings, {})
    assert (r.customers.runs, r.customers.unique_customers) == (2, 1)
    assert r.calls.placed == 0 and r.customers.calls_per_customer == {"0": 2}
    # a row from before 073 has no square to name; it is still counted
    assert r.customers.open_by_square == {"(none)": 2}
    assert r.customers.by_reach["never_dialled"].open == 2


def test_the_report_reads_are_windowed_on_entered_at_and_tenant_first() -> None:
    sql, params = run_endings_in_window_query("m1", "wf", T0, None)
    assert "entered_at >= $3" in sql and "exited_at" in sql and "enrollment_key" in sql
    assert "entered_at, current_node" in sql
    assert params == ["m1", "wf", T0, None]
    text, values = get_call_facts_by_runs_query("m1", ["a", "b"], [T0, T0], [None, T0])
    assert 'l."merchant_id" = $1' in text
    assert values == ["m1", ["a", "b"], [T0, T0], [None, T0]]
    assert 'min("call_initiated_time")' in text and "GROUP BY 1, 2" in text
    # one definition of answered, used for the count and the moment alike
    assert text.count("'NO_ANSWER', 'NUMBER_UNAVAILABLE', 'FAILED'") == 2


def test_a_run_is_staged_by_whether_anyone_spoke_before_it_ended() -> None:
    assert analytics._stage(0, False) == "never_dialled"
    assert analytics._stage(3, False) == "dialled_no_answer"
    assert analytics._stage(1, True) == "spoke"


def test_calls_fold_into_one_card_per_agent() -> None:
    summary = analytics.summarize_calls(
        {
            "outcomes": [
                {
                    "template": "nudge",
                    "outcome": "INTERESTED",
                    "spoke": True,
                    "calls": 30,
                    "timed_calls": 30,
                    "talk_seconds": 900,
                    "attempts": 30,
                    "cost": 15.0,
                },
                {
                    "template": "nudge",
                    "outcome": "NO_ANSWER",
                    "calls": 70,
                    "timed_calls": 0,
                    "talk_seconds": 0,
                    "attempts": 90,
                    "cost": 5.0,
                },
                {
                    "template": "nudge-generic",
                    "outcome": "INTERESTED",
                    "spoke": True,
                    "calls": 5,
                    "timed_calls": 5,
                    "talk_seconds": 100,
                    "attempts": 5,
                    "cost": 2.0,
                },
            ],
            "contacted": 90,
            "reached": 35,
        }
    )
    assert summary.placed == 105 and summary.connected == 35
    # Busiest agent first, and the cards add up to the plan-wide totals.
    assert [c.template for c in summary.by_template] == ["nudge", "nudge-generic"]
    assert sum(c.placed for c in summary.by_template) == summary.placed
    assert summary.by_template[0].by_outcome == {"INTERESTED": 30, "NO_ANSWER": 70}
    assert summary.by_template[0].talk_seconds_avg == 30.0
    # 120 attempts over 100 calls on that agent.
    assert summary.by_template[0].attempts_avg == 1.2


def test_a_lead_with_no_template_is_named_not_blank() -> None:
    summary = analytics.summarize_calls(
        {
            "outcomes": [{"template": "", "outcome": "N/A", "calls": 1}],
            "contacted": 1,
            "reached": 0,
        }
    )
    assert summary.by_template[0].template == "(no template)"


def test_calls_are_grouped_by_template_as_well_as_outcome() -> None:
    text, _ = get_call_stats_by_runs_query("m1", ["a"], [T0], [None])
    assert 'COALESCE("template"' in text and "GROUP BY 1, 2, 3" in text


def test_who_spoke_is_the_lead_stores_word_not_a_second_python_rule() -> None:
    """A plan whose calls all FAILED, or hit NUMBER_UNAVAILABLE: nobody
    spoke, nobody answered — the row's `spoke` decides, never a Python set
    that forgot two outcomes. BUSY is a picked-up line (24 Sep 2026): the
    store marks it spoke, and answered is connected, not spoke-plus-BUSY."""
    rows = [
        {"template": "t", "outcome": "FAILED", "spoke": False, "calls": 3},
        {"template": "t", "outcome": "NUMBER_UNAVAILABLE", "spoke": False, "calls": 2},
        {"template": "t", "outcome": "BUSY", "spoke": True, "calls": 4},
        {"template": "t", "outcome": "INTERESTED", "spoke": True, "calls": 1},
    ]
    s = analytics.summarize_calls({"outcomes": rows, "contacted": 5, "reached": 5})
    assert (s.placed, s.connected, s.answered) == (10, 5, 5)
    assert s.by_template[0].connected == 5


def test_the_window_is_bounded_and_defaults_to_the_last_day() -> None:
    start, end = analytics.bounded_window(None, T0)
    assert end == T0 and (end - start).days == analytics.DEFAULT_WINDOW_DAYS == 1
    with pytest.raises(ValueError):
        analytics.bounded_window(T0, T0)
    with pytest.raises(ValueError):
        analytics.bounded_window(T0 - timedelta(days=8), T0)
    assert analytics.bounded_window(T0 - timedelta(days=7), T0) == (
        T0 - timedelta(days=7),
        T0,
    )


def test_a_report_window_over_the_ceiling_is_a_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(*args):
        raise AssertionError("no read for a refused window")

    monkeypatch.setattr(
        outreach_api.analytics.enrollment_accessor, "run_endings_in_window", never
    )
    r = _client().get(
        "/workflows/wf/report",
        params={
            "merchant_id": "m1",
            "since": "2026-01-01T00:00:00Z",
            "until": "2026-09-01T00:00:00Z",
        },
    )
    assert r.status_code == 422 and "7 days" in r.text


def test_the_summary_is_composed_in_the_decoder_from_all_four_reads() -> None:
    from datetime import date

    s = decode_run_summary(
        [
            {
                "grouping_level": 3,
                "runs": 4,
                "median_minutes_to_exit": 5,
                "recovered_amount": None,
            }
        ],
        [],
        [{"day": date(2026, 9, 20), "runs": 4}],
        [{"current_node": "listen", "runs": 2}],
    )
    assert s.runs == 4 and s.runs_per_day[0].runs == 4
    assert s.open_by_node == {"listen": 2}


@pytest.mark.asyncio
async def test_a_report_for_a_plan_that_is_not_ours_is_nothing(monkeypatch) -> None:
    async def no_plan(merchant_id: str, workflow_id: str):
        return None

    monkeypatch.setattr(analytics.workflow_accessor, "get_workflow", no_plan)
    assert await analytics.workflow_report("m1", "wf", None, None) is None


# --- the trail names its letters ---------------------------------------------


def _step(node: str, arrived_by: str = "walk", **over) -> RunStep:
    base = dict(
        node=node, node_type="wait", arrived_at=NOW, left_at=NOW, arrived_by=arrived_by
    )
    base.update(over)
    return RunStep(**base)


@pytest.mark.asyncio
async def test_the_trail_names_the_letter_behind_each_row(monkeypatch) -> None:
    woke = uuid4()
    asked: Dict[str, Any] = {}

    async def topics(merchant_id: str, ids: List[str]) -> Dict[str, str]:
        asked["ids"] = ids
        return {"ev-door": "OFFERED", str(woke): "OFFER_SELECTED"}

    monkeypatch.setattr(runs, "event_topics", topics)
    run = _run(
        status="exited",
        exit_reason="goal_met",
        context={
            "source_event_id": "ev-door",
            "goal": {"topic": "GRANTED", "event_id": "x"},
        },
    )
    steps = [
        _step("rule", "door", outcome="yes", next_node="quiet"),
        _step(
            "quiet",
            "walk",
            cut_short_by=woke,
            outcome="OFFER_SELECTED",
            next_node="rule",
        ),
        _step("rule", "letter", outcome="yes", next_node="quiet"),
        _step("listen", "walk", outcome="goal_met", next_node=None),
    ]
    out = await runs.name_letters("m1", run, steps)
    assert [s.event_topic for s in out] == [
        "OFFERED",
        "OFFER_SELECTED",
        None,
        "GRANTED",
    ]
    # one read, the door's id and the waking letter's, nothing else
    assert sorted(asked["ids"]) == sorted(["ev-door", str(woke)])


@pytest.mark.asyncio
async def test_a_trail_with_no_letters_reads_nothing(monkeypatch) -> None:
    async def boom(*a):  # pragma: no cover - must not be called
        raise AssertionError("no ids, no read")

    monkeypatch.setattr(runs, "event_topics", boom)
    out = await runs.name_letters("m1", _run(), [_step("quiet", "timer")])
    assert out[0].event_topic is None


def test_event_topics_read_is_tenant_first_and_by_id() -> None:
    sql, params = event_topics_query("m1", ["a", "b"])
    assert "merchant_id = $1" in sql and "id = ANY($2::uuid[])" in sql
    assert params == ["m1", ["a", "b"]]
