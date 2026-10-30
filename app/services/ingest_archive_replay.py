"""Replay archived ``POST /ingest/events`` requests from GCS into Postgres.

The archive (app/services/ingest_archive.py) keeps each request as one JSON
object: the body the merchant sent, the path it went to, and the auth header
it carried. A replay lists the event objects of one folder (``parked`` or
``copied``) whose arrival time falls in ``[start, end)``, and POSTs each body
to its path on THIS pod (``127.0.0.1:PORT``) with its own stored header, one
by one, oldest first. The door then runs exactly as for a live push: the
merchant's auth, validation, and the dedupe on (merchant_id, source,
external_id) — so replaying an event that already landed answers
``duplicate: true`` and stores nothing.

Leads are not replayed here: ``/leads`` does not dedupe, so they are
reconciled by hand (see dev_only_postgres_wal_night_runbook.html).

A run is started by ``POST /admin/ingest-archive/replay`` and goes on in the
background: the ingress gives a request 5 s, a window can hold thousands of
objects. Progress and the final summary are logged under
``ingest.archive.replay``, and the report (counts plus every refusal and
failure, never a token) is written to ``replay-reports/<run_id>.json`` in the
same bucket.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.core.config.static import INGEST_ARCHIVE_GCS_BUCKET, PORT
from app.core.logger import logger
from app.services.ingest_archive import FOLDERS, PARK, _resolve_bucket, archive_mode

LOG_COMPONENT = "ingest.archive.replay"

#: The only door replayed; its folder name in the bucket.
EVENTS_DOOR = "ingest-events"
#: Folders a replay may read: ``parked`` (never reached Postgres) and
#: ``copied`` (reached the route; replaying it fills only what the store
#: refused, the rest answers ``duplicate``).
REPLAYABLE_FOLDERS = (FOLDERS[PARK], "copied")
#: Bounds one run, so a typo in a date cannot replay a month.
MAX_WINDOW = timedelta(hours=24)
#: Per request; the door itself answers in milliseconds when Postgres is up.
REQUEST_TIMEOUT_S = 30.0
#: How often progress is logged.
PROGRESS_EVERY = 100
#: Where reports go, beside the archive folders.
REPORT_FOLDER = "replay-reports"

#: One run per pod at a time: the id of the run holding the guard, or None.
#: Two pods running the same window is harmless (events dedupe), only
#: wasteful. The route CLAIMS the guard before its first await (listing a
#: big window can outlast the ingress's 5 s, and a client retry must not
#: start a second run), and ``run_replay`` releases it when it ends. Plain
#: state, not a Lock: everything here runs on one event loop, so a check
#: followed by a set with no await in between cannot interleave.
_active_run: Optional[str] = None


class ReplayError(Exception):
    """A replay that cannot start; ``status_code`` is the HTTP answer."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ReplayResult:
    """What one run did. ``refused`` and ``failed`` rows never hold a token."""

    run_id: str
    folder: str
    start: str
    end: str
    total: int = 0
    stored: int = 0  # 200, duplicate false: the event is now in Postgres
    duplicate: int = 0  # 200, duplicate true: it was already there
    refused: List[Dict[str, Any]] = field(default_factory=list)  # 4xx
    failed: List[Dict[str, Any]] = field(default_factory=list)  # 5xx, errors
    error: Optional[str] = None  # could not list, or stopped before the end
    complete: bool = False  # True only when every listed event was tried

    @property
    def processed(self) -> int:
        """Events tried so far: an interrupted run's report says where it
        stopped, so a rerun of the window (safe, events dedupe) finishes it."""
        return self.stored + self.duplicate + len(self.refused) + len(self.failed)

    def summary(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "folder": self.folder,
            "start": self.start,
            "end": self.end,
            "total": self.total,
            "processed": self.processed,
            "complete": self.complete,
            "stored": self.stored,
            "duplicate": self.duplicate,
            "refused": len(self.refused),
            "failed": len(self.failed),
            "error": self.error,
        }


