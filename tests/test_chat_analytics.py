"""Tests for the chat conversational-log analytics layer (Phase 1).

Covers the DB-free pieces:
- ``list_chat_sessions_query`` / ``count_chat_sessions_query`` filter +
  placeholder construction (admin vs scoped, all filters).
- ``record_chat_turn_metrics_query`` shape (upsert + 15 params — migration 041
  added ``drops`` as ``$15``), including that ``drops`` is in the DO UPDATE set
  and is serialised as a JSON string for the ``$15::jsonb`` cast.
- ``decode_chat_session_summary`` (preview truncation, message_count default)
  and ``decode_chat_turn_metrics``, including ``drops`` decoding.
- ``TurnMetrics`` capturing ``assistant_idx`` from the turn_end event +
  stamping ``total_ms`` at emit.
- The aggregate analytics query builders: filter/placeholder construction, the
  granularity whitelist, and the output aliases each API branch consumes.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.ai.voice.agents.breeze_buddy.chat.metrics import TurnMetrics
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.database.decoder.breeze_buddy.chat_session import (
    decode_chat_session_summary,
    decode_chat_turn_metrics,
)
from app.database.queries.breeze_buddy.chat_analytics import (
    build_chat_analytics_where_clause,
    get_chat_analytics_summary_query,
    get_chat_analytics_trends_query,
)
from app.database.queries.breeze_buddy.chat_session import (
    count_chat_sessions_query,
    list_chat_sessions_query,
    record_chat_turn_metrics_query,
)
from app.schemas.breeze_buddy.chat import (
    ChatSessionStatus,
    ChatTurnDrop,
    WidgetChannel,
)

# ---------------------------------------------------------------------------
# Session list / count query builders
# ---------------------------------------------------------------------------


def test_list_query_admin_no_filters_has_no_where():
    query, values = list_chat_sessions_query({}, limit=20, offset=0)
    # No outer WHERE on chat_session (the correlated subqueries have their own
    # inner WHEREs — only the segment between the outer FROM and ORDER BY
    # matters here).
    outer = query.split("FROM chat_session cs")[1].split("ORDER BY")[0]
    assert "WHERE" not in outer
    # LIMIT/OFFSET take the first two placeholders when there are no filters.
    assert "LIMIT $1 OFFSET $2" in query
    assert values == [20, 0]
    # Most-recently-active first + the two derived columns.
    assert "ORDER BY cs.last_activity_at DESC" in query
    assert "AS message_count" in query
    assert "AS preview" in query


def test_list_query_all_filters_placeholder_order():
    dt1 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    dt2 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    filters = {
        "reseller_ids": ["r1", "r2"],
        "template_id": "tid",
        "status": "ENDED",
        "date_from": dt1,
        "date_to": dt2,
    }
    query, values = list_chat_sessions_query(filters, limit=50, offset=100)
    assert "reseller_id = ANY($1)" in query
    assert "template_id = $2::uuid" in query
    assert "status = $3" in query
    assert "created_at >= $4" in query
    assert "created_at < $5" in query
    assert "LIMIT $6 OFFSET $7" in query
    assert values == [["r1", "r2"], "tid", "ENDED", dt1, dt2, 50, 100]


def test_list_query_single_reseller_and_merchant_forms():
    query, values = list_chat_sessions_query(
        {"reseller_id": "r1", "merchant_id": "m1"}, limit=10, offset=0
    )
    assert "reseller_id = $1" in query
    assert "merchant_id = $2" in query
    assert "LIMIT $3 OFFSET $4" in query
    assert values == ["r1", "m1", 10, 0]


def test_count_query_mirrors_filters_without_pagination():
    filters = {"reseller_ids": ["r1"], "template_id": "tid"}
    query, values = count_chat_sessions_query(filters)
    assert query.strip().startswith("SELECT COUNT(*) AS total FROM chat_session")
    assert "reseller_id = ANY($1)" in query
    assert "template_id = $2::uuid" in query
    assert "LIMIT" not in query
    assert values == [["r1"], "tid"]


# ---------------------------------------------------------------------------
# Turn-metrics query builder
# ---------------------------------------------------------------------------


def test_record_metrics_query_is_idempotent_upsert():
    query, values = record_chat_turn_metrics_query(
        "sess",
        4,
        ttft_ms=12.5,
        ttfui_ms=30.0,
        ttlui_ms=80.0,
        total_ms=120.0,
        ui_ops=3,
        ui_dropped=1,
        healer_applied=0,
        tool_calls=2,
        prose_chars=40,
        ui_chars=900,
        status="ACTIVE",
        phase="baseline",
    )
    assert "INSERT INTO chat_turn_metrics" in query
    assert "ON CONFLICT (session_id, idx) DO UPDATE" in query
    # 15 params since migration 041 added `drops` as $15.
    assert len(values) == 15
    assert values[0] == "sess" and values[1] == 4
    assert values[-3] == "ACTIVE" and values[-2] == "baseline"
    # drops defaults to None when the caller passes nothing — the column is
    # nullable and "nothing dropped" must not write an empty array.
    assert values[-1] is None
    # Every mutable column has to be in the DO UPDATE set, or a re-emit for
    # the same (session_id, idx) silently keeps the stale value.
    assert "drops = EXCLUDED.drops" in query


def test_record_metrics_query_serialises_drops():
    """`drops` is the per-drop evidence array (migration 041); it must reach
    the driver as a JSON string for the $15::jsonb cast."""
    query, values = record_chat_turn_metrics_query(
        "sess",
        4,
        ttft_ms=None,
        ttfui_ms=None,
        ttlui_ms=None,
        total_ms=None,
        ui_ops=0,
        ui_dropped=1,
        healer_applied=0,
        tool_calls=0,
        prose_chars=0,
        ui_chars=0,
        status="ACTIVE",
        phase="baseline",
        drops_json='[{"reason": "bad_op"}]',
    )
    assert "$15::jsonb" in query
    assert values[-1] == '[{"reason": "bad_op"}]'


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------


def test_decode_session_summary_truncates_preview_and_defaults():
    long_preview = "x" * 300
    # Typed Any: decoders take asyncpg.Record, but a dict duck-types fine at
    # runtime (both support [] + .get); Any keeps the type-checker happy.
    row: Any = {
        "id": "sid",
        "template_id": "tid",
        "reseller_id": "r1",
        "merchant_id": None,
        "status": "ACTIVE",
        "outcome": None,
        "current_channel": None,  # legacy SELECT → defaults to CHAT
        "message_count": 7,
        "preview": long_preview,
        "created_at": None,
        "last_activity_at": None,
        "ended_at": None,
    }
    summary = decode_chat_session_summary(row)
    assert summary is not None
    assert summary.id == "sid"
    assert summary.message_count == 7
    assert summary.current_channel == WidgetChannel.CHAT
    # Truncated to the 160-char cap + ellipsis.
    assert summary.preview is not None
    assert summary.preview.endswith("…")
    assert len(summary.preview) <= 161


def test_decode_session_summary_short_preview_untouched_and_count_default():
    row: Any = {
        "id": "sid",
        "template_id": "tid",
        "reseller_id": "r1",
        "status": "ENDED",
        "preview": "hi there",
        "message_count": None,  # COUNT never NULLs, but be defensive
        "created_at": None,
        "last_activity_at": None,
    }
    summary = decode_chat_session_summary(row)
    assert summary is not None
    assert summary.preview == "hi there"
    assert summary.message_count == 0
    assert summary.status == ChatSessionStatus.ENDED


def test_decode_turn_metrics_maps_fields():
    row: Any = {
        "session_id": "sess",
        "idx": 4,
        "ttft_ms": 12.5,
        "ttfui_ms": 30.0,
        "ttlui_ms": 80.0,
        "total_ms": 120.0,
        "ui_ops": 3,
        "ui_dropped": 1,
        "healer_applied": 0,
        "tool_calls": 2,
        "prose_chars": 40,
        "ui_chars": 900,
        "status": "ACTIVE",
        "phase": "baseline",
        # Both read paths select _TURN_METRICS_COLUMNS, which lists `drops`
        # (migration 041), so a real row always carries the key — NULL when
        # nothing dropped. Omitting it here made this row unrepresentative.
        "drops": None,
        "created_at": None,
    }
    metrics = decode_chat_turn_metrics(row)
    assert metrics is not None
    assert metrics.session_id == "sess" and metrics.idx == 4
    assert metrics.total_ms == 120.0
    assert metrics.ui_ops == 3 and metrics.ui_dropped == 1
    assert metrics.drops is None


def _turn_metrics_row(**overrides: Any) -> Any:
    row: Any = {
        "session_id": "sess",
        "idx": 0,
        "ttft_ms": None,
        "ttfui_ms": None,
        "ttlui_ms": None,
        "total_ms": None,
        "ui_ops": 0,
        "ui_dropped": 0,
        "healer_applied": 0,
        "tool_calls": 0,
        "prose_chars": 0,
        "ui_chars": 0,
        "status": "ACTIVE",
        "phase": "baseline",
        "drops": None,
        "created_at": None,
    }
    row.update(overrides)
    return row


def test_decode_turn_metrics_parses_drops_json():
    """Migration 041's `drops` column had no decoder coverage at all.

    Asserted against the str shape only, because that is what actually
    arrives: no jsonb type codec is registered on the pool, so asyncpg hands
    jsonb back as text. `parse_json` would in fact raise TypeError on an
    already-decoded list (it only short-circuits on dict, and `drops` is an
    array) — so pinning a list shape here would assert behaviour the code
    does not have.
    """
    decoded = decode_chat_turn_metrics(
        _turn_metrics_row(
            drops=(
                '[{"sig": {"op": "add"}, "reason": '
                '"props_validation_failed:Button", "raw": "{\\"op\\":\\"add\\"}"}]'
            )
        )
    )
    assert decoded is not None
    assert decoded.drops is not None and len(decoded.drops) == 1
    # Parsed dicts are coerced into ChatTurnDrop by the schema, not left raw.
    drop = decoded.drops[0]
    assert isinstance(drop, ChatTurnDrop)
    assert drop.reason == "props_validation_failed:Button"
    assert drop.sig == {"op": "add"}
    assert drop.raw == '{"op":"add"}'


# ---------------------------------------------------------------------------
# TurnMetrics — assistant_idx capture + total_ms stamp
# ---------------------------------------------------------------------------


def test_turn_metrics_captures_assistant_idx_from_turn_end():
    m = TurnMetrics(session_id="s", template_id="t", t0=time.monotonic())
    m.observe(
        SSEEvent(
            event="turn_end",
            data={"session_status": "ACTIVE", "assistant_idx": 7},
        )
    )
    assert m.assistant_idx == 7
    assert m.status == "ACTIVE"


def test_turn_metrics_ignores_missing_or_non_int_idx():
    m = TurnMetrics(session_id="s", template_id="t", t0=time.monotonic())
    m.observe(
        SSEEvent(event="turn_end", data={"session_status": "FAILED"})
    )  # no assistant_idx (e.g. failed/canceled turn)
    assert m.assistant_idx is None


def test_turn_metrics_total_ms_stamped_on_emit():
    m = TurnMetrics(session_id="s", template_id="t", t0=time.monotonic())
    assert m.total_ms is None
    m.emit()
    assert isinstance(m.total_ms, float)


# ---------------------------------------------------------------------------
# Phase 2 — aggregate analytics query builders
# ---------------------------------------------------------------------------


def _outer_projection(query: str) -> str:
    """The final SELECT list — the columns the caller actually receives.

    Needed because a whole-query substring search cannot distinguish an alias
    that is exposed from one that is internal to a CTE. The trends query
    re-projects its CTE columns::

        sess AS (... COUNT(*) FILTER (...) AS ended_conversations ...)
        SELECT COALESCE(s.ended_conversations, 0) AS ended_conversations

    so ``"AS ended_conversations" in query`` stays true even if the *outer*
    alias is deleted — leaving the test green while the endpoint raises
    KeyError on ``row["ended_conversations"]``. Verified: that exact mutation
    passed all 20 tests before this helper existed.

    Valid only while the outer SELECT contains no nested SELECT, which callers
    assert via the sanity check on the slice.
    """
    return query.rsplit("SELECT", 1)[-1].split("FROM", 1)[0]


def test_chat_analytics_where_clause_filters_and_placeholders():
    conditions, values = build_chat_analytics_where_clause(
        {"template_id": "tid", "reseller_ids": ["r1"], "status": "ENDED"}
    )
    assert conditions == [
        "cs.template_id = $1::UUID",
        "cs.reseller_id = ANY($2)",
        "cs.status = $3",
    ]
    assert values == ["tid", ["r1"], "ENDED"]


def test_chat_analytics_where_clause_date_to_is_inclusive():
    # date_from = Jun 1, date_to = Jun 2 (inclusive) → the date_to bound is the
    # START of Jun 3, so the span between the two bounds is exactly 2 days.
    conditions, values = build_chat_analytics_where_clause(
        {"date_from": date(2026, 6, 1), "date_to": date(2026, 6, 2)}
    )
    assert conditions == ["cs.created_at >= $1", "cs.created_at < $2"]
    assert all(isinstance(v, datetime) for v in values)
    assert values[1] - values[0] == timedelta(days=2)


def test_chat_analytics_where_clause_empty_filters():
    conditions, values = build_chat_analytics_where_clause({})
    assert conditions == [] and values == []


def test_chat_summary_query_aggregate_shape():
    """Asserts the output CONTRACT (the column aliases the decoder reads), not
    the SQL that produces it.

    The ungrouped summary was rewritten from a LEFT JOIN into per-table CTEs
    that cross-join, deliberately: the joined shape fans out one session row
    per message, which silently weights AVG/percentile by message count. This
    test pinned the pre-rewrite text and so broke on a change that was correct
    — hence aliases here, and no assertions about joins or expressions.
    """
    query, values = get_chat_analytics_summary_query({})
    # Every alias the ungrouped branch of get_chat_analytics reads
    # (app/api/routers/breeze_buddy/analytics/handlers.py). Derived from the
    # consumer, not from the query — a list copied off the query itself would
    # keep passing when both drift together, which is exactly the failure this
    # test exists to catch.
    #
    # A whole-query search is sound *here*, unlike in the trends test: this
    # query's outer projection is `SELECT sess.*, msg.*, turn.*`, so the CTE
    # aliases are literally the returned column names. There is no separate
    # outer alias that could be dropped independently.
    for alias in (
        "AS total_conversations",
        "AS active_conversations",
        "AS idle_conversations",
        "AS ended_conversations",
        "AS user_ended_conversations",
        "AS idle_timeout_conversations",
        "AS total_agents",
        "AS total_messages",
        "AS user_messages",
        "AS assistant_messages",
        "AS avg_session_seconds",
        "AS median_reply_ms",
    ):
        assert alias in query, f"summary query no longer exposes {alias}"
    # Session-level aggregates must not be computed across the message join.
    assert "percentile_cont" in query
    assert values == []


def test_chat_summary_query_group_by_template():
    query, values = get_chat_analytics_summary_query(
        {"template_id": "tid"}, group_by="template"
    )
    assert "cs.template_id::text AS template_id" in query
    assert "GROUP BY cs.template_id" in query
    assert "cs.template_id = $1::UUID" in query
    assert values == ["tid"]
    # The grouped branch reads all of these with a hard `row[...]` subscript,
    # so a dropped alias is a 500 rather than a zero. It has its own SELECT
    # list, so passing the ungrouped test above says nothing about it.
    for alias in (
        "AS total_conversations",
        "AS active_conversations",
        "AS idle_conversations",
        "AS ended_conversations",
        "AS total_messages",
    ):
        assert alias in query, f"grouped summary query no longer exposes {alias}"


def test_chat_trends_query_buckets_by_granularity():
    """Granularity reaches DATE_TRUNC, and only from the whitelist.

    Asserts `DATE_TRUNC('<unit>'` without pinning the column expression: the
    query now buckets sessions and messages separately (messages by their own
    created_at, so a long thread doesn't pile onto its start day), so the unit
    appears twice against two different aliases.
    """
    for granularity in ("day", "week", "month"):
        q, _ = get_chat_analytics_trends_query({}, granularity)
        assert f"DATE_TRUNC('{granularity}'" in q

    qd, _ = get_chat_analytics_trends_query({}, "day")
    # These are the keys the time_granularity branch of get_chat_analytics
    # reads. conversations_started, ended_conversations and time_bucket are
    # read with a hard `row[...]` subscript, so a dropped alias is a KeyError
    # at request time, not a silently-zeroed field.
    #
    # Asserted against the OUTER projection, not the whole query — see
    # _outer_projection for why the difference is load-bearing here.
    projection = _outer_projection(qd)
    assert "WITH" not in projection and len(projection) < len(qd), (
        "outer-SELECT slice failed; the query shape changed and this helper "
        "needs updating rather than silently checking the whole string"
    )
    for alias in (
        "AS time_bucket",
        "AS conversations_started",
        "AS ended_conversations",
        "AS total_messages",
        "AS user_messages",
        "AS assistant_messages",
    ):
        assert alias in projection, f"trends query no longer returns {alias}"


def test_chat_trends_query_ignores_unwhitelisted_granularity():
    """The security property: an unknown unit falls back to 'day' and is never
    interpolated into the SQL.

    Named "ignores" rather than "rejects" deliberately — the builder does not
    raise on an unknown unit, it substitutes 'day'. A name promising rejection
    would imply an exception this code never throws.
    """
    q, _ = get_chat_analytics_trends_query({}, "century")
    assert "DATE_TRUNC('day'" in q
    assert "century" not in q

    injected, _ = get_chat_analytics_trends_query({}, "day'); DROP TABLE x; --")
    assert "DROP TABLE" not in injected
    assert "DATE_TRUNC('day'" in injected
