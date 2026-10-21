"""Deliver a finished transcript through the template's on_stop_hooks.

No destination lives in code. The template says where (``send_http_request``
hooks with url/auth/headers); this module builds the payload from the
meeting and its lines and runs each hook through the existing HTTP executor,
waiting for the response so delivery_state can record what happened.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig
from app.core.logger import logger
from app.schemas.breeze_buddy.meetings import Meeting, MeetingSegment


def build_payload(
    meeting: Meeting, segments: Sequence[MeetingSegment]
) -> Dict[str, Any]:
    """The standard transcript document, also the placeholder vocabulary."""
    lines = [
        {
            "seq": s.seq,
            "speaker": s.speaker,
            "text": s.text,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
        }
        for s in segments
    ]
    transcript_text = "\n".join(
        f"[{_hms(s.start_ms)}] {('Speaker ' + s.speaker + ': ') if s.speaker else ''}{s.text}"
        for s in segments
    )
    return {
        "meeting_id": meeting.id,
        "lead_id": meeting.lead_id,
        "merchant_id": meeting.merchant_id,
        "reseller_id": meeting.reseller_id,
        "state": meeting.state.value,
        "started_at": meeting.started_at.isoformat(),
        "ended_at": meeting.ended_at.isoformat() if meeting.ended_at else None,
        "duration_s": meeting.duration_s,
        "end_reason": meeting.end_reason.value if meeting.end_reason else None,
        "line_count": len(lines),
        "segments": lines,
        "transcript_text": transcript_text,
    }


def _hms(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


async def deliver(
    meeting: Meeting,
    segments: Sequence[MeetingSegment],
    hooks: Sequence[HookConfig],
    session: aiohttp.ClientSession,
) -> Tuple[bool, Optional[str]]:
    """Run every send_http_request hook with the transcript.

    Returns ``(ok, error)``: ok only when every hook answered 2xx. A hook
    without a body gets the standard payload; one with a body has its
    ``{placeholders}`` resolved from the same payload. Hooks of other kinds
    are ignored here (they have no transcript to receive).
    """
    http_hooks = [h for h in hooks if h.name == "send_http_request" and h.http_request]
    if not http_hooks:
        return False, "no send_http_request hook configured"

    payload = build_payload(meeting, segments)
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
        except Exception as e:  # executor is documented not to raise; belt and braces
            result = None
            errors.append(f"{config.url}: {type(e).__name__}: {e}")
            continue
        status = result[0] if result else 0
        if not 200 <= status < 300:
            body = (result[1] if result else "")[:200]
            errors.append(f"{config.url}: HTTP {status} {body}".strip())
    if errors:
        logger.warning(
            f"[MEETING] delivery of {meeting.id} failed: {'; '.join(errors)}"
        )
        return False, "; ".join(errors)[:1000]
    logger.info(f"[MEETING] delivered {meeting.id} via {len(http_hooks)} hook(s)")
    return True, None
