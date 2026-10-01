#!/usr/bin/env python3
"""Backfill the call outcome columns (migration 080) on historical leads.

Rows finished before CALL_OUTCOME_WRITES_ENABLED was switched on have only the
legacy ``outcome``. This classifies each one from its legacy word and the
``meta_data`` the pipeline left behind, and writes the call outcome columns
plus ``backfilled_at``. Best effort by design: where history does not say
(a carrier busy stored as NO_ANSWER, the agent's word behind a TRANSFERRED),
the columns say only what is known.

What it never does: touch ``outcome``, ``meta_data`` or ``updated_at`` (the
daily coverage report windows on ``updated_at``), fire the CRM finished hook,
or overwrite a row that already has ``connection_status``. A rerun resumes
where the last one stopped.

Usage (connection from .env, like scripts/migrate.py):
    backfill_call_outcomes.py --before 2026-10-01T00:00:00+00:00 --dry-run
    backfill_call_outcomes.py --before ... --review BUSY --review-limit 200
    backfill_call_outcomes.py --before ... [--batch 1000] [--sleep 0.2]

``--before`` is when CALL_OUTCOME_WRITES_ENABLED was turned on: only rows
created before it are touched.

Undo: UPDATE lead_call_tracker SET <call outcome columns> = NULL,
backfilled_at = NULL WHERE backfilled_at IS NOT NULL.

Plan: docs/CALL_OUTCOMES.md (Phase 2, 2b). Deleted with the legacy column.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

import asyncpg
from dotenv import load_dotenv

from app.schemas.breeze_buddy.outcomes import (
    LEGACY_IVR_ERRORS,
    LEGACY_NOT_DIALED_REASONS,
    LEGACY_REJECTED_REASONS,
    CallOutcome,
    ConnectionStatus,
    EndReason,
    OutcomeSource,
    end_reason_from_ended_by,
)

load_dotenv()

# The columns this script writes, in the order of the UPDATE's unnest arrays.
COLUMNS = (
    "connection_status",
    "connection_reason",
    "provider_status",
    "end_reason",
    "agent_outcome",
    "outcome_source",
)


def _source(meta: Mapping[str, Any]) -> OutcomeSource:
    """Who decided a surviving agent word: an observer marks the row, an IVR
    press leaves ``dtmf_inputs`` in node_traversal, anything else was the LLM."""
    if meta.get("observer_triggered"):
        return OutcomeSource.OBSERVER
    for entry in meta.get("node_traversal") or []:
        if isinstance(entry, dict) and entry.get("dtmf_inputs"):
            return OutcomeSource.IVR
    return OutcomeSource.LLM


def classify(outcome: Optional[str], meta: Optional[Mapping[str, Any]]) -> CallOutcome:
    """PURE: a finished row's legacy word and meta_data -> its call outcome.

    Order matters where one legacy word has several writers (BUSY, NO_ANSWER,
    UNKNOWN): the most specific signal is checked first.
    """
    meta = meta or {}
    word = (outcome or "").strip()
    ended = end_reason_from_ended_by(meta.get("call_ended_by"))

    if word in LEGACY_NOT_DIALED_REASONS:
        return CallOutcome(
            connection_status=ConnectionStatus.NOT_DIALED,
            connection_reason=LEGACY_NOT_DIALED_REASONS[word],
        )
    if word in LEGACY_REJECTED_REASONS:
        return CallOutcome(
            connection_status=ConnectionStatus.REJECTED,
            connection_reason=LEGACY_REJECTED_REASONS[word],
        )

    if word == "NO_ANSWER":
        # The carrier path writes meta_data={} — an empty row is a carrier
        # failure (busy / failed / no-answer can no longer be told apart).
        # Anything else is an agent that wrote the word after a conversation.
        if not meta:
            return CallOutcome(connection_status=ConnectionStatus.NO_ANSWER)
        return CallOutcome(
            connection_status=ConnectionStatus.ANSWERED, end_reason=ended
        )

    if word == "UNKNOWN":
        if meta.get("cleanup") == "completed_no_pipeline":
            return CallOutcome(
                connection_status=ConnectionStatus.ANSWERED,
                provider_status="completed",
            )
        if meta.get("errors") and meta.get("call_ended_by") == "system":
            # end_call_with_errors: the media socket connected, setup failed.
            return CallOutcome(
                connection_status=ConnectionStatus.ANSWERED,
                end_reason=EndReason.PIPELINE_ERROR,
            )
        return CallOutcome(connection_status=ConnectionStatus.UNKNOWN)

    if word == "EARLY_HANGUP":
        return CallOutcome(
            connection_status=ConnectionStatus.ANSWERED,
            end_reason=EndReason.EARLY_HANGUP,
        )
    if word in LEGACY_IVR_ERRORS:
        return CallOutcome(
            connection_status=ConnectionStatus.ANSWERED,
            end_reason=EndReason.IVR_ERROR,
        )
    if word == "TRANSFERRED":
        # The agent's own word was overwritten in the legacy column; lost.
        return CallOutcome(
            connection_status=ConnectionStatus.ANSWERED,
            end_reason=EndReason.TRANSFERRED,
        )
    if word == "ended_by_widget":
        return CallOutcome(
            connection_status=ConnectionStatus.ANSWERED,
            end_reason=EndReason.CUSTOMER_HANGUP,
        )

    if word == "BUSY":
        # The idle timeout overwrites whatever the agent set, so it is checked
        # before the hook's trace (an idle timeout can follow a hook write).
        if meta.get("call_end_reason") == "user_idle_timeout":
            return CallOutcome(
                connection_status=ConnectionStatus.ANSWERED,
                end_reason=EndReason.IDLE_TIMEOUT,
            )
        # The outcome hook always creates meta_data.outcome; the fallbacks
        # (disconnect, end_conversation_global, IVR incomplete) never do.
        if "outcome" in meta:
            return CallOutcome(
                connection_status=ConnectionStatus.ANSWERED,
                end_reason=ended,
                agent_outcome="BUSY",
                outcome_source=_source(meta),
            )
        return CallOutcome(
            connection_status=ConnectionStatus.ANSWERED, end_reason=ended
        )

    if not word:
        # FINISHED with no outcome at all: answered only if a conversation left
        # a transcript behind.
        if meta.get("transcription"):
            return CallOutcome(
                connection_status=ConnectionStatus.ANSWERED, end_reason=ended
            )
        return CallOutcome(connection_status=ConnectionStatus.UNKNOWN)

    return CallOutcome(
        connection_status=ConnectionStatus.ANSWERED,
        end_reason=ended,
        agent_outcome=word,
        outcome_source=_source(meta),
    )


def column_values(
    outcome: Optional[str], meta: Optional[Mapping[str, Any]]
) -> Dict[str, Optional[str]]:
    """The row's values for every column in COLUMNS (None where unknown)."""
    columns = classify(outcome, meta).columns()
    return {column: columns.get(column) for column in COLUMNS}


