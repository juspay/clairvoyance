"""A broadcast's report — the funnel (canon/06-outreach.md:45-46: "skips on
T18; sends/refusals/receipts on the runs' manifest rows — runs WHERE
source_broadcast_id = B"; ADR 0010: "the funnel reads calls via the stamped
enrollment id"). Broadcasts phase 4.

One launch's numbers, where the workflow's own report counts every run of
the plan — from events and from every broadcast that launched it:
- the frozen list: how many, where each row stands, why the skipped were
  skipped ("the number was smaller than expected" answered, canon T18);
- the runs it started: still open by status, ended by exit reason;
- the calls those runs placed — the same lead reads and the same fold as
  the plan's calls summary (analytics.calls_of_runs), so the two never
  count a run apart.

gather (three reads, on the replica: the console's report) -> decide (PURE:
build_funnel). The calls read goes in batches of CALL_BATCH runs: a
broadcast is not bounded by a window the way the Performance tab is.

Not counted yet: the runs' WhatsApp messages (the manifest, T16) — the
manifest has no index on the run that sent a message (056); today's
broadcasts call. Named here so the gap is not silent.
"""

from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Optional, Sequence, Tuple

from app.crm.outreach.analytics import calls_of_runs
from app.crm.outreach.broadcasts import (
    BroadcastNotFound,
    max_age_days,
    report_window_end,
)
from app.crm.outreach.db.accessors import (
    broadcast as broadcast_accessor,
    broadcast_recipient as recipient_accessor,
    enrollment as enrollment_accessor,
)
from app.crm.outreach.schemas import (
    Broadcast,
    BroadcastFunnel,
    BroadcastRecipients,
    BroadcastRuns,
    RunEnding,
    WorkflowCallSummary,
)

#: How many runs one lead read carries. The Performance tab's widest window
#: (a week) is about this many runs for the busiest merchant; a broadcast of
#: lakhs is read in several.
CALL_BATCH = 20_000


async def broadcast_funnel(merchant_id: str, broadcast_id: str) -> BroadcastFunnel:
    """The report for one broadcast. Raises BroadcastNotFound (not this
    merchant's)."""
    broadcast = await broadcast_accessor.get_broadcast(merchant_id, broadcast_id)
    if broadcast is None:
        raise BroadcastNotFound(broadcast_id)
    counts = await recipient_accessor.recipient_counts(merchant_id, broadcast_id)
    endings = await enrollment_accessor.broadcast_run_endings(merchant_id, broadcast_id)
    calls = await calls_of_runs(merchant_id, endings, batch=CALL_BATCH)
    return build_funnel(
        broadcast,
        counts,
        endings,
        calls,
        await max_age_days(broadcast),
        datetime.now(timezone.utc),
    )


def build_funnel(
    broadcast: Broadcast,
    counts: Dict[Tuple[str, Optional[str]], int],
    endings: Sequence[RunEnding],
    calls: WorkflowCallSummary,
    age_days: float,
    now: datetime,
) -> BroadcastFunnel:
    """PURE decide: the three reads folded into the report. ``closes_at`` is
    when the numbers go final (broadcasts.report_window_end) — counted from
    the last run's entry, or from the fire before any run is read."""
    by_status: Counter = Counter()
    skipped: Counter = Counter()
    for (status, reason), n in counts.items():
        by_status[status] += n
        if status == "skipped":
            skipped[reason or "unknown"] += n
    still_open = Counter(e.status for e in endings if e.status != "exited")
    ended = Counter(e.exit_reason or "unknown" for e in endings if e.status == "exited")
    last_entered = max((e.entered_at for e in endings if e.entered_at), default=None)
    return BroadcastFunnel(
        broadcast_id=broadcast.id,
        status=broadcast.status,
        resolved_at=broadcast.resolved_at,
        dispatched_at=broadcast.dispatched_at,
        closes_at=report_window_end(last_entered or broadcast.dispatched_at, age_days),
        closed_at=broadcast.closed_at,
        recipients=BroadcastRecipients(
            frozen=sum(by_status.values()),
            by_status=dict(by_status),
            skipped_by_reason=dict(skipped),
        ),
        runs=BroadcastRuns(
            started=len(endings),
            open=dict(still_open),
            by_exit_reason=dict(ended),
        ),
        calls=calls,
        computed_at=now,
    )
