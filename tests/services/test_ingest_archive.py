"""The inbound request archive (app/services/ingest_archive.py), driven
through the real routes with the middleware in front, as app/main.py wires
it.

    both flags off            -> nothing written, the request runs as today
    BB_INGEST_ARCHIVE_ENABLED -> copied/ object in the background, the
                                 request runs as today
    BB_INGEST_DB_WRITES_DISABLED
                              -> parked/ object, the door's normal success
                                 answer, auth and Postgres never touched;
                                 if the write fails, or the body is not a
                                 JSON object, the request runs as today
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

import app.api.routers.breeze_buddy.leads as leads_door
import app.crm.record.api as record_api
import app.services.ingest_archive as archive
from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.auth import _extract_token
from app.crm.record.schemas import EventIn
from app.schemas.breeze_buddy.auth import UserInfo, UserRole

REPO = Path(__file__).resolve().parents[2]
TEMPLATE_ID = "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"
EVENTS = "/ingest/events"
LEADS = "/agent/voice/breeze-buddy/leads"

EVENT_BODY: Dict[str, Any] = {
    "merchant_id": "m1",
    "source": "shopify",
    "topic": "orders/create",
    "external_id": "orders/create:123",
    "payload": {"id": 123},
}
LEAD_BODY: Dict[str, Any] = {
    "request_id": "order-1",
    "payload": {"customer_mobile_number": "+919999999999"},
    "template_id": TEMPLATE_ID,
    "reseller_id": "RESELLER",
    "merchant_id": "shop.myshopify.com",
}
S2S = {"x-s2s-token": "tok-merchant-A"}
BEARER = {"authorization": "Bearer jwt-relay"}


# --- doubles --------------------------------------------------------------------


class _Blob:
    def __init__(self, bucket: "_Bucket", name: str):
        self._bucket = bucket
        self._name = name

    def upload_from_string(
        self, data: str, content_type: Optional[str] = None, **kwargs: Any
    ):
        self._bucket.written.append((self._name, data))
        self._bucket.upload_kwargs.append({"content_type": content_type, **kwargs})
        self._bucket.threads.append(threading.current_thread().name)


class _Bucket:
    def __init__(self) -> None:
        self.written: List[Tuple[str, str]] = []
        self.upload_kwargs: List[Dict[str, Any]] = []
        self.threads: List[str] = []

    def blob(self, name: str) -> _Blob:
        return _Blob(self, name)

    def objects(self) -> List[Tuple[str, Dict[str, Any]]]:
        return [(name, json.loads(data)) for name, data in self.written]


class _BrokenBucket:
    def blob(self, name: str):
        raise RuntimeError("gcs unreachable")


class _SlowBlob:
    def upload_from_string(self, data: str, **kwargs: Any):
        time.sleep(0.3)


class _SlowBucket:
    def blob(self, name: str) -> _SlowBlob:
        return _SlowBlob()


class _Calls:
    """Counts what ran behind the middleware: auth and the store."""

    def __init__(self) -> None:
        self.auth: List[str] = []
        self.store: List[Any] = []


def _flags(
    monkeypatch: pytest.MonkeyPatch, *, archive_on: bool = False, db_off: bool = False
) -> None:
    async def archive_flag() -> bool:
        return archive_on

    async def db_flag() -> bool:
        return db_off

    monkeypatch.setattr(archive.dyn_cfg, "BB_INGEST_ARCHIVE_ENABLED", archive_flag)
    monkeypatch.setattr(archive.dyn_cfg, "BB_INGEST_DB_WRITES_DISABLED", db_flag)


def _bucket(monkeypatch: pytest.MonkeyPatch, bucket: Any) -> Any:
    monkeypatch.setattr(archive, "_resolve_bucket", lambda: bucket)
    return bucket


def _app(monkeypatch: pytest.MonkeyPatch) -> Tuple[FastAPI, _Calls]:
    """Both real routers at their real prefixes behind the real middleware,
    as in app/main.py. Auth and the stores are doubles that record whether
    they ran."""
    calls = _Calls()

    async def verify(merchant_id: str, request: Request) -> str:
        calls.auth.append(merchant_id)
        return merchant_id

    async def ingest(**kwargs: Any) -> Optional[str]:
        calls.store.append(kwargs)
        return "11111111-2222-3333-4444-555555555555"

    async def push(req: PushLeadRequest, user: UserInfo) -> Dict[str, Any]:
        calls.store.append(req)
        return {
            "status": "queued",
            "lead_call_tracker_id": "lead-from-postgres",
            "order_id": req.request_id,
            "message": "Call request added to queue for processing",
        }

    def rbac_user() -> UserInfo:
        calls.auth.append("rbac")
        return UserInfo(id="u", username="relay", role=UserRole.ADMIN)

    monkeypatch.setattr(record_api, "verify_s2s_caller", verify)
    monkeypatch.setattr(record_api, "ingest_event", ingest)
    monkeypatch.setattr(leads_door, "push_lead_handler", push)

    app = FastAPI()
    app.include_router(record_api.ingest_router, prefix="/ingest")
    app.include_router(leads_door.router, prefix="/agent/voice/breeze-buddy")
    app.dependency_overrides[get_current_user_with_rbac] = rbac_user
    app.add_middleware(archive.IngestArchiveMiddleware)
    return app, calls


def _settle(bucket: _Bucket, count: int) -> None:
    """Copy-mode writes run in the background; wait for them to land."""
    deadline = time.monotonic() + 2
    while len(bucket.written) < count and time.monotonic() < deadline:
        time.sleep(0.01)


# --- the wiring and the flags ----------------------------------------------------


def test_middleware_paths_are_real_post_routes_of_the_app() -> None:
    # The paths live in the middleware; if a prefix ever changes, the
    # archive would silently stop matching. This pins them to app/main.py.
    from app.main import app as real_app

    post_routes = {
        route.path
        for route in real_app.routes
        if isinstance(route, APIRoute) and "POST" in route.methods
    }
    assert set(archive.DOORS) <= post_routes
    assert any(
        m.cls is archive.IngestArchiveMiddleware for m in real_app.user_middleware
    )


@pytest.mark.parametrize(
    "archive_on,db_off,mode",
    [
        (False, False, archive.OFF),
        (True, False, archive.COPY),
        (False, True, archive.PARK),  # disabling the DB implies the archive
        (True, True, archive.PARK),
    ],
)
def test_mode_from_the_two_flags(
    monkeypatch: pytest.MonkeyPatch, archive_on: bool, db_off: bool, mode: str
) -> None:
    _flags(monkeypatch, archive_on=archive_on, db_off=db_off)
    assert asyncio.run(archive.archive_mode()) == mode


def test_unreadable_flags_mean_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> bool:
        raise RuntimeError("redis down")

    monkeypatch.setattr(archive.dyn_cfg, "BB_INGEST_ARCHIVE_ENABLED", broken)
    monkeypatch.setattr(archive.dyn_cfg, "BB_INGEST_DB_WRITES_DISABLED", broken)
    assert asyncio.run(archive.archive_mode()) == archive.OFF


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": "Bearer jwt-1"},
        {"x-s2s-token": "s2s-1"},
        {"x-webhook-token": "wh-1"},
        {"x-merchant-token": "mt-1"},
        {"authorization": "raw-1"},
        {"authorization": "Bearer jwt-1", "x-s2s-token": "s2s-1"},
        {"authorization": "raw-1", "x-s2s-token": "s2s-1"},
        {},
    ],
)
def test_stored_header_authenticates_as_the_same_token(
    headers: Dict[str, str],
) -> None:
    # The replay sends ONLY the stored header; the events door must then
    # resolve the same token it resolved the first time.
    def request(h: Dict[str, str]) -> StarletteRequest:
        return StarletteRequest(
            {
                "type": "http",
                "headers": [(k.encode(), v.encode()) for k, v in h.items()],
            }
        )

    stored = archive.presented_auth(headers)
    replayed = {stored["header"]: stored["value"]} if stored else {}
    assert _extract_token(request(replayed)) == _extract_token(request(headers))


# --- both flags off -----------------------------------------------------------------


def test_off_runs_as_today(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch)
    bucket = _bucket(monkeypatch, _Bucket())
    app, calls = _app(monkeypatch)

    with TestClient(app) as client:
        events = client.post(EVENTS, json=EVENT_BODY, headers=S2S)
        leads = client.post(LEADS, json=LEAD_BODY, headers=BEARER)

    assert events.status_code == 200 and events.json()["id"] is not None
    assert leads.status_code == 201
    assert calls.auth == ["m1", "rbac"] and len(calls.store) == 2
    assert bucket.written == []


def test_other_requests_never_read_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: List[str] = []

    async def spy() -> bool:
        reads.append("read")
        return True

    monkeypatch.setattr(archive.dyn_cfg, "BB_INGEST_ARCHIVE_ENABLED", spy)
    monkeypatch.setattr(archive.dyn_cfg, "BB_INGEST_DB_WRITES_DISABLED", spy)
    app, _ = _app(monkeypatch)

    with TestClient(app) as client:
        client.get(EVENTS)  # same path, not a POST
        client.post("/ingest/elsewhere", json={})
        client.post(LEADS + "/request", json={})  # a neighbouring lead path

    assert reads == []


# --- copy mode ------------------------------------------------------------------------


def test_copy_mode_copies_and_runs_as_today(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, archive_on=True)
    bucket = _bucket(monkeypatch, _Bucket())
    app, calls = _app(monkeypatch)

    with TestClient(app) as client:
        events = client.post(EVENTS, json=EVENT_BODY, headers=S2S)
        leads = client.post(LEADS, json=LEAD_BODY, headers=BEARER)
        _settle(bucket, 2)

    # the requests ran exactly as today, bodies intact
    assert events.json()["id"] == "11111111-2222-3333-4444-555555555555"
    assert leads.json()["lead_call_tracker_id"] == "lead-from-postgres"
    assert calls.auth == ["m1", "rbac"]
    assert calls.store[0]["payload"] == {"id": 123}
    assert calls.store[1].request_id == "order-1"
    # and both were copied, with the caller's credential
    assert all(name.startswith("copied/") for name, _ in bucket.written)
    objects = {name.split("/")[1]: record for name, record in bucket.objects()}
    assert objects["ingest-events"]["mode"] == "copy"
    assert objects["ingest-events"]["auth"] == {
        "header": "x-s2s-token",
        "value": "tok-merchant-A",
    }
    assert objects["ingest-events"]["key"] == "orders/create:123"
    assert objects["ingest-events"]["body"] == EVENT_BODY
    assert objects["buddy-leads"]["auth"] == {
        "header": "authorization",
        "value": "Bearer jwt-relay",
    }
    assert objects["buddy-leads"]["key"] == "order-1"


def test_copy_mode_gcs_failure_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, archive_on=True)
    _bucket(monkeypatch, _BrokenBucket())
    app, calls = _app(monkeypatch)

    with TestClient(app) as client:
        response = client.post(EVENTS, json=EVENT_BODY, headers=S2S)

    assert response.status_code == 200 and response.json()["id"] is not None
    assert calls.auth == ["m1"] and len(calls.store) == 1


# --- park mode --------------------------------------------------------------------------


def test_park_mode_event_skips_auth_and_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flags(monkeypatch, db_off=True)
    bucket = _bucket(monkeypatch, _Bucket())
    app, calls = _app(monkeypatch)

    with TestClient(app) as client:
        response = client.post(EVENTS, json=EVENT_BODY, headers=S2S)

    assert response.status_code == 200
    assert response.json() == {"id": None, "duplicate": False, "archived": True}
    assert calls.auth == [] and calls.store == []
    [(name, record)] = bucket.objects()
    assert name.startswith("parked/ingest-events/")
    assert record["mode"] == "park"
    assert record["path"] == EVENTS
    assert record["auth"] == {"header": "x-s2s-token", "value": "tok-merchant-A"}
    assert record["merchant_id"] == "m1"
    assert record["key"] == "orders/create:123"
    assert record["receipt"] == response.json()
    body = dict(record["body"])
    assert body.pop("occurred_at").endswith("+00:00")  # pinned arrival time
    assert body == EVENT_BODY
    # the parked body is a valid request for the replay
    assert EventIn.model_validate(record["body"]).external_id == "orders/create:123"


def test_park_mode_lead_skips_auth_and_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flags(monkeypatch, db_off=True)
    bucket = _bucket(monkeypatch, _Bucket())
    app, calls = _app(monkeypatch)

    with TestClient(app) as client:
        response = client.post(LEADS, json=LEAD_BODY, headers=BEARER)

    assert response.status_code == 201
    answer = response.json()
    assert set(answer) == {"status", "lead_call_tracker_id", "order_id", "message"}
    assert answer["status"] == "queued" and answer["order_id"] == "order-1"
    assert answer["message"] == "Call request added to queue for processing"
    assert len(answer["lead_call_tracker_id"]) == 36
    assert calls.auth == [] and calls.store == []
    [(name, record)] = bucket.objects()
    assert name.startswith("parked/buddy-leads/")
    assert record["path"] == LEADS
    assert record["auth"] == {"header": "authorization", "value": "Bearer jwt-relay"}
    assert record["body"] == LEAD_BODY  # exactly what the caller sent
    assert record["key"] == "order-1"
    assert record["receipt"]["lead_call_tracker_id"] == answer["lead_call_tracker_id"]
    assert PushLeadRequest.model_validate(record["body"]).request_id == "order-1"


def test_park_mode_keeps_a_supplied_occurred_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flags(monkeypatch, db_off=True)
    bucket = _bucket(monkeypatch, _Bucket())
    app, _ = _app(monkeypatch)
    body = {**EVENT_BODY, "occurred_at": "2026-10-15T10:00:00+05:30"}

    with TestClient(app) as client:
        client.post(EVENTS, json=body, headers=S2S)

    assert bucket.objects()[0][1]["body"]["occurred_at"] == "2026-10-15T10:00:00+05:30"


def test_park_mode_parks_without_auth_or_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As ruled: while DB writes are disabled nothing is checked at the door.
    # A missing credential or a missing field is parked, and refused by the
    # door's own auth and validation when it is replayed.
    # Why not run the checks that need no database here: auth and validation
    # stay in one place, the doors, so the middleware holds no second copy
    # that could drift from them, and the replay runs them for real.
    _flags(monkeypatch, db_off=True)
    bucket = _bucket(monkeypatch, _Bucket())
    app, calls = _app(monkeypatch)
    incomplete = {k: v for k, v in EVENT_BODY.items() if k != "external_id"}

    with TestClient(app) as client:
        response = client.post(EVENTS, json=incomplete)

    assert response.status_code == 200 and calls.auth == []
    [(_, record)] = bucket.objects()
    assert record["auth"] is None and record["key"] == ""


@pytest.mark.parametrize("content", [b"not json", b'["a", "list"]', b'"a string"'])
def test_park_mode_non_object_body_takes_the_normal_route(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    # Not a JSON object: nothing to park. The route answers its own 422,
    # before auth runs, so no database is touched.
    _flags(monkeypatch, db_off=True)
    bucket = _bucket(monkeypatch, _Bucket())
    app, calls = _app(monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            EVENTS, content=content, headers={**S2S, "content-type": "application/json"}
        )

    assert response.status_code == 422
    assert bucket.written == [] and calls.auth == [] and calls.store == []


@pytest.mark.parametrize("gcs", ["broken", "unset", "slow"])
def test_park_mode_write_failure_runs_as_today(
    monkeypatch: pytest.MonkeyPatch, gcs: str
) -> None:
    _flags(monkeypatch, db_off=True)
    if gcs == "slow":
        monkeypatch.setattr(archive, "UPLOAD_TIMEOUT_S", 0.05)
    _bucket(
        monkeypatch,
        {"broken": _BrokenBucket(), "unset": None, "slow": _SlowBucket()}[gcs],
    )
    app, calls = _app(monkeypatch)

    with TestClient(app) as client:
        events = client.post(EVENTS, json=EVENT_BODY, headers=S2S)
        leads = client.post(LEADS, json=LEAD_BODY, headers=BEARER)

    # auth and Postgres ran, with the bodies intact
    assert events.json()["id"] == "11111111-2222-3333-4444-555555555555"
    assert leads.json()["lead_call_tracker_id"] == "lead-from-postgres"
    assert calls.auth == ["m1", "rbac"]
    assert calls.store[0]["external_id"] == "orders/create:123"
    assert calls.store[1].request_id == "order-1"


def test_oversize_event_takes_the_normal_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, db_off=True)
    bucket = _bucket(monkeypatch, _Bucket())
    app, calls = _app(monkeypatch)
    big = {**EVENT_BODY, "payload": {"blob": "x" * (archive.MAX_BODY_BYTES + 10)}}

    with TestClient(app) as client:
        response = client.post(EVENTS, json=big, headers=S2S)

    assert response.status_code == 413  # the door's own size limit
    assert bucket.written == [] and calls.store == []


def test_the_archive_and_the_events_door_follow_one_cap() -> None:
    """Both read CRM_INGEST_MAX_BODY_BYTES. Apart, a raised dial would send a
    letter between the two past park mode into auth, which reads the Postgres
    park mode exists to spare (5xx, retried into more 5xx); a lowered one would
    park a letter the door then refuses at replay, after the producer was told
    it succeeded. Booted fresh: static config is read once at import."""
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "CRM_INGEST_MAX_BODY_BYTES": "2097152",
    }
    env.setdefault("JWT_SECRET_KEY", "test")
    env.setdefault("JWT_ALGORITHM", "HS256")
    booted = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.services.ingest_archive as a, app.crm.record.api as r; "
            "print(a.MAX_BODY_BYTES, r.MAX_LETTER_BYTES)",
        ],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert booted.returncode == 0, booted.stderr[-2000:]
    assert booted.stdout.split()[-2:] == ["2097152", "2097152"]


def test_a_bug_in_the_archive_never_breaks_the_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Anything unexpected inside the archive means "run as today".
    _flags(monkeypatch, db_off=True)
    _bucket(monkeypatch, _Bucket())
    app, calls = _app(monkeypatch)

    def broken_answer(body: Dict[str, Any]) -> Dict[str, Any]:
        raise RuntimeError("bug")

    door = archive.DOORS[EVENTS]
    monkeypatch.setitem(
        archive.DOORS,
        EVENTS,
        archive.Door(
            name=door.name,
            key_field=door.key_field,
            status_code=door.status_code,
            answer=broken_answer,
            prepare=door.prepare,
        ),
    )

    with TestClient(app) as client:
        response = client.post(EVENTS, json=EVENT_BODY, headers=S2S)

    assert response.status_code == 200 and response.json()["id"] is not None
    assert calls.auth == ["m1"] and len(calls.store) == 1


# --- the write itself -----------------------------------------------------------------


def test_write_is_bounded_and_off_the_shared_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flags(monkeypatch, db_off=True)
    bucket = _bucket(monkeypatch, _Bucket())
    app, _ = _app(monkeypatch)

    with TestClient(app) as client:
        client.post(EVENTS, json=EVENT_BODY, headers=S2S)

    assert bucket.upload_kwargs == [
        {
            "content_type": "application/json",
            "timeout": archive.UPLOAD_TIMEOUT_S,
            "retry": None,
        }
    ]
    assert bucket.threads[0].startswith("ingest-archive")


def test_concurrent_first_writes_share_one_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: List[str] = []

    def slow_get(name: str) -> object:
        calls.append(name)
        time.sleep(0.1)
        return sentinel

    monkeypatch.setattr(archive, "get_gcs_bucket", slow_get)
    monkeypatch.setattr(archive, "INGEST_ARCHIVE_GCS_BUCKET", "b")
    monkeypatch.setattr(archive, "_bucket_resolved", False)
    monkeypatch.setattr(archive, "_bucket", None)

    results: List[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(archive._resolve_bucket()))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == ["b"] and results == [sentinel] * 5


def test_unset_bucket_env_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "INGEST_ARCHIVE_GCS_BUCKET", "")
    monkeypatch.setattr(archive, "_bucket_resolved", False)
    monkeypatch.setattr(archive, "_bucket", None)
    assert archive._upload("x.json", "{}") is False


def test_the_credential_is_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.logger import logger

    _flags(monkeypatch, db_off=True)
    bucket = _bucket(monkeypatch, _Bucket())
    app, _ = _app(monkeypatch)
    lines: List[str] = []
    sink = logger.add(lambda m: lines.append(str(m.record)), format="{message}")
    try:
        with TestClient(app) as client:
            client.post(EVENTS, json=EVENT_BODY, headers=S2S)
    finally:
        logger.remove(sink)

    name = bucket.written[0][0]
    assert any("archived to gs://" in line and name in line for line in lines)
    assert not any("tok-merchant-A" in line for line in lines)
