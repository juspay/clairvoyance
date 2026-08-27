"""The push door (A9) — POST /ingest/events: envelope validation,
auth-before-store ordering, receipt shapes, and the two fail postures
(front door raises -> 503; buddy mirror record_event stays fail-open)."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.crm.record.api as record_api
import app.crm.record.ingest as record_ingest
from app.crm.record.api import push_event_route
from app.crm.record.schemas import EventIn, EventReceipt


def _event(**overrides: Any) -> EventIn:
    fields: Dict[str, Any] = {
        "merchant_id": "m1",
        "source": "loyalty-svc",
        "topic": "order.placed",
        "external_id": "ord-42",
        "payload": {"order_id": "42"},
    }
    fields.update(overrides)
    return EventIn(**fields)


def _event_body(**overrides: Any) -> Dict[str, Any]:
    """The same envelope as a JSON body, for route-level tests."""
    body: Dict[str, Any] = {
        "merchant_id": "m1",
        "source": "loyalty-svc",
        "topic": "order.placed",
        "external_id": "ord-42",
        "payload": {"order_id": "42"},
    }
    body.update(overrides)
    return body


def _allow_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_verify(merchant_id: str, request: Request) -> str:
        return merchant_id

    monkeypatch.setattr(record_api, "verify_s2s_caller", fake_verify)


# --- EventIn: the envelope other teams fill in ---


def test_event_in_defaults_schema_version_and_occurred_at() -> None:
    event = _event()
    assert event.schema_version == "1"
    assert event.occurred_at is None


@pytest.mark.parametrize("field", ["merchant_id", "source", "topic", "external_id"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_event_in_rejects_blank_envelope_keys(field: str, blank: str) -> None:
    # "" would still satisfy the DB's NOT NULL yet poison the dedupe
    # UNIQUE (merchant_id, source, external_id) — reject at the door.
    # "   " is the same hole wearing a hat: min_length=1 counts it as
    # length 3, so str_strip_whitespace has to run first to close it.
    with pytest.raises(ValidationError):
        _event(**{field: blank})


def test_event_in_strips_surrounding_whitespace() -> None:
    # The strip is real, not just a rejection side effect — a padded id
    # must dedupe against its clean twin, not sit beside it.
    assert _event(external_id="  ord-42  ").external_id == "ord-42"


@pytest.mark.parametrize(
    "field,value",
    [
        ("customer_id", "cus_1"),  # ADR 0020: attribution isn't theirs to send
        ("occured_at", "2026-08-25T10:00:00Z"),  # typo'd occurred_at
    ],
)
def test_event_in_rejects_unknown_fields(field: str, value: str) -> None:
    # extra="forbid" turns two silent failures loud. Ignored, the smuggled
    # customer_id teaches the producer a lie, and the typo'd timestamp
    # vanishes while the row quietly stores now().
    with pytest.raises(ValidationError):
        _event(**{field: value})


def test_event_in_rejects_naive_occurred_at() -> None:
    # asyncpg hands a naive value to timestamptz as-is and Postgres reads
    # it in the SESSION zone — a UTC producer omitting the Z lands hours
    # off, and T13 col 9 measures triggered sends from this column.
    with pytest.raises(ValidationError):
        _event(occurred_at="2026-08-25T10:00:00")


def test_event_in_accepts_offset_aware_occurred_at() -> None:
    assert _event(occurred_at="2026-08-25T10:00:00Z").occurred_at is not None
    assert _event(occurred_at="2026-08-25T10:00:00+05:30").occurred_at is not None


# --- The route: auth, receipt shapes, fail posture ---


def test_push_stores_letter_and_returns_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_auth(monkeypatch)
    seen: Dict[str, Any] = {}

    async def fake_ingest(**kwargs: Any) -> Optional[str]:
        seen.update(kwargs)
        return "11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(record_api, "ingest_event", fake_ingest)
    occurred = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
    receipt = asyncio.run(push_event_route(_event(occurred_at=occurred), "m1"))

    assert isinstance(receipt, EventReceipt)
    assert str(receipt.id) == "11111111-2222-3333-4444-555555555555"
    assert receipt.duplicate is False
    assert seen["merchant_id"] == "m1"
    assert seen["source"] == "loyalty-svc"
    assert seen["topic"] == "order.placed"
    assert seen["external_id"] == "ord-42"
    assert seen["payload"] == {"order_id": "42"}
    assert seen["occurred_at"] == occurred
    assert seen["schema_version"] == "1"


def test_push_duplicate_is_200_with_duplicate_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_auth(monkeypatch)

    async def fake_ingest(**kwargs: Any) -> Optional[str]:
        return None  # dedupe conflict — the letter is already stored

    monkeypatch.setattr(record_api, "ingest_event", fake_ingest)
    receipt = asyncio.run(push_event_route(_event(), "m1"))

    assert receipt.id is None
    assert receipt.duplicate is True


def test_push_rejected_auth_never_reaches_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Through the real route, not the handler function: auth is the route's
    # declared dependency, so "before the store" is FastAPI's ordering
    # guarantee rather than a line this file has to keep in place. Calling
    # the handler directly would skip the very thing under test.
    async def deny(merchant_id: str, request: Request) -> str:
        raise HTTPException(status_code=401, detail="Invalid s2s token")

    stored = False

    async def fake_ingest(**kwargs: Any) -> Optional[str]:
        nonlocal stored
        stored = True
        return None

    monkeypatch.setattr(record_api, "verify_s2s_caller", deny)
    monkeypatch.setattr(record_api, "ingest_event", fake_ingest)

    app = FastAPI()
    app.include_router(record_api.ingest_router, prefix="/ingest")
    response = TestClient(app).post("/ingest/events", json=_event_body())

    assert response.status_code == 401
    assert stored is False


def test_auth_is_declared_as_the_routes_dependency() -> None:
    # design/ingest-doors: "a route without its auth dependency is a
    # BLOCKER". Declared, not called in the body, so the next route added
    # beside this one cannot quietly ship without it.
    route = next(
        r
        for r in record_api.ingest_router.routes
        if isinstance(r, APIRoute) and r.path == "/events"
    )
    assert record_api.verified_caller in [d.call for d in route.dependant.dependencies]


def test_push_store_failure_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # Front door fails CLOSED: the producer asked us to store and we
    # couldn't — a 200 here would silently drop their event.
    _allow_auth(monkeypatch)

    async def broken_ingest(**kwargs: Any) -> Optional[str]:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(record_api, "ingest_event", broken_ingest)

    with pytest.raises(HTTPException) as e:
        asyncio.run(push_event_route(_event(), "m1"))
    assert e.value.status_code == 503


def test_ingest_route_mounted_under_ingest() -> None:
    from app.crm.api import router as crm_router

    assert "/ingest/events" in {
        getattr(route, "path", "") for route in crm_router.routes
    }


def test_journey_and_ingest_mount_once_each() -> None:
    # The record module owns two routers precisely so the root can mount
    # each under its own prefix. One shared `router` object included
    # twice would expose /ingest/{customer_id}/journey and
    # /customers/events as phantom surfaces and make OpenAPI lie.
    from app.crm.api import router as crm_router

    paths = [getattr(route, "path", "") for route in crm_router.routes]

    assert "/customers/{customer_id}/journey" in paths
    assert "/ingest/events" in paths
    assert "/ingest/{customer_id}/journey" not in paths
    assert "/customers/events" not in paths

    # Path+method, not path alone: one path legitimately carries several
    # verbs (GET /workflows lists, POST /workflows creates). A router
    # included twice shows up as the same verb on the same path.
    surface = [
        (getattr(r, "path", ""), m)
        for r in crm_router.routes
        for m in sorted(getattr(r, "methods", []) or [])
    ]
    assert len(surface) == len(set(surface)), f"duplicate mounts: {surface}"


# --- ingest.py: the raising variant vs the fail-open mirror door ---


def test_ingest_event_serializes_payload_and_returns_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: Dict[str, Any] = {}

    async def fake_insert(*args: Any) -> Optional[str]:
        seen["args"] = args
        return "event-id"

    monkeypatch.setattr(record_ingest.accessor, "insert_event", fake_insert)
    result = asyncio.run(
        record_ingest.ingest_event(
            merchant_id="m1",
            source="s",
            topic="t",
            external_id="x",
            payload={"when": datetime(2026, 8, 25)},
        )
    )

    assert result == "event-id"
    assert seen["args"][4] == json.dumps({"when": datetime(2026, 8, 25)}, default=str)


def test_ingest_event_raises_on_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_insert(*args: Any) -> Optional[str]:
        raise RuntimeError("db down")

    monkeypatch.setattr(record_ingest.accessor, "insert_event", broken_insert)

    with pytest.raises(RuntimeError):
        asyncio.run(
            record_ingest.ingest_event(
                merchant_id="m1",
                source="s",
                topic="t",
                external_id="x",
                payload={},
            )
        )


def test_record_event_stays_fail_open_on_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard for the refactor: buddy-side mirrors must never
    # break the call that produced the fact (module rules §6).
    async def broken_insert(*args: Any) -> Optional[str]:
        raise RuntimeError("db down")

    monkeypatch.setattr(record_ingest.accessor, "insert_event", broken_insert)

    result = asyncio.run(
        record_ingest.record_event(
            merchant_id="m1",
            source="s",
            topic="t",
            external_id="x",
            payload={},
        )
    )
    assert result is None
