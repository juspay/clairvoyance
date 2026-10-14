"""Inbound request archive for the two ingest doors, switched by two DevCycle
flags and applied by ``IngestArchiveMiddleware`` AHEAD of routing.

    BB_INGEST_ARCHIVE_ENABLED     copy: every POST is copied to GCS in the
                                  background, then runs exactly as today
                                  (auth, Postgres). Used to see objects land
                                  before anything else changes.
    BB_INGEST_DB_WRITES_DISABLED  park: every POST is written to GCS and
                                  answered with the door's normal success
                                  body. No auth, no validation, no Postgres:
                                  nothing behind the middleware runs. If the
                                  GCS write fails, or the body is not a JSON
                                  object, the request runs exactly as today.
                                  Implies copy.

Why ahead of routing: a Postgres restart (the logical-decoding flag) takes
the store away for minutes. The events door's auth itself reads Postgres,
so anything after routing would still answer 5xx, and every producer would
retry into more 5xx. Planned use (dev_only_postgres_wal_night_runbook.html):
copy on, check objects land, park on, restart Postgres, both off, replay.

    gs://INGEST_ARCHIVE_GCS_BUCKET/<copied|parked>/<door>/<YYYY-MM-DD>/<HH>/<stamp>-<uuid>.json

Only ``parked/`` is replayed: ``copied/`` requests also went to Postgres.
Each object holds ``path`` and ``body`` (the replay POSTs body to path),
``auth`` (the header the caller sent, name and value), ``merchant_id``,
``key``, ``received_at`` and ``receipt`` (what the door answered). A parked
request meets the door's auth and validation for the first time at replay,
so one that was never allowed, or never valid, is refused there and never
reaches Postgres. Events dedupe on (merchant_id, source, external_id);
leads do not, so a lead replay looks each request_id up first.

``auth`` is a live credential: anyone who can READ the bucket can act as
the callers in it. The app's service account only creates objects, reads
are for the replay operators, the lifecycle is short, and the value is
never logged.

Fail-safe throughout: an unreadable flag means off, and any archive failure
means the request runs exactly as today.
"""

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Set, Tuple

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import dynamic as dyn_cfg
from app.core.config.static import INGEST_ARCHIVE_GCS_BUCKET
from app.core.logger import logger
from app.services.gcp.storage.client import get_gcs_bucket

LOG_COMPONENT = "ingest.archive"

OFF, COPY, PARK = "off", "copy", "park"
#: Top-level folder per mode; the replay reads ``parked/`` only.
FOLDERS = {COPY: "copied", PARK: "parked"}

#: How long a write may take. nautilus gives the events door 3 s end to end.
UPLOAD_TIMEOUT_S = 2.0
#: The events door refuses bodies over 1 MB (MAX_LETTER_BYTES in
#: app/crm/record/api.py), so a bigger one could never be replayed; it takes
#: the normal path instead. The cap also bounds what an unauthenticated
#: request can make the middleware hold in memory.
MAX_BODY_BYTES = 1 * 1024 * 1024

#: Uploads run on their own small pool, never the loop's shared default
#: executor (BLOCKING_THREAD_POOL_SIZE in app/core/config/static.py), so a
#: slow GCS cannot starve call placement, logins or DNS lookups. An upload
#: still queued when its wait runs out is cancelled before it starts.
UPLOAD_THREADS = 8
_executor = ThreadPoolExecutor(
    max_workers=UPLOAD_THREADS, thread_name_prefix="ingest-archive"
)

_bucket: Any = None
_bucket_resolved = False
_bucket_lock = threading.Lock()
#: Strong references to copy-mode writes: asyncio keeps only weak ones.
_pending: Set["asyncio.Task[bool]"] = set()


@dataclass(frozen=True)
class Door:
    """What the middleware needs to know about one door."""

    name: str  # folder in the bucket
    key_field: str  # body field the object is filed under
    status_code: int  # the door's normal success status
    answer: Callable[[Dict[str, Any]], Dict[str, Any]]  # its normal success body
    prepare: Callable[[Dict[str, Any], datetime], Dict[str, Any]]  # body to keep


def _as_sent(body: Dict[str, Any], received_at: datetime) -> Dict[str, Any]:
    return body


def _pin_occurred_at(body: Dict[str, Any], received_at: datetime) -> Dict[str, Any]:
    """An omitted ``occurred_at`` means receive time on the normal path
    (LEAST($7, now()) in the insert). Pin it, or the replay would stamp
    replay time and shift every timer that measures from arrival."""
    if body.get("occurred_at") is None:
        return {**body, "occurred_at": received_at.isoformat()}
    return body


