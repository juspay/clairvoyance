"""Daily coverage / consistency report for the call outcome columns.

While the call outcome columns (migration 080) are written beside the legacy
``outcome``, this reads every lead finished in the last day and asks two
questions of it:

  coverage     Did the write set ``connection_status``? A write path that
               forgot the columns shows up as uncovered rows, grouped by the
               legacy outcome it wrote — which names the path.
  consistency  Do the columns agree with the legacy value the same write
               produced? A carrier NO_ANSWER must be NO_ANSWER / BUSY /
               FAILED / CANCELED; a dispatcher refusal NOT_DIALED with the
               same reason; an agent outcome the legacy word. A mismatch is a
               write path mapping wrong.

Posts to Slack once a day, and only while CALL_OUTCOME_WRITES_ENABLED is on.
Goes away with the legacy column.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.ai.voice.agents.breeze_buddy.services.call_limiter import CALL_LIMIT_OUTCOME
from app.core.logger import logger
from app.database.accessor.breeze_buddy.call_outcome import (
    call_outcome_writes_enabled,
)
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    get_call_outcome_coverage,
)
from app.schemas.breeze_buddy.outcomes import (
    ConnectionReason,
    ConnectionStatus,
    EndReason,
)
from app.services.slack.alert import slack_alert

CALL_OUTCOME_COVERAGE_INTERVAL_SECONDS = 24 * 60 * 60
_WINDOW = timedelta(seconds=CALL_OUTCOME_COVERAGE_INTERVAL_SECONDS)
_TOP = 10
# Below this share of covered rows (or with any mismatch) the report tags
# on-call; otherwise it posts quietly.
_COVERAGE_TARGET = 0.999

# Legacy word a dispatcher / abort path writes -> the reason it must carry.
_NOT_DIALED_REASONS: Dict[str, ConnectionReason] = {
    "PRECHECK_FAILED": ConnectionReason.PRECHECK_FAILED,
    "BLACKLISTED": ConnectionReason.BLACKLISTED,
    "NUMBER_UNAVAILABLE": ConnectionReason.NUMBER_UNAVAILABLE,
    "INVALID_PHONE": ConnectionReason.INVALID_PHONE,
    "NO_CONFIG": ConnectionReason.NO_CONFIG,
    "ABORT": ConnectionReason.ABORTED,
    "ABORTED": ConnectionReason.ABORTED,
    CALL_LIMIT_OUTCOME: ConnectionReason.CALL_LIMIT,
}
_REJECTED_REASONS: Dict[str, ConnectionReason] = {
    "BLOCKED_REJECT": ConnectionReason.BLOCKED,
    "BLOCKED_REDIRECT": ConnectionReason.BLOCKED,
    "CAPACITY_REJECTED": ConnectionReason.CAPACITY,
}
# Every carrier failure is written NO_ANSWER in the legacy column.
_CARRIER_STATUSES = frozenset(
    {
        ConnectionStatus.NO_ANSWER.value,
        ConnectionStatus.BUSY.value,
        ConnectionStatus.FAILED.value,
        ConnectionStatus.CANCELED.value,
    }
)
# Legacy words an answered call gets when the agent decided nothing.
_SYSTEM_FALLBACKS = frozenset(
    {
        "BUSY",
        "UNKNOWN",
        "EARLY_HANGUP",
        "IVR_ERROR",
        "IVR_LOOP_GUARD",
        "IVR_NODE_MISSING",
        "TRANSFERRED",
        "ended_by_widget",
    }
)


@dataclass(frozen=True)
class CoverageReport:
    since: datetime
    finished: int
    covered: int
    consistent: int
    uncovered: List[Tuple[str, int]]
    mismatches: List[Tuple[str, int]]

    @property
    def coverage(self) -> float:
        return self.covered / self.finished if self.finished else 1.0

    @property
    def consistency(self) -> float:
        return self.consistent / self.covered if self.covered else 1.0

    @property
    def healthy(self) -> bool:
        return self.coverage >= _COVERAGE_TARGET and not self.mismatches


def is_consistent(row: Mapping[str, Any]) -> bool:
    """PURE: do a covered row's call outcome columns agree with its legacy
    outcome, as the one write that produced both would have set them?"""
    legacy: Optional[str] = row.get("outcome")
    status = row.get("connection_status")
    reason = row.get("connection_reason")
    end_reason = row.get("end_reason")
    agent_outcome = row.get("agent_outcome")

    if status == ConnectionStatus.NOT_DIALED.value:
        expected = _NOT_DIALED_REASONS.get(legacy or "")
        return expected is not None and reason == expected.value
    if status == ConnectionStatus.REJECTED.value:
        expected = _REJECTED_REASONS.get(legacy or "")
        return expected is not None and reason == expected.value
    if status in _CARRIER_STATUSES:
        return legacy == "NO_ANSWER"
    if status == ConnectionStatus.UNKNOWN.value:
        return legacy == "UNKNOWN"
    if status == ConnectionStatus.ANSWERED.value:
        if not agent_outcome:
            return legacy is None or legacy in _SYSTEM_FALLBACKS
        return (
            (legacy or "").strip().upper() == agent_outcome
            # The legacy column is overwritten on a successful transfer and on
            # the user-idle timeout; the agent's decision survives in the
            # agent column.
            or (legacy == "TRANSFERRED" and end_reason == EndReason.TRANSFERRED.value)
            or (legacy == "BUSY" and end_reason == EndReason.IDLE_TIMEOUT.value)
        )
    return False


def _label(row: Mapping[str, Any]) -> str:
    parts = [
        row.get("connection_status"),
        row.get("connection_reason"),
        row.get("end_reason"),
        row.get("agent_outcome"),
    ]
    return f"{row.get('outcome') or '(none)'} → " + "/".join(p or "-" for p in parts)


def summarize_coverage(
    rows: Sequence[Mapping[str, Any]], since: datetime
) -> CoverageReport:
    """PURE: the day's grouped rows in, the report out."""
    finished = covered = consistent = 0
    uncovered: Counter = Counter()
    mismatches: Counter = Counter()
    for row in rows:
        count = int(row.get("rows") or 0)
        finished += count
        if not row.get("connection_status"):
            uncovered[row.get("outcome") or "(none)"] += count
            continue
        covered += count
        if is_consistent(row):
            consistent += count
        else:
            mismatches[_label(row)] += count
    return CoverageReport(
        since=since,
        finished=finished,
        covered=covered,
        consistent=consistent,
        uncovered=uncovered.most_common(_TOP),
        mismatches=mismatches.most_common(_TOP),
    )


