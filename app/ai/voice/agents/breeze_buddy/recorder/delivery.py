"""Deliver a finished transcript through the template's on_stop_hooks.

No destination lives in code. The template says where (``send_http_request``
hooks with url/auth/headers); this builds the payload from the record and
runs each hook through the existing HTTP executor, waiting for the response
so the outcome can be logged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.recorder.session import MeetingRecord
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig
from app.core.logger import logger


def _hms(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def build_payload(record: MeetingRecord) -> Dict[str, Any]:
    """The standard transcript document, also the placeholder vocabulary."""
    lines = [{"seq": i, **line.as_dict()} for i, line in enumerate(record.lines)]
    transcript_text = "\n".join(
        f"[{_hms(line.start_ms)}] "
        f"{('Speaker ' + line.speaker + ': ') if line.speaker else ''}{line.text}"
        for line in record.lines
    )
    return {
        "meeting_id": record.meeting_id,
        "lead_id": record.lead_id,
        "merchant_id": record.merchant_id,
        "reseller_id": record.reseller_id,
        "started_at": record.started_at.isoformat(),
        "ended_at": record.ended_at.isoformat(),
        "duration_s": record.duration_s,
        "end_reason": record.end_reason,
        "line_count": len(lines),
        "segments": lines,
        "transcript_text": transcript_text,
    }


async def deliver(
    record: MeetingRecord,
    hooks: Sequence[HookConfig],
    session: aiohttp.ClientSession,
) -> Tuple[bool, Optional[str]]:
    """Run every send_http_request hook with the transcript.

    Returns ``(ok, error)``: ok only when every hook answered 2xx. A hook
    without a body gets the standard payload; one with a body has its
    ``{placeholders}`` resolved from the same payload.
    """
    http_hooks = [h for h in hooks if h.name == "send_http_request" and h.http_request]
    if not http_hooks:
        return False, "no send_http_request hook configured"

    payload = build_payload(record)
    executor = HttpRequestExecutor(session=session)
    errors: List[str] = []
    for hook in http_hooks:
        config = hook.http_request
        assert config is not None
        if config.body is None:
            config = config.model_copy(update={"body": payload})
        try:
            result = await executor.execute(
                config=config, resolved_fields=payload, fire_and_forget=False
            )
        except Exception as e:
            errors.append(f"{config.url}: {type(e).__name__}: {e}")
            continue
        status = result[0] if result else 0
        if not 200 <= status < 300:
            body = (result[1] if result else "")[:200]
            errors.append(f"{config.url}: HTTP {status} {body}".strip())
    if errors:
        logger.warning(
            f"[MEETING] delivery of {record.meeting_id} failed: {'; '.join(errors)}"
        )
        return False, "; ".join(errors)[:1000]
    logger.info(
        f"[MEETING] delivered {record.meeting_id} via {len(http_hooks)} hook(s)"
    )
    return True, None
