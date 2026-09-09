"""Ingest browser logs and re-emit them through the backend logger.

Trust posture: the request body is fully attacker-controlled and lands
verbatim in the production log stream. The loom frontend redacts and
caps it before sending; this file does the stamping, the level mapping,
and the emission.

This handler still never returns a 5xx. The frontend already declines to
report failures from this path (loom logger.ts, ``reportApiFailure``), so
a 5xx no longer loops — but one bad entry silently costing the other 49
is a worse trade than reporting it in ``dropped``.
"""

import uuid
from datetime import datetime, timezone

from fastapi import Request

from app.api.routers.breeze_buddy.widget_common import client_ip
from app.core.logger import logger
from app.core.logger.context import clear_log_context, set_log_context
from app.schemas import UserInfo
from app.schemas.breeze_buddy.client_logs import (
    ClientLogBatch,
    ClientLogEntry,
    ClientLogIngestResponse,
    ClientLogLevel,
)

# Static map keyed by the enum. No client string ever reaches loguru's
# level parameter, and the enum has no CRITICAL member — two layers
# between a browser and a paged on-call.
_LEVEL_MAP: dict[ClientLogLevel, str] = {
    ClientLogLevel.DEBUG: "DEBUG",
    ClientLogLevel.INFO: "INFO",
    ClientLogLevel.WARN: "WARNING",
    ClientLogLevel.ERROR: "ERROR",
}

_MAX_USER_AGENT_CHARS = 300


def _emit_entry(
    entry: ClientLogEntry,
    index: int,
    *,
    batch: ClientLogBatch,
    received_at: str,
    user_agent: str | None,
) -> None:
    """Re-emit ONE browser entry through loguru.

    ``bind()`` (not ``update_log_context``) for per-entry fields: it
    returns an independent logger, so entry N's url can never leak onto
    entry N+1.

    ``fe_context`` is bound as a single NESTED key. Spreading the client
    dict into extra (``logger.bind(**entry.context)``) would let it
    overwrite json_sink's reserved top-level keys (level, message, ...)
    and forge a CRITICAL record — that spread is the bug this comment
    exists to prevent.
    """
    fields = {
        "fe_channel": entry.channel,
        "fe_url": entry.url,
        "fe_client_ts": entry.client_ts,
        # Ships as one string. json_sink runs json.dumps, which escapes the
        # newlines, so no sink ever sees an embedded line break.
        "fe_stack": entry.stack,
        "fe_context": entry.context,
        "fe_entry_index": index,
        "fe_received_at": received_at,
        "fe_user_agent": user_agent,
        "fe_session_id": batch.session_id,
    }
    # json_sink filters None only for log-context keys, not for bind()
    # extras, so unset fields would print as literal nulls on every line.
    # No format args on purpose: loguru only runs str.format when
    # args/kwargs are passed, so braces in the message stay literal.
    logger.bind(**{k: v for k, v in fields.items() if v is not None}).log(
        _LEVEL_MAP[entry.level], entry.message
    )


async def handle_client_log_batch(
    request: Request, batch: ClientLogBatch, current_user: UserInfo
) -> ClientLogIngestResponse:
    """Stamp identity from the JWT and re-emit every entry.

    Identity fields are NEVER read from the body (extra="forbid" on the
    models rejects an attempt); the verified token is the only source.
    """
    batch_id = uuid.uuid4().hex
    received_at = datetime.now(timezone.utc).isoformat()
    user_agent = request.headers.get("user-agent", "")[:_MAX_USER_AGENT_CHARS] or None

    # Request-scoped identity via the mandated entrypoint convention —
    # the loguru patcher injects it into every record in this request,
    # including this handler's own operational lines.
    set_log_context(
        component="frontend.logs.ingest",
        source="loom",
        fe_user_id=current_user.id,
        fe_username=current_user.username,
        fe_role=current_user.role.value,
        fe_merchant_ids=",".join(current_user.merchant_ids[:10]) or None,
        fe_client_ip=client_ip(request),
        fe_batch_id=batch_id,
    )
    try:
        accepted = 0
        for index, entry in enumerate(batch.entries):
            try:
                _emit_entry(
                    entry,
                    index,
                    batch=batch,
                    received_at=received_at,
                    user_agent=user_agent,
                )
                accepted += 1
            except Exception as exc:  # noqa: BLE001
                # One bad entry must not fail the batch, and must not
                # produce a 5xx the frontend would log and re-ship.
                # source="clairvoyance" so an ingest failure is never
                # mistaken for a frontend event.
                logger.bind(source="clairvoyance").error(
                    f"client_logs: entry {index} failed to emit: {exc!r}"
                )
        return ClientLogIngestResponse(
            accepted=accepted, dropped=len(batch.entries) - accepted
        )
    finally:
        clear_log_context()