def _stamp(name: str) -> Optional[datetime]:
    """The arrival time encoded in an object name
    (``.../<YYYYMMDDTHHMMSSffffffZ>-<hex>.json``), or None."""
    try:
        stamp = name.rsplit("/", 1)[-1].split("-", 1)[0]
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def hour_prefixes(folder: str, start: datetime, end: datetime) -> List[str]:
    """One prefix per UTC hour folder the window touches."""
    hour = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    last = end.astimezone(timezone.utc)
    prefixes = []
    while hour < last:
        prefixes.append(f"{folder}/{EVENTS_DOOR}/{hour:%Y-%m-%d}/{hour:%H}/")
        hour += timedelta(hours=1)
    return prefixes


def _list_names(bucket: Any, folder: str, start: datetime, end: datetime) -> List[str]:
    """Blocking: the event objects of ``folder`` that arrived in [start, end),
    oldest first."""
    names = []
    for prefix in hour_prefixes(folder, start, end):
        for blob in bucket.list_blobs(prefix=prefix):
            stamp = _stamp(blob.name)
            if stamp is not None and start <= stamp < end:
                names.append(blob.name)
    return sorted(names)


def _validate(folder: str, start: datetime, end: datetime) -> None:
    if folder not in REPLAYABLE_FOLDERS:
        raise ReplayError(400, f"folder must be one of {list(REPLAYABLE_FOLDERS)}")
    if start.tzinfo is None or end.tzinfo is None:
        raise ReplayError(400, "start and end need a timezone offset")
    if end <= start:
        raise ReplayError(400, "end must be after start")
    if end - start > MAX_WINDOW:
        raise ReplayError(400, f"the window may be at most {MAX_WINDOW}")


async def check_replay(folder: str, start: datetime, end: datetime) -> Any:
    """The fast checks a replay must pass before it is accepted; returns the
    bucket. No listing here: a big window can take longer to list than the
    ingress gives a request, so a real run lists in the background.

    Refuses while Postgres writes are disabled: the replay goes through the
    same middleware, which would only park every request again.
    """
    _validate(folder, start, end)
    if await archive_mode() == PARK:
        raise ReplayError(
            409,
            "BB_INGEST_DB_WRITES_DISABLED is on: a replay would be parked again. "
            "Turn it off first.",
        )
    bucket = _resolve_bucket()
    if bucket is None:
        raise ReplayError(503, "INGEST_ARCHIVE_GCS_BUCKET is not usable on this pod")
    return bucket


async def list_replay(
    bucket: Any, folder: str, start: datetime, end: datetime
) -> List[str]:
    """The window's event objects, oldest first."""
    try:
        return await asyncio.to_thread(_list_names, bucket, folder, start, end)
    except Exception as e:  # permission or network: say which
        raise ReplayError(503, f"cannot list the archive bucket: {e}") from e


async def plan_replay(folder: str, start: datetime, end: datetime) -> List[str]:
    """Check a replay may start and list what it would replay (dry runs)."""
    bucket = await check_replay(folder, start, end)
    return await list_replay(bucket, folder, start, end)


def is_running() -> bool:
    return _active_run is not None


def claim(run_id: str) -> bool:
    """Take the one-run-per-pod guard for ``run_id``, without yielding:
    False if another run holds it. Every path that does not hand the guard
    to ``run_replay`` must ``release(run_id)``."""
    global _active_run
    if _active_run is not None:
        return False
    _active_run = run_id
    return True


def release(run_id: str) -> None:
    """Give the guard back; a no-op unless ``run_id`` holds it."""
    global _active_run
    if _active_run == run_id:
        _active_run = None


def _client() -> httpx.AsyncClient:
    """This pod's own server. ``trust_env=False``: never via a proxy."""
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{PORT}",
        timeout=REQUEST_TIMEOUT_S,
        trust_env=False,
    )


def _row(record: Any, name: str, status: Any, detail: str) -> Dict[str, Any]:
    """One refusal or failure row. Tolerates any record shape: an object that
    is not a JSON object, or whose ``auth`` is not one, is still reported
    here instead of raising and ending the whole run."""
    if not isinstance(record, dict):
        record = {}
    auth = record.get("auth")
    return {
        "object": name,
        "merchant_id": record.get("merchant_id"),
        "key": record.get("key"),
        "received_at": record.get("received_at"),
        "auth_header": auth.get("header") if isinstance(auth, dict) else None,
        "status": status,
        "detail": detail[:300],
    }