def label(values: Mapping[str, Optional[str]]) -> str:
    return "/".join(values.get(c) or "-" for c in COLUMNS[:5])


# ---------------------------------------------------------------------------
# DB mechanics
# ---------------------------------------------------------------------------

SELECT_BATCH = """
    SELECT "id", "outcome", "meta_data"
    FROM "lead_call_tracker"
    WHERE "status" = 'FINISHED'
      AND "connection_status" IS NULL
      AND "created_at" < $1
      AND "id" > $2
    ORDER BY "id"
    LIMIT $3
"""

# Sets only the call outcome columns and backfilled_at. The IS NULL guard
# means a row the live writers reached in the meantime is never overwritten.
UPDATE_BATCH = """
    UPDATE "lead_call_tracker" AS l
    SET "connection_status" = v.connection_status,
        "connection_reason" = v.connection_reason,
        "provider_status"   = v.provider_status,
        "end_reason"        = v.end_reason,
        "agent_outcome"     = v.agent_outcome,
        "outcome_source"    = v.outcome_source,
        "backfilled_at"     = now()
    FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[],
                $6::text[], $7::text[])
         AS v(id, connection_status, connection_reason, provider_status,
              end_reason, agent_outcome, outcome_source)
    WHERE l."id" = v.id AND l."connection_status" IS NULL
"""


def _meta(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DB"],
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
    )


async def run(
    before: datetime,
    dry_run: bool,
    batch: int,
    sleep: float,
    review: Optional[str],
    review_limit: int,
) -> Tuple[int, Counter]:
    conn = await _connect()
    groups: Counter = Counter()
    written = 0
    reviewed = 0
    cursor = ""
    try:
        while True:
            rows = await conn.fetch(SELECT_BATCH, before, cursor, batch)
            if not rows:
                break
            cursor = rows[-1]["id"]
            ids: List[str] = []
            arrays: Dict[str, List[Optional[str]]] = {c: [] for c in COLUMNS}
            for row in rows:
                meta = _meta(row["meta_data"])
                values = column_values(row["outcome"], meta)
                groups[f"{row['outcome'] or '(none)'} -> {label(values)}"] += 1
                if review and row["outcome"] == review and reviewed < review_limit:
                    reviewed += 1
                    print(
                        json.dumps(
                            {
                                "id": row["id"],
                                "outcome": row["outcome"],
                                "call_ended_by": meta.get("call_ended_by"),
                                "call_end_reason": meta.get("call_end_reason"),
                                "has_hook_trace": "outcome" in meta,
                                "has_transcript": bool(meta.get("transcription")),
                                "classified": values,
                            }
                        )
                    )
                ids.append(row["id"])
                for column in COLUMNS:
                    arrays[column].append(values[column])
            if not dry_run:
                status = await conn.execute(
                    UPDATE_BATCH, ids, *(arrays[c] for c in COLUMNS)
                )
                written += int(status.split()[-1])
            if sleep:
                await asyncio.sleep(sleep)
    finally:
        await conn.close()
    return written, groups


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--before",
        required=True,
        type=datetime.fromisoformat,
        help="Only rows created before this instant (when writes were switched on)",
    )
    p.add_argument("--dry-run", action="store_true", help="Classify, write nothing")
    p.add_argument("--batch", type=int, default=1000)
    p.add_argument("--sleep", type=float, default=0.2, help="Seconds between batches")
    p.add_argument(
        "--review",
        metavar="OUTCOME",
        help="Print rows with this legacy outcome and their classification",
    )
    p.add_argument("--review-limit", type=int, default=200)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.before.tzinfo is None:
        print("error: --before needs a timezone (e.g. +00:00)", file=sys.stderr)
        return 2
    written, groups = asyncio.run(
        run(
            args.before,
            args.dry_run,
            args.batch,
            args.sleep,
            args.review,
            args.review_limit,
        )
    )
    print(
        f"\n{'Would classify' if args.dry_run else 'Classified'} {sum(groups.values())} row(s):"
    )
    for group, count in groups.most_common():
        print(f"  {count:>8}  {group}")
    if not args.dry_run:
        print(f"\nWrote {written} row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
