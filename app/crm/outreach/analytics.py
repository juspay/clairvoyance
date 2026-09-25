"""Analytics over a plan's runs (the console's Performance tab and the
Runs tab's cards): the windowed summary, the calls summary, and the day
report — every read here is over runs that ENTERED a window of entered_at,
and every number is a fold the reader can re-derive from the rows.

Kept apart from runs.py on purpose: runs.py is the run OPERATIONS surface
(list, resume, a run's trail and calls, retention) that workers and the
walker's neighbours cross through; nothing here is on any run's path.
Three reads, each the narrowest one that answers its own question, then
PURE folds (summarize_calls, build_report) that a test can pin without a
database. api.py delegates here; db/ is reached only through the
accessors, never directly.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.schemas import (
    REACH_STAGES,
    ReportCallBar,
    ReportCalls,
    ReportCustomers,
    ReportReach,
    ReportTemplate,
    RunEnding,
    TemplateCalls,
    WorkflowCallSummary,
    WorkflowReport,
    WorkflowRunSummary,
)
from app.database.accessor import get_call_facts_by_runs, get_call_stats_by_runs

# The widest window a report or calls summary will materialise, and the
# window a caller who names none gets. Both reads pull every run that
# entered the window into memory (RunEnding tuples, then parallel arrays
# bound into the lead CTE), so the window is bounded by what one pooled
# connection can carry: Flipkart enters 15k–31k runs a DAY (21 Sep 2026),
# so a day is the default and a week the ceiling until the fold moves
# into SQL. A wider ask is a 422, never a silent clip.
MAX_WINDOW_DAYS = 7
DEFAULT_WINDOW_DAYS = 1


def bounded_window(
    since: Optional[datetime], until: Optional[datetime]
) -> Tuple[datetime, datetime]:
    """PURE: the half-open [since, until) a report actually reads. No
    ``until`` means now; no ``since`` means DEFAULT_WINDOW_DAYS before it;
    a span over MAX_WINDOW_DAYS, or one that ends before it starts, is
    refused (ValueError → the route's 422), never silently clipped —
    the reader asked for a window and gets exactly it or a reason."""
    end = until or datetime.now(timezone.utc)
    start = since or end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if start >= end:
        raise ValueError("since must be before until")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise ValueError(f"the window may span at most {MAX_WINDOW_DAYS} days")
    return start, end


def _lifetimes(
    endings: Sequence[RunEnding],
) -> List[Tuple[str, datetime, Optional[datetime]]]:
    """(id, entered_at, exited_at) per run — what the lead store needs to
    find each run's stamped leads and their retries."""
    return [(e.id, e.entered_at or _EPOCH, e.exited_at) for e in endings]


async def workflow_summary(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
    tz: str = "Asia/Kolkata",
) -> WorkflowRunSummary:
    """The plan's report over a window of entered_at (rollout phase 09),
    with its runs per day in ``tz`` and where its open runs stand now."""
    return await enrollment_accessor.workflow_summary(
        merchant_id, workflow_id, since, until, tz
    )


async def workflow_call_summary(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> WorkflowCallSummary:
    """The calls placed by the plan's runs that entered in the window —
    stamped leads and their retries, the same set the report folds, so the
    two reads can never disagree on a run. Two lead reads over that one
    set: the per-(template, outcome) rows for the table, and the per-run
    facts for how many runs were contacted and reached (the report's own
    ``reached``). Raises ValueError on an unbounded or inverted window."""
    since, until = bounded_window(since, until)
    endings = await enrollment_accessor.run_endings_in_window(
        merchant_id, workflow_id, since, until
    )
    runs = _lifetimes(endings)
    rows = await get_call_stats_by_runs(merchant_id, runs)
    facts = await get_call_facts_by_runs(merchant_id, runs)
    contacted = reached = 0
    for per_template in facts.values():
        if sum(int(r.get("placed") or 0) for r in per_template) >= 1:
            contacted += 1
        if sum(int(r.get("answered") or 0) for r in per_template) >= 1:
            reached += 1
    return summarize_calls(
        {"outcomes": rows, "contacted": contacted, "reached": reached}
    )


def summarize_calls(stats: Dict[str, Any]) -> WorkflowCallSummary:
    """PURE: fold the lead store's per (template, outcome) rows into the
    plan-wide summary, and into one card per agent that actually rang.

    One read answers both: the store groups by template as well as outcome,
    so the totals are the sum of the cards and the two can never disagree —
    which a second query for the same numbers could not promise.

    Who answered is not decided here: each row carries ``spoke``, the lead
    store's one answered definition judged per row (BUSY included since 24
    Sep 2026 — a picked-up line), so ``connected`` and ``answered`` are the
    same count and can never drift from ``reached_runs``, which the same
    definition counts. Both names stay on the wire for the console."""
    by_outcome: Dict[str, int] = {}
    placed = connected = answered = timed = attempts = 0
    talk = 0.0
    cost: Optional[float] = None
    per_template: Dict[str, Dict[str, Any]] = {}
    for row in stats.get("outcomes", []):
        outcome = str(row.get("outcome") or "N/A")
        calls = int(row.get("calls") or 0)
        by_outcome[outcome] = by_outcome.get(outcome, 0) + calls
        card = per_template.setdefault(
            str(row.get("template") or ""),
            {
                "placed": 0,
                "connected": 0,
                "by_outcome": {},
                "talk": 0.0,
                "timed": 0,
                "attempts": 0,
                "cost": None,
            },
        )
        spoke = bool(row.get("spoke"))
        card["placed"] += calls
        card["by_outcome"][outcome] = card["by_outcome"].get(outcome, 0) + calls
        if spoke:
            card["connected"] += calls
        card["timed"] += int(row.get("timed_calls") or 0)
        card["attempts"] += int(row.get("attempts") or 0)
        card["talk"] += float(row.get("talk_seconds") or 0)
        if row.get("cost") is not None:
            card["cost"] = (card["cost"] or 0.0) + float(row["cost"])
        placed += calls
        if spoke:
            connected += calls
            answered += calls
        timed += int(row.get("timed_calls") or 0)
        talk += float(row.get("talk_seconds") or 0)
        attempts += int(row.get("attempts") or 0)
        if row.get("cost") is not None:
            cost = (cost or 0.0) + float(row["cost"])
    return WorkflowCallSummary(
        by_template=[
            TemplateCalls(
                template=name or "(no template)",
                placed=card["placed"],
                connected=card["connected"],
                by_outcome=card["by_outcome"],
                talk_seconds_avg=(
                    round(card["talk"] / card["timed"], 1) if card["timed"] else None
                ),
                attempts_avg=(
                    round(card["attempts"] / card["placed"], 2)
                    if card["placed"]
                    else None
                ),
                cost_total=(
                    round(card["cost"], 2) if card["cost"] is not None else None
                ),
            )
            # Busiest agent first: a plan's main template is the card a
            # merchant looks at, and an arm that rang twice should not head
            # the page because its name sorts earlier.
            for name, card in sorted(
                per_template.items(), key=lambda kv: (-kv[1]["placed"], kv[0])
            )
        ],
        placed=placed,
        connected=connected,
        answered=answered,
        by_outcome=by_outcome,
        talk_seconds_avg=round(talk / timed, 1) if timed else None,
        attempts_avg=round(attempts / placed, 2) if placed else None,
        contacted_runs=int(stats.get("contacted") or 0),
        reached_runs=int(stats.get("reached") or 0),
        cost_total=round(cost, 2) if cost is not None else None,
    )


async def workflow_report(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> Optional[WorkflowReport]:
    """The day report for a window of the plan's runs (entered_at): the
    customer table and the call table. None when no such plan; ValueError
    on an unbounded or inverted window (bounded_window).

    Two reads, each the narrowest one that answers its own question — the
    plan's runs that entered in the window (how each stands), and what the
    lead store did for each of them (how many calls, and when the first
    conversation began). The arithmetic is pure and lives below, so the
    shape a merchant reads is testable without a database."""
    since, until = bounded_window(since, until)
    workflow = await workflow_accessor.get_workflow(merchant_id, workflow_id)
    if workflow is None:
        return None
    endings = await enrollment_accessor.run_endings_in_window(
        merchant_id, workflow_id, since, until
    )
    facts = await get_call_facts_by_runs(merchant_id, _lifetimes(endings))
    return build_report(endings, facts)


# A run with no entered_at (never on a real row) still needs a lower bound
# for its retry-lead window; the epoch admits everything.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def build_report(
    endings: List[RunEnding], facts: Dict[str, List[Dict[str, Any]]]
) -> WorkflowReport:
    """PURE decide: the two tables, from the runs and their calls.

    Before/after is judged per run by time — the run's first ANSWERED call
    (over every template) against its exited_at. A run that ended with no
    answered call, or whose first conversation came after the end,
    converted (or dropped) before we spoke to them. The seven ending
    buckets partition the runs, so the customer table always sums to
    ``runs``. The per-template cards fold from the same rows.

    ``by_reach`` cuts the same runs by stage (never dialled / dialled, no
    answer / spoke) with the same time rule, so its "spoke" column is
    exactly the after-we-spoke pair above; ``open_by_square`` is where the
    open ones stand."""
    by_reach = {
        stage: {
            "runs": 0,
            "goal_met": 0,
            "withdrawn": 0,
            "ejected": 0,
            "other_ended": 0,
            "open": 0,
        }
        for stage in REACH_STAGES
    }
    open_by_square: Dict[str, int] = {}
    customers = {
        "runs": len(endings),
        "unique_customers": len({e.enrollment_key for e in endings}),
        "reached": 0,
        "goal_met_before_reach": 0,
        "goal_met_after_reach": 0,
        "withdrawn_before_reach": 0,
        "withdrawn_after_reach": 0,
        "ejected": 0,
        "other_ended": 0,
        "open": 0,
    }
    plan_calls = _empty_calls()
    per_customer: Dict[int, int] = {}
    histogram: Dict[int, _Bar] = {}
    cards: Dict[str, Dict[str, Any]] = {}
    for ending in endings:
        rows = facts.get(ending.id) or []
        placed = sum(int(r.get("placed") or 0) for r in rows)
        _add_to_bar(histogram, rows)
        answered = sum(int(r.get("answered") or 0) for r in rows)
        firsts = [r["first_answered_at"] for r in rows if r.get("first_answered_at")]
        first = min(firsts) if firsts else None
        for key in plan_calls:
            plan_calls[key] += sum(int(r.get(key) or 0) for r in rows)
        per_customer[placed] = per_customer.get(placed, 0) + 1
        if answered >= 1:
            customers["reached"] += 1
        for r in rows:
            card = cards.setdefault(
                str(r.get("template") or ""),
                {
                    "calls": _empty_calls(),
                    "reached": 0,
                    "per_customer": {},
                    "histogram": {},
                },
            )
            _add_to_bar(card["histogram"], [r])
            for key in card["calls"]:
                card["calls"][key] += int(r.get(key) or 0)
            n = int(r.get("placed") or 0)
            card["per_customer"][n] = card["per_customer"].get(n, 0) + 1
            if int(r.get("answered") or 0) >= 1:
                card["reached"] += 1

        if ending.status != "exited":
            customers["open"] += 1
            square = ending.current_node or "(none)"
            open_by_square[square] = open_by_square.get(square, 0) + 1
            stage = _stage(placed, answered >= 1)
            by_reach[stage]["runs"] += 1
            by_reach[stage]["open"] += 1
            continue
        spoke_first = (
            first is not None
            and ending.exited_at is not None
            and first < ending.exited_at
        )
        when = "after" if spoke_first else "before"
        stage = _stage(placed, spoke_first)
        by_reach[stage]["runs"] += 1
        if ending.exit_reason == "goal_met":
            customers[f"goal_met_{when}_reach"] += 1
            by_reach[stage]["goal_met"] += 1
        elif ending.exit_reason == "withdrawn":
            customers[f"withdrawn_{when}_reach"] += 1
            by_reach[stage]["withdrawn"] += 1
        elif ending.exit_reason == "ejected":
            customers["ejected"] += 1
            by_reach[stage]["ejected"] += 1
        else:
            customers["other_ended"] += 1
            by_reach[stage]["other_ended"] += 1

    dialled = sum(n for count, n in per_customer.items() if count >= 1)
    return WorkflowReport(
        customers=ReportCustomers(
            **customers,
            calls_per_customer={str(k): v for k, v in sorted(per_customer.items())},
            call_histogram=_bars(histogram),
            by_reach={k: ReportReach(**v) for k, v in by_reach.items()},
            open_by_square=dict(sorted(open_by_square.items())),
        ),
        calls=_report_calls(plan_calls, dialled, customers["reached"]),
        by_template=[
            ReportTemplate(
                template=name or "(no template)",
                calls=_report_calls(
                    card["calls"],
                    sum(n for c, n in card["per_customer"].items() if c >= 1),
                    card["reached"],
                ),
                reached=card["reached"],
                calls_per_customer={
                    str(k): v for k, v in sorted(card["per_customer"].items())
                },
                call_histogram=_bars(card["histogram"]),
            )
            # Busiest agent first — a plan's main template is the card a
            # merchant looks at.
            for name, card in sorted(
                cards.items(), key=lambda kv: (-kv[1]["calls"]["placed"], kv[0])
            )
        ],
    )


# A calls-per-customer bar while it is being folded: runs, outcome → leads.
_Bar = Tuple[int, Dict[str, int]]


def _add_to_bar(histogram: Dict[int, _Bar], rows: List[Dict[str, Any]]) -> None:
    """Put one run on the bar for how many FINISHED calls ``rows`` hold —
    placed or not, so a call the dialler refused counts, one still queued
    or on the line does not — and add their outcomes to that bar. A run
    with none is the 0 bar, with nothing to break down."""
    calls = sum(int(r.get("finished") or 0) for r in rows)
    runs, outcomes = histogram.get(calls, (0, {}))
    for r in rows:
        for outcome, n in _outcomes(r.get("outcomes")).items():
            outcomes[outcome] = outcomes.get(outcome, 0) + n
    histogram[calls] = (runs + 1, outcomes)


def _outcomes(raw: Any) -> Dict[str, int]:
    """The facts row's jsonb, as asyncpg hands it back (text, no codec) or
    as a dict; anything unreadable is no breakdown rather than a failure."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}


def _bars(histogram: Dict[int, _Bar]) -> List[ReportCallBar]:
    """The folded bars, ascending by calls; outcomes busiest first."""
    return [
        ReportCallBar(
            calls=calls,
            runs=runs,
            outcomes=dict(sorted(outcomes.items(), key=lambda kv: (-kv[1], kv[0]))),
        )
        for calls, (runs, outcomes) in sorted(histogram.items())
    ]


def _stage(placed: int, spoke: bool) -> str:
    """PURE: which of the three reach stages a run is in."""
    if spoke:
        return "spoke"
    return "dialled_no_answer" if placed >= 1 else "never_dialled"


def _empty_calls() -> Dict[str, int]:
    return {
        "leads": 0,
        "placed": 0,
        "answered": 0,
        "no_answer": 0,
        "busy": 0,
        "in_progress": 0,
    }


def _report_calls(calls: Dict[str, int], dialled: int, reached: int) -> ReportCalls:
    """PURE: the call table with its two repeat counts — placed calls beyond
    the first to a run, answered calls beyond the first."""
    return ReportCalls(
        **calls,
        repeat_calls=calls["placed"] - dialled,
        repeat_answered=calls["answered"] - reached,
    )
