"""Persist bounded Guardrail session summaries in ``evaluation_result``."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Optional

from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_result import (
    merge_evaluation_result,
    set_evaluation_result_status,
)
from app.schemas.breeze_buddy.conversation_analysis import EvaluationType

from .metrics import GuardrailSessionMetrics

_RESULT_TYPE = "SESSION_SUMMARY"
_MAX_EVIDENCE_TURNS = 20
_MAX_CONFIGURATION_SEGMENTS = 20
_PROCESSING = "PROCESSING"
_COMPLETED = "COMPLETED"


def _direction(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _merge_direction(existing: Any, delta: Any) -> dict[str, Any]:
    left = _direction(existing)
    right = _direction(delta)
    reasons: Counter[str] = Counter()
    for source in (left.get("reason_counts"), right.get("reason_counts")):
        if isinstance(source, dict):
            for key, value in source.items():
                if isinstance(key, str) and isinstance(value, int) and value > 0:
                    reasons[key] += value

    evidence: list[int] = []
    for source in (left.get("evidence_turns"), right.get("evidence_turns")):
        if not isinstance(source, list):
            continue
        for value in source:
            if len(evidence) >= _MAX_EVIDENCE_TURNS:
                break
            if isinstance(value, int) and value not in evidence:
                evidence.append(value)

    def integer(key: str, source: dict[str, Any]) -> int:
        value = source.get(key, 0)
        return value if isinstance(value, int) and value >= 0 else 0

    def number(key: str, source: dict[str, Any]) -> float:
        value = source.get(key, 0)
        return float(value) if isinstance(value, (int, float)) and value >= 0 else 0.0

    return {
        "evaluated": integer("evaluated", left) + integer("evaluated", right),
        "allowed": integer("allowed", left) + integer("allowed", right),
        "blocked": integer("blocked", left) + integer("blocked", right),
        "failed_closed": integer("failed_closed", left)
        + integer("failed_closed", right),
        "total_latency_ms": round(
            number("total_latency_ms", left) + number("total_latency_ms", right),
            3,
        ),
        "max_latency_ms": round(
            max(
                number("max_latency_ms", left),
                number("max_latency_ms", right),
            ),
            3,
        ),
        "reason_counts": dict(sorted(reasons.items())),
        "evidence_turns": evidence,
    }


def merge_session_summary(
    existing: Optional[dict[str, Any]], delta: dict[str, Any]
) -> dict[str, Any]:
    current = existing or {}
    config_id = delta.get("evaluation_config_id")
    config_revision = delta.get("configuration_revision")
    template_id = delta.get("template_id")
    configurations = [
        dict(item)
        for item in current.get("configurations", [])
        if isinstance(item, dict)
    ]
    matching = next(
        (
            item
            for item in configurations
            if item.get("evaluation_config_id") == config_id
            and item.get("configuration_revision") == config_revision
            and item.get("template_id") == template_id
        ),
        None,
    )
    segment = {
        "evaluation_config_id": config_id,
        "configuration_revision": config_revision,
        "template_id": template_id,
        "focus_enabled": bool(
            (matching or {}).get("focus_enabled") or delta.get("focus_enabled")
        ),
        "input": _merge_direction((matching or {}).get("input"), delta.get("input")),
        "output": _merge_direction((matching or {}).get("output"), delta.get("output")),
    }
    truncated_segments = current.get("configuration_segments_truncated", 0)
    if not isinstance(truncated_segments, int) or truncated_segments < 0:
        truncated_segments = 0
    if matching is None:
        configurations.append(segment)
        if len(configurations) > _MAX_CONFIGURATION_SEGMENTS:
            configurations = configurations[-_MAX_CONFIGURATION_SEGMENTS:]
            truncated_segments += 1
    else:
        matching.update(segment)

    return {
        "type": _RESULT_TYPE,
        "schema_version": 1,
        "channel": delta["channel"],
        "focus_enabled": bool(
            current.get("focus_enabled") or delta.get("focus_enabled")
        ),
        "input": _merge_direction(current.get("input"), delta.get("input")),
        "output": _merge_direction(current.get("output"), delta.get("output")),
        "configurations": configurations,
        "configuration_segments_truncated": truncated_segments,
    }


async def persist_guardrail_metrics(
    metrics: Optional[GuardrailSessionMetrics],
    *,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    started_at: datetime,
    status: str = _COMPLETED,
) -> None:
    """Merge one in-memory batch into its conversation-owned summary row.

    The accessor holds a transaction-scoped PostgreSQL advisory lock for the
    result identity, so the first insert and later merges remain atomic even if
    a caller bypasses the normal per-session Redis lock.
    """
    if metrics is None or not metrics.should_persist:
        return
    try:
        await merge_evaluation_result(
            metrics.evaluation_config_id,
            EvaluationType.GUARDRAIL.value,
            source_id,
            reseller_id,
            merchant_id,
            metrics.template_id,
            started_at,
            _RESULT_TYPE,
            metrics.as_result(),
            merge_session_summary,
            status,
        )
    except Exception as exc:
        # Analytics must never weaken or interrupt Guardrail enforcement.
        logger.warning(
            f"Could not persist Guardrail metrics for source {source_id}: {exc}"
        )


async def finalize_guardrail_metrics(source_id: str) -> None:
    """Mark an existing chat summary complete when its session terminates."""
    try:
        await set_evaluation_result_status(
            source_id,
            EvaluationType.GUARDRAIL.value,
            _RESULT_TYPE,
            _COMPLETED,
        )
    except Exception as exc:
        logger.warning(
            f"Could not finalize Guardrail metrics for source {source_id}: {exc}"
        )


__all__ = [
    "finalize_guardrail_metrics",
    "merge_session_summary",
    "persist_guardrail_metrics",
]