def _event_answer(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /ingest/events' receipt, marked as parked: no event id exists yet."""
    return {"id": None, "duplicate": False, "archived": True}


def _lead_answer(body: Dict[str, Any]) -> Dict[str, Any]:
    """push_lead_handler's body, key for key. The tracker id is PROVISIONAL:
    the replay creates the lead under its own id, so until then GET
    /leads/{id} is 404 and an abort answers "Lead not found". The object's
    receipt keeps this id; the runbook reconciles it by request_id."""
    return {
        "status": "queued",
        "lead_call_tracker_id": str(uuid.uuid4()),
        "order_id": body.get("request_id"),
        "message": "Call request added to queue for processing",
    }


#: The two doors, matched exactly on POST. The events router is mounted under
#: /ingest (app/crm/api.py), the lead router under /agent/voice/breeze-buddy
#: (app/main.py); tests/services/test_ingest_archive.py pins both against
#: the real app's routes.
DOORS: Dict[str, Door] = {
    "/ingest/events": Door(
        name="ingest-events",
        key_field="external_id",
        status_code=200,
        answer=_event_answer,
        prepare=_pin_occurred_at,
    ),
    "/agent/voice/breeze-buddy/leads": Door(
        name="buddy-leads",
        key_field="request_id",
        status_code=201,
        answer=_lead_answer,
        prepare=_as_sent,
    ),
}


async def _flag(read: Callable[[], Awaitable[bool]]) -> bool:
    try:
        return bool(await read())
    except Exception as e:  # noqa: BLE001 - unreadable flag = off
        logger.bind(component=LOG_COMPONENT).error(
            f"archive flag unreadable, treated as off: {e}"
        )
        return False


async def archive_mode() -> str:
    """PARK if Postgres writes are disabled (which implies the archive),
    else COPY if the archive is on, else OFF."""
    if await _flag(dyn_cfg.BB_INGEST_DB_WRITES_DISABLED):
        return PARK
    if await _flag(dyn_cfg.BB_INGEST_ARCHIVE_ENABLED):
        return COPY
    return OFF


def presented_auth(headers: Mapping[str, str]) -> Optional[Dict[str, str]]:
    """The credential exactly as the caller sent it: one header, name and
    value, so the replay sends the same header and the door verifies the
    same token. Precedence matches app/crm/auth.py ``_extract_token``
    (bearer Authorization, the s2s headers, then a raw Authorization);
    tests/services/test_ingest_archive.py pins the two together."""
    auth = headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return {"header": "authorization", "value": auth}
    for name in ("x-s2s-token", "x-webhook-token", "x-merchant-token"):
        value = headers.get(name)
        if value:
            return {"header": name, "value": value}
    if auth:
        return {"header": "authorization", "value": auth}
    return None


def object_name(mode: str, door: str, received_at: datetime) -> str:
    """One object per request, grouped by mode, door, day and hour (UTC), so
    a replay can list exactly the window the flag was on."""
    stamp = received_at.strftime("%Y%m%dT%H%M%S%fZ")
    return (
        f"{FOLDERS[mode]}/{door}/{received_at:%Y-%m-%d}/{received_at:%H}/"
        f"{stamp}-{uuid.uuid4().hex}.json"
    )


def _resolve_bucket() -> Any:
    """The bucket, resolved once per process. Credentials and the bucket
    name are env vars, so a miss needs a redeploy anyway; remembering it
    keeps a misconfiguration to one error line. Under a lock, and marked
    resolved only after the attempt, so a write arriving while the first
    resolve is still building the client waits for it."""
    global _bucket, _bucket_resolved
    with _bucket_lock:
        if not _bucket_resolved:
            if INGEST_ARCHIVE_GCS_BUCKET:
                _bucket = get_gcs_bucket(INGEST_ARCHIVE_GCS_BUCKET)
            else:
                logger.bind(component=LOG_COMPONENT).error(
                    "an ingest archive flag is on but INGEST_ARCHIVE_GCS_BUCKET "
                    "is not set — nothing is archived"
                )
            _bucket_resolved = True
    return _bucket


def _upload(name: str, data: str) -> bool:
    """Blocking GCS write on the archive pool. False = no bucket. Bounded,
    with no retries: the library default (60 s per attempt plus a 120 s
    retry deadline) would hold the thread long after the door gave up."""
    bucket = _resolve_bucket()
    if bucket is None:
        return False
    bucket.blob(name).upload_from_string(
        data,
        content_type="application/json",
        timeout=UPLOAD_TIMEOUT_S,
        retry=None,
    )
    return True


async def _write(record: Dict[str, Any], name: str) -> bool:
    """Write one object; True only when it is in the bucket."""
    log = logger.bind(
        component=LOG_COMPONENT,
        mode=record["mode"],
        door=record["door"],
        merchant_id=record["merchant_id"],
        key=record["key"],
    )
    try:
        loop = asyncio.get_running_loop()
        stored = await asyncio.wait_for(
            loop.run_in_executor(
                _executor, _upload, name, json.dumps(record, default=str)
            ),
            timeout=UPLOAD_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.error(f"archive write exceeded {UPLOAD_TIMEOUT_S}s: {name}")
        return False
    except Exception as e:  # noqa: BLE001 - a failed write is a False
        log.error(f"archive write failed for {name}: {e}")
        return False
    if stored:
        # Names the object only: the record holds a credential.
        log.info(f"archived to gs://{INGEST_ARCHIVE_GCS_BUCKET}/{name}")
    return stored


def _record(
    mode: str,
    door: Door,
    path: str,
    received_at: datetime,
    auth: Optional[Dict[str, str]],
    body: Any,
    receipt: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    fields = body if isinstance(body, dict) else {}
    return {
        "mode": mode,
        "door": door.name,
        "method": "POST",
        "path": path,
        "received_at": received_at.isoformat(),
        "merchant_id": fields.get("merchant_id"),
        "key": str(fields.get(door.key_field, "")),
        "auth": auth,
        "body": body,
        "receipt": receipt,
    }


def _copy(door: Door, path: str, raw: bytes, auth: Optional[Dict[str, str]]) -> None:
    """Copy mode: schedule the write and return at once; the request runs
    as today whatever happens to the copy."""
    try:
        body: Any = json.loads(raw)
    except ValueError:
        body = raw.decode("utf-8", "replace")
    received_at = datetime.now(timezone.utc)
    record = _record(COPY, door, path, received_at, auth, body, None)
    task = asyncio.create_task(
        _write(record, object_name(COPY, door.name, received_at))
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _park(
    door: Door, path: str, raw: bytes, auth: Optional[Dict[str, str]]
) -> Optional[JSONResponse]:
    """Park mode: the door's success answer once the object is in the
    bucket, or None and the request runs as today. A body that is not a
    JSON object is never parked: the route answers it with its own 422,
    before any auth or database work."""
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    received_at = datetime.now(timezone.utc)
    body = door.prepare(body, received_at)
    answer = door.answer(body)
    record = _record(PARK, door, path, received_at, auth, body, answer)
    if await _write(record, object_name(PARK, door.name, received_at)):
        return JSONResponse(status_code=door.status_code, content=answer)
    return None


async def _buffer(
    receive: Receive, limit: int
) -> Tuple[List[Message], Optional[bytes]]:
    """Read the request body. Returns the messages read (to hand on intact)
    and the body, or None when it is larger than ``limit`` or the client
    went away."""
    messages: List[Message] = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            return messages, None
        size += len(message.get("body", b""))
        if size > limit:
            return messages, None
        if not message.get("more_body", False):
            return messages, b"".join(m.get("body", b"") for m in messages)


def _pass_body_on(messages: List[Message], receive: Receive) -> Receive:
    """A body can be read only once. The middleware has already read it, so
    the route gets this instead: what ``_buffer`` read, then the rest."""
    pending = list(messages)

    async def receive_again() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return receive_again


class IngestArchiveMiddleware:
    """Applies ``archive_mode()`` to POSTs on the two doors in ``DOORS``,
    ahead of routing, validation and auth. Any other request passes straight
    through untouched; a door POST with both flags off costs two flag reads."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        door = None
        path = scope.get("path", "")
        if scope["type"] == "http" and scope.get("method") == "POST":
            door = DOORS.get(path)
        if door is None:
            await self.app(scope, receive, send)
            return
        mode = await archive_mode()
        if mode == OFF:
            await self.app(scope, receive, send)
            return

        messages, raw = await _buffer(receive, MAX_BODY_BYTES)
        downstream = _pass_body_on(messages, receive)
        if raw is not None:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            auth = presented_auth(headers)
            try:
                if mode == PARK:
                    response = await _park(door, path, raw, auth)
                    if response is not None:
                        await response(scope, downstream, send)
                        return
                else:
                    _copy(door, path, raw, auth)
            except Exception as e:  # noqa: BLE001 - never break the door
                logger.bind(component=LOG_COMPONENT, door=door.name).error(
                    f"archive skipped, request runs as today: {e}"
                )
        await self.app(scope, downstream, send)