async def _replay_one(
    client: httpx.AsyncClient, bucket: Any, name: str, result: ReplayResult
) -> None:
    record: Dict[str, Any] = {}
    try:
        text = await asyncio.to_thread(bucket.blob(name).download_as_text)
        record = json.loads(text)
        auth = record.get("auth") or {}
        headers = {auth["header"]: auth["value"]} if auth.get("header") else {}
        response = await client.post(
            record["path"], json=record["body"], headers=headers
        )
    except Exception as e:  # unreadable object, network, timeout
        result.failed.append(_row(record, name, "error", repr(e)))
        return
    if response.status_code == 200:
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        if not isinstance(answer, dict):
            answer = {}
        if answer.get("archived"):
            # Parked again: BB_INGEST_DB_WRITES_DISABLED was turned on during
            # the run, so this event went back to GCS and never reached
            # Postgres. A failure row, so the report says to replay it again.
            result.failed.append(
                _row(record, name, 200, "parked: Postgres writes disabled mid-run")
            )
        elif answer.get("duplicate"):
            result.duplicate += 1
        else:
            result.stored += 1
    elif response.status_code < 500:
        result.refused.append(_row(record, name, response.status_code, response.text))
    else:
        result.failed.append(_row(record, name, response.status_code, response.text))


def _write_report(bucket: Any, result: ReplayResult) -> str:
    name = f"{REPORT_FOLDER}/{result.run_id}.json"
    report = {**result.summary(), "refusals": result.refused, "failures": result.failed}
    bucket.blob(name).upload_from_string(
        json.dumps(report, default=str), content_type="application/json"
    )
    return name


async def run_replay(
    run_id: str,
    folder: str,
    start: datetime,
    end: datetime,
    names: Optional[List[str]] = None,
) -> ReplayResult:
    """Replay the window one event at a time through this pod's own events
    door. Lists the window itself when ``names`` is not given, which is how
    the route starts a real run: the caller already has its 202.

    Expects the caller to hold the guard for ``run_id`` (``claim``); claims it
    itself when called directly. Releases it when the run ends, however it
    ends."""
    result = ReplayResult(
        run_id=run_id,
        folder=folder,
        start=start.isoformat(),
        end=end.isoformat(),
    )
    log = logger.bind(component=LOG_COMPONENT, run_id=run_id)
    if _active_run != run_id and not claim(run_id):
        raise ReplayError(409, "a replay is already running on this pod")
    bucket = None
    try:
        bucket = _resolve_bucket()
        if names is None:
            try:
                names = await list_replay(bucket, folder, start, end)
            except ReplayError as e:
                names = []
                result.error = e.detail
                log.error(f"replay could not list the window: {e.detail}")
        result.total = len(names)
        log.info(f"replay started: {len(names)} {folder} events, {start} to {end}")
        async with _client() as client:
            for done, name in enumerate(names, start=1):
                await _replay_one(client, bucket, name, result)
                if done % PROGRESS_EVERY == 0:
                    log.info(f"replay progress: {json.dumps(result.summary())}")
        result.complete = result.error is None
    finally:
        # Written on every way out: the end of the loop, an unexpected error,
        # or the task being cancelled (a pod shutting down). A partial report
        # says how far the run got. A forced kill (SIGKILL, OOM) still skips
        # it; the run's progress lines in the logs cover that case.
        try:
            if not result.complete and result.error is None:
                result.error = (
                    f"interrupted after {result.processed} of {result.total} "
                    "events; rerun the window to finish (events dedupe)"
                )
            where = "not written (no bucket)"
            if bucket is not None:
                try:
                    report = await asyncio.to_thread(_write_report, bucket, result)
                    where = f"gs://{INGEST_ARCHIVE_GCS_BUCKET}/{report}"
                except (
                    BaseException
                ) as e:  # noqa: BLE001 - never mask the run's own exit
                    where = f"not written ({e!r})"
            outcome = "finished" if result.complete else "stopped early"
            log.info(
                f"replay {outcome}: {json.dumps(result.summary())}; report {where}"
            )
        finally:
            release(run_id)
    return result


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
