"""Database access for durable topic analysis."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.conversation_analysis import (
    claim_analysis_by_id_query,
    complete_analysis_query,
    create_chat_analysis_query,
    create_voice_analysis_query,
    fail_analysis_query,
    get_analysis_transcript_query,
    get_topic_conversations_query,
    get_topic_dashboard_query,
    get_topics_for_source_query,
)


async def create_voice_analysis(lead_id: str) -> Optional[str]:
    query, values = create_voice_analysis_query(lead_id)
    rows = await run_parameterized_query(query, values)
    return str(rows[0]["id"]) if rows else None


async def create_chat_analysis(session_id: str) -> Optional[str]:
    query, values = create_chat_analysis_query(session_id)
    rows = await run_parameterized_query(query, values)
    return str(rows[0]["id"]) if rows else None


def _decode_topics(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            logger.error("Corrupt topics JSON in topic_result")
            return []
    return [dict(topic) for topic in value or [] if isinstance(topic, dict)]


async def claim_analysis_by_id(analysis_id: str) -> Optional[Dict[str, Any]]:
    query, values = claim_analysis_by_id_query(analysis_id)
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None


async def get_analysis_transcript(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    query, values = get_analysis_transcript_query(job["channel"], job["source_id"])
    rows = await run_parameterized_query(query, values)
    if not rows or not rows[0]["transcript"]:
        return []
    transcript = rows[0]["transcript"]
    if isinstance(transcript, str):
        transcript = json.loads(transcript)
    return [dict(turn) for turn in transcript if isinstance(turn, dict)]


async def complete_analysis(
    analysis_id: str,
    topics: List[Dict[str, Any]],
) -> None:
    query, values = complete_analysis_query(analysis_id, json.dumps({"topics": topics}))
    await run_parameterized_query(query, values)


async def fail_analysis(analysis_id: str, error_message: str) -> None:
    query, values = fail_analysis_query(analysis_id, error_message)
    await run_parameterized_query(query, values)


async def get_topic_dashboard(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    query, values = get_topic_dashboard_query(filters)
    rows = await run_parameterized_query(query, values)
    return [dict(row) for row in rows or []]


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
    channel: str,
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """Return extracted topics within the caller's tenant scope."""
    query, values = get_topics_for_source_query(
        source_id,
        channel,
        reseller_ids,
        merchant_ids,
    )
    rows = await run_parameterized_query(query, values)
    return _decode_topics(rows[0]["topics"]) if rows else []
