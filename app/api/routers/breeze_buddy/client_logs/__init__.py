"""Browser log ingestion (loom frontend -> backend log stream).

Route under ``/agent/voice/breeze-buddy``:

- ``POST /client-logs``   accept a batch of browser log entries (202)

Auth: RBAC bearer token — logged-in users only. Identity (user id,
username, role, merchant ids), client IP, receive time and
``source="loom"`` are stamped from the JWT and the request, never read
from the body (``extra="forbid"``). Levels map onto
{DEBUG, INFO, WARNING, ERROR}; a client can never reach CRITICAL.

CLIENT CONTRACT (the loom half is written against this):
- Any non-2xx means: drop the buffer and DO NOT log the failure. loom's
  ``reportApiFailure`` returns early for this path; that early return is
  what lets this endpoint reject bad input with a 422 instead of quietly
  repairing it. Remove it and the first failure becomes an unbounded loop.
- Validation is therefore strict, not lenient. The frontend already
  redacts and caps every field, so this endpoint repeats none of it.
- The pagehide flush uses ``fetch(..., {keepalive: true})`` (a beacon
  cannot set ``Authorization``); keepalive caps the body at 64 KiB,
  which is exactly this endpoint's body cap.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api.routers.breeze_buddy.client_logs.handlers import handle_client_log_batch
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.client_logs import (
    MAX_BODY_BYTES,
    ClientLogBatch,
    ClientLogIngestResponse,
)

router = APIRouter()


def _body_errors(exc: ValidationError) -> List[Dict[str, Any]]:
    """Re-shape manual-validation errors to FastAPI's native 422 format.

    Prefix ``loc`` with ``body``, as FastAPI's own request validation
    does. ``ctx`` and ``input`` are dropped rather than echoed: their
    values are attacker-controlled and not always JSON-encodable, and a
    raw-bytes or huge-int echo turned this 422 into a 500.
    """
    return [
        {
            **{k: v for k, v in err.items() if k not in ("ctx", "input")},
            "loc": ("body", *err["loc"]),
        }
        for err in exc.errors(include_url=False)
    ]


@router.post(
    "/client-logs",
    response_model=ClientLogIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ship a batch of browser log entries into the backend log stream",
)
async def ingest_client_logs(
    request: Request,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> ClientLogIngestResponse:
    """Accept a batch of browser log entries and re-emit them via loguru.

    The body is read manually (not as a declared Pydantic parameter) so
    the size check runs BEFORE FastAPI buffers and parses an oversized
    payload. Browsers always send Content-Length for a string body, so
    the header check is sufficient here.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Log batch too large",  # static: never echo client data
        )
    # Stream with a running cap: a chunked request carries no
    # Content-Length, and ``await request.body()`` would buffer it
    # without bound before any length check could run.
    chunks: List[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Log batch too large",
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        batch = ClientLogBatch.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(_body_errors(exc)) from None
    return await handle_client_log_batch(request, batch, current_user)