def _lines(items: List[Tuple[str, int]]) -> str:
    return "\n".join(f"`{label}`: {count}" for label, count in items) or "none"


async def report_call_outcome_coverage() -> None:
    """Scheduled daily: read the last day's finished leads, post the report."""
    if not await call_outcome_writes_enabled():
        return
    since = datetime.now(timezone.utc) - _WINDOW
    try:
        rows = await get_call_outcome_coverage(since)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Call outcome coverage read failed: {e}")
        return

    report = summarize_coverage(rows, since)
    logger.info(
        f"Call outcome coverage since {since.isoformat()}: "
        f"finished={report.finished} covered={report.covered} "
        f"consistent={report.consistent} mismatch_groups={len(report.mismatches)}"
    )
    try:
        await slack_alert.send(
            title="Call outcome columns: daily coverage",
            fields=[
                {"name": "Window", "value": f"since {since:%Y-%m-%d %H:%M} UTC"},
                {"name": "Finished leads", "value": str(report.finished)},
                {"name": "Covered", "value": f"{report.coverage:.2%}"},
                {"name": "Consistent", "value": f"{report.consistency:.2%}"},
            ],
            sections=[
                {
                    "title": "Uncovered, by legacy outcome",
                    "text": _lines(report.uncovered),
                },
                {
                    "title": "Mismatches (legacy → status/reason/end/agent)",
                    "text": _lines(report.mismatches),
                },
            ],
            include_tags=not report.healthy,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Call outcome coverage Slack post failed: {e}")
