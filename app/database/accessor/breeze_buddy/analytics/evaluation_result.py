"""Topic analytics access for evaluation_result."""

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.analytics.evaluation_result import (
    get_topic_conversations_query,
    get_topic_dashboard_rows_query,
    get_topics_for_source_query,
)


def _decode_topics(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            logger.error("Corrupt topics JSON in evaluation_result")
            return []
    return [dict(topic) for topic in value or [] if isinstance(topic, dict)]


def _topic_counts(
    topic_rows: List[Dict[str, Any]], current_start: date
) -> Dict[tuple[str, str], Dict[str, Any]]:
    counts: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in topic_rows:
        started_at = row["started_at"]
        if started_at.date() < current_start:
            continue
        key = (str(row["template_id"]), str(row["raw_topic_type"]))
        label = str(row.get("raw_label") or row["raw_topic_type"])
        count = counts.setdefault(
            key,
            {"sources": set(), "first_seen_at": started_at, "label": label},
        )
        count["sources"].add(str(row["source_id"]))
        count["first_seen_at"] = min(count["first_seen_at"], started_at)
        count["label"] = min(count["label"], label)

    by_template: Dict[str, List[tuple[str, Dict[str, Any]]]] = {}
    for (template_id, topic_type), count in counts.items():
        by_template.setdefault(template_id, []).append((topic_type, count))
    for topics in by_template.values():
        topics.sort(
            key=lambda item: (
                -len(item[1]["sources"]),
                item[1]["first_seen_at"],
                item[0],
            )
        )
        for rank, (_, count) in enumerate(topics, 1):
            count["rank"] = rank
    return counts


def _mapped_topic(
    row: Dict[str, Any], counts: Dict[tuple[str, str], Dict[str, Any]]
) -> tuple[str, str, int, bool]:
    count = counts.get((str(row["template_id"]), str(row["raw_topic_type"])))
    if count and count["rank"] <= 10:
        return str(row["raw_topic_type"]), str(count["label"]), count["rank"], False
    return "__other__", "Other", 11, True


def _summary_rows(
    topic_rows: List[Dict[str, Any]],
    counts: Dict[tuple[str, str], Dict[str, Any]],
    current_start: date,
) -> List[Dict[str, Any]]:
    totals: Dict[tuple[str, str], set[str]] = {}
    grouped: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    for row in topic_rows:
        period = "current" if row["started_at"].date() >= current_start else "previous"
        template_id = str(row["template_id"])
        source_id = str(row["source_id"])
        totals.setdefault((period, template_id), set()).add(source_id)
        topic_type, label, rank, is_other = _mapped_topic(row, counts)
        summary = grouped.setdefault(
            (period, template_id, topic_type),
            {
                "result_type": "summary",
                "period": period,
                "template_id": template_id,
                "template_name": row["template_name"],
                "topic_type": topic_type,
                "label": label,
                "rank": rank,
                "is_other": is_other,
                "underlying_topic_types": set(),
                "source_ids": set(),
            },
        )
        summary["underlying_topic_types"].add(str(row["raw_topic_type"]))
        summary["source_ids"].add(source_id)

    results = []
    for (period, template_id, _), summary in grouped.items():
        underlying = sorted(summary.pop("underlying_topic_types"))
        conversation_count = len(summary.pop("source_ids"))
        total = len(totals[(period, template_id)])
        results.append(
            {
                **summary,
                "underlying_topic_types": underlying,
                "underlying_topic_count": len(underlying),
                "conversation_count": conversation_count,
                "conversation_share": round(conversation_count * 100 / total, 2),
            }
        )
    return sorted(
        results,
        key=lambda row: (
            row["period"],
            row["template_name"],
            row["rank"],
            row["topic_type"],
        ),
    )


def _trend_rows(
    topic_rows: List[Dict[str, Any]],
    counts: Dict[tuple[str, str], Dict[str, Any]],
    current_start: date,
) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, datetime, str], Dict[str, Any]] = {}
    for row in topic_rows:
        started_at = row["started_at"]
        if started_at.date() < current_start:
            continue
        template_id = str(row["template_id"])
        topic_type, label, _, _ = _mapped_topic(row, counts)
        time_bucket = started_at.replace(hour=0, minute=0, second=0, microsecond=0)
        trend = grouped.setdefault(
            (template_id, time_bucket, topic_type),
            {
                "result_type": "trend",
                "period": "current",
                "time_bucket": time_bucket,
                "template_id": template_id,
                "template_name": row["template_name"],
                "topic_type": topic_type,
                "label": label,
                "source_ids": set(),
            },
        )
        trend["source_ids"].add(str(row["source_id"]))

    results = []
    for trend in grouped.values():
        conversation_count = len(trend.pop("source_ids"))
        results.append({**trend, "conversation_count": conversation_count})
    return sorted(
        results,
        key=lambda row: (
            row["template_name"],
            row["time_bucket"],
            row["topic_type"],
        ),
    )


async def get_topic_dashboard(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    query, values = get_topic_dashboard_rows_query(filters)
    rows = await run_parameterized_query(query, values)
    topic_rows = [dict(row) for row in rows or []]
    # ponytail: aggregate bounded dashboard rows here; move back to SQL if
    # production result volume makes transfer or memory cost material.
    counts = _topic_counts(topic_rows, filters["date_from"])
    return _summary_rows(topic_rows, counts, filters["date_from"]) + _trend_rows(
        topic_rows, counts, filters["date_from"]
    )


async def get_topic_conversations(
    filters: Dict[str, Any],
    limit: int,
    cursor_started_at: Optional[datetime] = None,
    cursor_id: Optional[UUID] = None,
) -> List[Dict[str, Any]]:
    query, values = get_topic_conversations_query(
        filters, limit, cursor_started_at, cursor_id
    )
    rows = await run_parameterized_query(query, values)
    results = [dict(row) for row in rows or []]
    for result in results:
        result["topics"] = _decode_topics(result.get("topics"))
    return results


async def get_topics_for_source(
    source_id: str,
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """Return extracted topics within the caller's tenant scope."""
    query, values = get_topics_for_source_query(
        source_id,
        reseller_ids,
        merchant_ids,
    )
    rows = await run_parameterized_query(query, values)
    return _decode_topics(rows[0]["topics"]) if rows else []
