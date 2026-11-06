"""Replay of archived ingest events (app/services/ingest_archive_replay.py).

The replay POSTs each stored body to this pod's own events door with the
merchant's stored auth header. Here that "own pod" is the real events router
behind the real archive middleware (ASGI transport), so auth, validation,
dedupe and the flags behave as in production; only GCS and the store are
doubles.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import app.api.routers.breeze_buddy.admin.ingest_archive as replay_route
import app.crm.record.api as record_api
import app.services.ingest_archive as archive
import app.services.ingest_archive_replay as replay
from app.api.routers.breeze_buddy.admin import router as admin_router
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas.breeze_buddy.auth import UserInfo, UserRole

IST = timezone(timedelta(hours=5, minutes=30))
GOOD = "good-token"


# --- doubles --------------------------------------------------------------------


class _Blob:
    def __init__(self, bucket: "_Bucket", name: str) -> None:
        self.bucket, self.name = bucket, name

    def download_as_text(self) -> str:
        return self.bucket.objects[self.name]

    def upload_from_string(
        self, data: str, content_type: Optional[str] = None, **kwargs: Any
    ):
        # **kwargs: the archive's real upload passes timeout= and retry=.
        self.bucket.objects[self.name] = data


class _Bucket:
    def __init__(self) -> None:
        self.objects: Dict[str, str] = {}
        self.listed: List[str] = []

    def blob(self, name: str) -> _Blob:
        return _Blob(self, name)

    def list_blobs(self, prefix: str):
        self.listed.append(prefix)
        return [_Blob(self, n) for n in self.objects if n.startswith(prefix)]

    def put(self, folder: str, at: datetime, record: Dict[str, Any]) -> str:
        name = archive.object_name(
            archive.PARK if folder == "parked" else archive.COPY,
            "ingest-events",
            at.astimezone(timezone.utc),
        )
        self.objects[name] = json.dumps(record)
        return name


def _event(external_id: str, merchant: str = "m1") -> Dict[str, Any]:
    return {
        "merchant_id": merchant,
        "source": "shopify",
        "topic": "orders/create",
        "external_id": external_id,
        "payload": {"id": external_id},
        "occurred_at": "2026-10-27T01:01:00+05:30",
    }


def _record(body: Dict[str, Any], token: Optional[str] = GOOD) -> Dict[str, Any]:
    return {
        "mode": "park",
        "door": "ingest-events",
        "method": "POST",
        "path": "/ingest/events",
        "received_at": "2026-10-26T19:31:00+00:00",
        "merchant_id": body.get("merchant_id"),
        "key": body.get("external_id", ""),
        "auth": {"header": "x-s2s-token", "value": token} if token else None,
        "body": body,
        "receipt": {"id": None, "duplicate": False, "archived": True},
    }


class _Store:
    """The events table: dedupes on (merchant, source, external_id)."""

    def __init__(self, existing: tuple = ()) -> None:
        self.rows = {("m1", "shopify", e) for e in existing}
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Optional[str]:
        self.calls.append(kwargs)
        key = (kwargs["merchant_id"], kwargs["source"], kwargs["external_id"])
        if key in self.rows:
            return None
        self.rows.add(key)
        return "11111111-2222-3333-4444-555555555555"


def _flags(monkeypatch: pytest.MonkeyPatch, *, archive_on=False, db_off=False):
    async def a() -> bool:
        return archive_on

    async def d() -> bool:
        return db_off

    monkeypatch.setattr(archive.dyn_cfg, "BB_INGEST_ARCHIVE_ENABLED", a)
    monkeypatch.setattr(archive.dyn_cfg, "BB_INGEST_DB_WRITES_DISABLED", d)


@pytest.fixture(autouse=True)
def _free_guard(monkeypatch: pytest.MonkeyPatch):
    """The one-run-per-pod guard is module state; no test may leak it."""
    monkeypatch.setattr(replay, "_active_run", None)
    yield
    replay._active_run = None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    """GCS bucket double, events store double, and the "own pod": the real
    events router behind the real archive middleware."""
    bucket = _Bucket()
    store = _Store()
    monkeypatch.setattr(archive, "_resolve_bucket", lambda: bucket)
    monkeypatch.setattr(replay, "_resolve_bucket", lambda: bucket)

    async def verify(merchant_id: str, request: Request) -> str:
        if request.headers.get("x-s2s-token") != GOOD:
            raise HTTPException(status_code=401, detail="Invalid s2s token")
        return merchant_id

    monkeypatch.setattr(record_api, "verify_s2s_caller", verify)
    monkeypatch.setattr(record_api, "ingest_event", store)

    pod = FastAPI()
    pod.include_router(record_api.ingest_router, prefix="/ingest")
    pod.add_middleware(archive.IngestArchiveMiddleware)
    monkeypatch.setattr(
        replay,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pod), base_url="http://pod"
        ),
    )
    _flags(monkeypatch)
    return bucket, store


START = datetime(2026, 10, 27, 1, 0, tzinfo=IST)
END = datetime(2026, 10, 27, 1, 10, tzinfo=IST)


# --- listing ----------------------------------------------------------------------


def test_hour_prefixes_cover_the_window_in_utc() -> None:
    prefixes = replay.hour_prefixes(
        "parked",
        datetime(2026, 10, 27, 0, 50, tzinfo=IST),
        datetime(2026, 10, 27, 2, 10, tzinfo=IST),
    )
    assert prefixes == [
        "parked/ingest-events/2026-10-26/19/",
        "parked/ingest-events/2026-10-26/20/",
    ]


def test_plan_lists_only_the_window_and_folder_oldest_first(world) -> None:
    bucket, _ = world
    inside_late = bucket.put(
        "parked", START + timedelta(minutes=9), _record(_event("b"))
    )
    inside_early = bucket.put(
        "parked", START + timedelta(minutes=1), _record(_event("a"))
    )
    bucket.put("parked", START - timedelta(seconds=1), _record(_event("before")))
    bucket.put("parked", END, _record(_event("at-end-excluded")))
    bucket.put("copied", START + timedelta(minutes=2), _record(_event("other-folder")))

    names = asyncio.run(replay.plan_replay("parked", START, END))

    assert names == [inside_early, inside_late]


@pytest.mark.parametrize(
    "folder,start,end,detail",
    [
        ("parked", END, START, "end must be after start"),
        ("parked", START, START + timedelta(hours=25), "at most"),
        ("leads", START, END, "folder must be one of"),
    ],
)
def test_plan_refuses_bad_input(world, folder, start, end, detail) -> None:
    with pytest.raises(replay.ReplayError) as e:
        asyncio.run(replay.plan_replay(folder, start, end))
    assert e.value.status_code == 400 and detail in e.value.detail


def test_plan_refuses_while_db_writes_are_disabled(world, monkeypatch) -> None:
    _flags(monkeypatch, db_off=True)
    with pytest.raises(replay.ReplayError) as e:
        asyncio.run(replay.plan_replay("parked", START, END))
    assert e.value.status_code == 409


def test_plan_reports_a_bucket_it_cannot_list(world, monkeypatch) -> None:
    class Denied:
        def list_blobs(self, prefix: str):
            raise PermissionError("403 storage.objects.list denied")

    monkeypatch.setattr(replay, "_resolve_bucket", lambda: Denied())
    with pytest.raises(replay.ReplayError) as e:
        asyncio.run(replay.plan_replay("parked", START, END))
    assert e.value.status_code == 503 and "storage.objects.list" in e.value.detail


# --- the replay itself -----------------------------------------------------------


def test_replay_stores_dedupes_and_reports_refusals(world) -> None:
    bucket, store = world
    store.rows.add(("m1", "shopify", "already-there"))
    t = START + timedelta(minutes=1)
    names = [
        bucket.put("parked", t, _record(_event("new-1"))),
        bucket.put(
            "parked", t + timedelta(seconds=1), _record(_event("already-there"))
        ),
        bucket.put(
            "parked", t + timedelta(seconds=2), _record(_event("bad"), token="revoked")
        ),
        bucket.put(
            "parked", t + timedelta(seconds=3), _record(_event("no-auth"), token=None)
        ),
        bucket.put(
            "parked",
            t + timedelta(seconds=4),
            _record({k: v for k, v in _event("x").items() if k != "external_id"}),
        ),
    ]
    bucket.objects[
        "parked/ingest-events/2026-10-26/19/20261026T193105000000Z-junk.json"
    ] = "{not json"
    names.append("parked/ingest-events/2026-10-26/19/20261026T193105000000Z-junk.json")

    result = asyncio.run(replay.run_replay("run-1", "parked", START, END, names))

    assert (result.total, result.stored, result.duplicate) == (6, 1, 1)
    assert sorted(r["status"] for r in result.refused) == [401, 401, 422]
    assert [r["status"] for r in result.failed] == ["error"]
    # the merchant's own body reached the door, exactly as sent
    stored = [c for c in store.calls if c["external_id"] == "new-1"][0]
    assert stored["payload"] == {"id": "new-1"} and stored["merchant_id"] == "m1"
    # the report is in the bucket, with no token anywhere
    report = json.loads(bucket.objects["replay-reports/run-1.json"])
    assert report["stored"] == 1 and report["refused"] == 3 and report["failed"] == 1
    assert GOOD not in json.dumps(report) and "revoked" not in json.dumps(report)
    assert {r["auth_header"] for r in report["refusals"]} == {"x-s2s-token", None}


@pytest.mark.parametrize(
    "raw",
    [
        "[]",  # valid JSON, not an object
        '"just a string"',
        "42",
        json.dumps({**_record(_event("bad-auth")), "auth": "not-a-dict"}),
        json.dumps({**_record(_event("bad-auth-2")), "auth": ["x"]}),
    ],
)
def test_a_malformed_object_is_recorded_and_the_run_continues(world, raw) -> None:
    # A bad object must become one failed row, not an exception that ends
    # the run: the objects after it are still replayed and the report is
    # still written.
    bucket, store = world
    t = START + timedelta(minutes=1)
    bad = "parked/ingest-events/2026-10-26/19/20261026T193100000000Z-bad.json"
    bucket.objects[bad] = raw
    good = bucket.put("parked", t + timedelta(seconds=1), _record(_event("after-bad")))

    result = asyncio.run(
        replay.run_replay("malformed", "parked", START, END, [bad, good])
    )

    assert result.total == 2 and result.stored == 1
    assert [r["object"] for r in result.failed] == [bad]
    assert result.failed[0]["auth_header"] is None
    assert ("m1", "shopify", "after-bad") in store.rows
    report = json.loads(bucket.objects["replay-reports/malformed.json"])
    assert report["failed"] == 1 and report["stored"] == 1


def test_events_parked_again_mid_run_count_as_failed(world, monkeypatch) -> None:
    # The run checks BB_INGEST_DB_WRITES_DISABLED only when it starts. If the
    # switch goes on mid-run, the rest are parked again (200, archived) and
    # never reach Postgres; they must be failures, not "stored".
    bucket, store = world
    t = START + timedelta(minutes=1)
    names = [
        bucket.put("parked", t + timedelta(seconds=i), _record(_event(f"mid-{i}")))
        for i in range(3)
    ]
    real_store = record_api.ingest_event

    async def store_then_park(**kwargs: Any) -> Optional[str]:
        stored = await real_store(**kwargs)
        _flags(monkeypatch, db_off=True)  # parking goes on after the first event
        return stored

    monkeypatch.setattr(record_api, "ingest_event", store_then_park)

    result = asyncio.run(replay.run_replay("midrun", "parked", START, END, names))

    assert (result.total, result.stored, result.duplicate) == (3, 1, 0)
    assert [r["key"] for r in result.failed] == ["mid-1", "mid-2"]
    assert all(r["status"] == 200 and "parked" in r["detail"] for r in result.failed)
    assert store.rows == {("m1", "shopify", "mid-0")}
    report = json.loads(bucket.objects["replay-reports/midrun.json"])
    assert report["stored"] == 1 and report["failed"] == 2


def test_a_complete_run_reports_complete(world) -> None:
    bucket, _ = world
    names = [
        bucket.put("parked", START + timedelta(seconds=i), _record(_event(f"ok-{i}")))
        for i in range(3)
    ]
    result = asyncio.run(replay.run_replay("whole", "parked", START, END, names))

    report = json.loads(bucket.objects["replay-reports/whole.json"])
    assert result.complete and report["complete"] is True
    assert report["processed"] == report["total"] == 3 and report["error"] is None


def test_an_error_mid_run_still_writes_a_partial_report(world, monkeypatch) -> None:
    # Something unexpected escapes the loop after two events: the report must
    # still be written, say how far the run got, and the guard must be freed.
    bucket, _ = world
    names = [
        bucket.put("parked", START + timedelta(seconds=i), _record(_event(f"e-{i}")))
        for i in range(5)
    ]
    real_one = replay._replay_one
    calls = {"n": 0}

    async def breaks_on_third(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        await real_one(*args, **kwargs)

    monkeypatch.setattr(replay, "_replay_one", breaks_on_third)

    with pytest.raises(RuntimeError):
        asyncio.run(replay.run_replay("broken", "parked", START, END, names))

    report = json.loads(bucket.objects["replay-reports/broken.json"])
    assert report["complete"] is False and report["total"] == 5
    assert report["processed"] == 2 and report["stored"] == 2
    assert "interrupted after 2 of 5" in report["error"]
    assert not replay.is_running()


def test_a_cancelled_run_still_writes_a_partial_report(world, monkeypatch) -> None:
    # A pod shutting down cancels the background task mid-run.
    bucket, _ = world
    names = [
        bucket.put("parked", START + timedelta(seconds=i), _record(_event(f"c-{i}")))
        for i in range(4)
    ]
    real_one = replay._replay_one

    async def main() -> None:
        first_done = asyncio.Event()
        calls = {"n": 0}

        async def slow_after_first(*args: Any, **kwargs: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                first_done.set()
                await asyncio.sleep(10)  # cancelled here
            await real_one(*args, **kwargs)

        monkeypatch.setattr(replay, "_replay_one", slow_after_first)
        task = asyncio.create_task(
            replay.run_replay("cancelled", "parked", START, END, names)
        )
        await first_done.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    report = json.loads(bucket.objects["replay-reports/cancelled.json"])
    assert report["complete"] is False and report["processed"] == 1
    assert "interrupted after 1 of 4" in report["error"]
    assert not replay.is_running()


def test_a_listing_failure_keeps_its_own_error(world, monkeypatch) -> None:
    bucket, _ = world

    def denied(*args: Any, **kwargs: Any):
        raise PermissionError("403 storage.objects.list denied")

    monkeypatch.setattr(replay, "_list_names", denied)
    result = asyncio.run(replay.run_replay("nolist", "parked", START, END))

    report = json.loads(bucket.objects["replay-reports/nolist.json"])
    assert result.complete is False and "storage.objects.list" in report["error"]
    assert "interrupted" not in report["error"]


def test_replaying_twice_stores_nothing_new(world) -> None:
    bucket, store = world
    names = [bucket.put("parked", START + timedelta(minutes=1), _record(_event("e1")))]

    first = asyncio.run(replay.run_replay("r1", "parked", START, END, names))
    second = asyncio.run(replay.run_replay("r2", "parked", START, END, names))

    assert (first.stored, first.duplicate) == (1, 0)
    assert (second.stored, second.duplicate) == (0, 1)
    assert len(store.rows) == 1


def test_replay_with_copy_mode_on_still_stores(world, monkeypatch) -> None:
    # Copy mode copies the replayed request again (harmless) and stores it.
    bucket, store = world
    _flags(monkeypatch, archive_on=True)
    names = [bucket.put("copied", START + timedelta(minutes=1), _record(_event("c1")))]

    result = asyncio.run(replay.run_replay("r3", "copied", START, END, names))

    assert result.stored == 1 and ("m1", "shopify", "c1") in store.rows


# --- the admin route ------------------------------------------------------------


def _route_client(monkeypatch, role: UserRole) -> tuple:
    app = FastAPI()
    app.include_router(admin_router, prefix="/agent/voice/breeze-buddy")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: UserInfo(
        id="u", username="ops", role=role
    )
    spawned: List[Any] = []
    monkeypatch.setattr(
        replay_route,
        "spawn_background_task",
        lambda coro, name=None: spawned.append(coro),
    )
    return TestClient(app), spawned


URL = "/agent/voice/breeze-buddy/admin/ingest-archive/replay"
BODY = {"start": "2026-10-27T01:00:00+05:30", "end": "2026-10-27T01:10:00+05:30"}


def test_route_is_admin_only(world, monkeypatch) -> None:
    client, spawned = _route_client(monkeypatch, UserRole.RESELLER)
    response = client.post(URL, json=BODY)
    assert response.status_code == 403 and spawned == []


def test_route_dry_run_lists_without_replaying(world, monkeypatch) -> None:
    bucket, store = world
    name = bucket.put("parked", START + timedelta(minutes=1), _record(_event("d1")))
    client, spawned = _route_client(monkeypatch, UserRole.ADMIN)

    response = client.post(URL, json={**BODY, "dry_run": True})

    assert response.status_code == 202
    assert response.json()["objects"] == 1 and response.json()["started"] is False
    assert response.json()["sample"] == [name] and spawned == [] and store.calls == []


def test_route_starts_a_background_run_that_stores(world, monkeypatch) -> None:
    bucket, store = world
    bucket.put("copied", START + timedelta(minutes=1), _record(_event("r1")))
    client, spawned = _route_client(monkeypatch, UserRole.ADMIN)

    response = client.post(URL, json={**BODY, "folder": "copied"})

    body = response.json()
    assert response.status_code == 202 and body["started"] is True
    # A real run answers before listing: no count yet, the report has it.
    assert body["objects"] is None and body["sample"] == []
    assert body["report"].endswith(f"{body['run_id']}.json")
    result = asyncio.run(spawned[0])  # what the background task runs
    assert result.total == 1 and result.stored == 1
    assert ("m1", "shopify", "r1") in store.rows
    report = json.loads(bucket.objects[f"replay-reports/{body['run_id']}.json"])
    assert report["total"] == 1 and report["error"] is None


@pytest.mark.parametrize(
    "body,status",
    [
        (
            {"start": "2026-10-27T01:00:00", "end": "2026-10-27T01:10:00"},
            422,
        ),  # no offset
        ({**BODY, "folder": "leads"}, 422),
        ({**BODY, "end": "2026-10-28T02:00:00+05:30"}, 400),  # over 24 h
    ],
)
def test_route_rejects_bad_input(world, monkeypatch, body, status) -> None:
    client, spawned = _route_client(monkeypatch, UserRole.ADMIN)
    assert client.post(URL, json=body).status_code == status and spawned == []


def test_route_refuses_while_db_writes_are_disabled(world, monkeypatch) -> None:
    _flags(monkeypatch, db_off=True)
    client, spawned = _route_client(monkeypatch, UserRole.ADMIN)
    response = client.post(URL, json=BODY)
    assert response.status_code == 409 and spawned == []


def test_route_refuses_a_second_run_on_the_same_pod(world, monkeypatch) -> None:
    monkeypatch.setattr(replay, "_active_run", "run-in-progress")
    client, spawned = _route_client(monkeypatch, UserRole.ADMIN)
    response = client.post(URL, json=BODY)
    assert response.status_code == 409 and spawned == []


def test_guard_is_claimed_before_listing_so_a_retry_gets_409(
    world, monkeypatch
) -> None:
    # A dry run lists inside the request, which can outlast the ingress's
    # 5 s. A request arriving in that gap must get 409, not a second run.
    import threading

    bucket, _ = world
    bucket.put("parked", START + timedelta(minutes=1), _record(_event("slow-1")))
    listing_started = threading.Event()
    let_listing_finish = threading.Event()
    real_list = replay._list_names

    def slow_list(*args: Any, **kwargs: Any):
        listing_started.set()
        let_listing_finish.wait(5)
        return real_list(*args, **kwargs)

    monkeypatch.setattr(replay, "_list_names", slow_list)
    client, spawned = _route_client(monkeypatch, UserRole.ADMIN)
    first: List[Any] = []
    t = threading.Thread(
        target=lambda: first.append(client.post(URL, json={**BODY, "dry_run": True}))
    )
    t.start()
    assert listing_started.wait(5)
    retry = client.post(URL, json=BODY)  # arrives while the dry run is listing
    let_listing_finish.set()
    t.join(10)

    assert retry.status_code == 409 and spawned == []
    assert first[0].status_code == 202 and first[0].json()["objects"] == 1
    assert not replay.is_running()  # the dry run released it


def test_a_real_run_answers_without_waiting_for_the_listing(world, monkeypatch) -> None:
    # The 202 and its run id must not wait for the GCS listing: a big window
    # can take longer to list than the ingress allows.
    import threading

    bucket, store = world
    bucket.put("parked", START + timedelta(minutes=1), _record(_event("late-1")))
    listing_called = threading.Event()
    real_list = replay._list_names

    def slow_list(*args: Any, **kwargs: Any):
        listing_called.set()
        return real_list(*args, **kwargs)

    monkeypatch.setattr(replay, "_list_names", slow_list)
    client, spawned = _route_client(monkeypatch, UserRole.ADMIN)

    response = client.post(URL, json=BODY)

    assert response.status_code == 202 and response.json()["run_id"]
    assert not listing_called.is_set()  # answered before any listing
    assert replay.is_running()  # a retry now gets 409
    assert client.post(URL, json=BODY).status_code == 409
    result = asyncio.run(spawned[0])
    assert listing_called.is_set() and result.stored == 1
    assert not replay.is_running()


def test_a_background_listing_failure_is_reported_and_frees_the_guard(
    world, monkeypatch
) -> None:
    bucket, _ = world

    def denied(*args: Any, **kwargs: Any):
        raise PermissionError("403 storage.objects.list denied")

    monkeypatch.setattr(replay, "_list_names", denied)
    client, spawned = _route_client(monkeypatch, UserRole.ADMIN)

    response = client.post(URL, json=BODY)
    assert response.status_code == 202
    result = asyncio.run(spawned[0])

    assert result.total == 0 and "storage.objects.list" in (result.error or "")
    report = json.loads(
        bucket.objects[f"replay-reports/{response.json()['run_id']}.json"]
    )
    assert "storage.objects.list" in report["error"]
    assert not replay.is_running()


def test_guard_is_released_after_a_dry_run_and_after_an_error(
    world, monkeypatch
) -> None:
    client, spawned = _route_client(monkeypatch, UserRole.ADMIN)

    assert client.post(URL, json={**BODY, "dry_run": True}).status_code == 202
    assert not replay.is_running()

    assert client.post(URL, json={**BODY, "folder": "leads"}).status_code == 422
    assert (
        client.post(URL, json={**BODY, "end": "2026-10-28T02:00:00+05:30"}).status_code
        == 400
    )
    assert not replay.is_running()

    # and a real run can start afterwards
    assert client.post(URL, json=BODY).status_code == 202
    assert replay.is_running() and len(spawned) == 1
    asyncio.run(spawned[0])
    assert not replay.is_running()


def test_run_replay_called_directly_claims_and_releases(world) -> None:
    bucket, _ = world
    names = [bucket.put("parked", START + timedelta(minutes=1), _record(_event("d1")))]
    asyncio.run(replay.run_replay("direct", "parked", START, END, names))
    assert not replay.is_running()
