"""Tests for the chat conversational-log analytics layer (Phase 1).

Covers the DB-free pieces:
- ``list_chat_sessions_query`` / ``count_chat_sessions_query`` filter +
  placeholder construction (admin vs scoped, all filters).
- ``record_chat_turn_metrics_query`` shape (upsert + 14 params).
- ``decode_chat_session_summary`` (preview truncation, message_count default)
  and ``decode_chat_turn_metrics``.
- ``TurnMetrics`` capturing ``assistant_idx`` from the turn_end event +
  stamping ``total_ms`` at emit.
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
from app.schemas.breeze_buddy.chat import ChatSessionStatus, WidgetChannel

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
    assert len(values) == 14
    assert values[0] == "sess" and values[1] == 4
    assert values[-2] == "ACTIVE" and values[-1] == "baseline"


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
        "created_at": None,
    }
    metrics = decode_chat_turn_metrics(row)
    assert metrics is not None
    assert metrics.session_id == "sess" and metrics.idx == 4
    assert metrics.total_ms == 120.0
    assert metrics.ui_ops == 3 and metrics.ui_dropped == 1


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
    query, values = get_chat_analytics_summary_query({})
    assert "COUNT(DISTINCT cs.id) AS total_conversations" in query
    assert "COUNT(m.session_id) AS total_messages" in query
    assert "COUNT(DISTINCT cs.template_id) AS total_agents" in query
    assert "LEFT JOIN chat_message m ON m.session_id = cs.id" in query
    assert "GROUP BY" not in query
    assert values == []


def test_chat_summary_query_group_by_template():
    query, values = get_chat_analytics_summary_query(
        {"template_id": "tid"}, group_by="template"
    )
    assert "cs.template_id::text AS template_id" in query
    assert "GROUP BY cs.template_id" in query
    assert "cs.template_id = $1::UUID" in query
    assert values == ["tid"]


def test_chat_trends_query_buckets_by_granularity():
    qd, _ = get_chat_analytics_trends_query({}, "day")
    assert "DATE_TRUNC('day', cs.created_at) AS time_bucket" in qd
    assert "COUNT(*) AS conversations_started" in qd
    qw, _ = get_chat_analytics_trends_query({}, "week")
    assert "DATE_TRUNC('week', cs.created_at)" in qw
    qm, _ = get_chat_analytics_trends_query({}, "month")
    assert "DATE_TRUNC('month', cs.created_at)" in qm
    # Unknown granularity falls back to day (no raw interpolation).
    qx, _ = get_chat_analytics_trends_query({}, "century")
    assert "DATE_TRUNC('day', cs.created_at)" in qx
