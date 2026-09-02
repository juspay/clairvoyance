"""Record's provider bays: dispatch through the slot, and the door's replies.

Everything here runs over a FAKE spec registered in the INGRESS slot — the
door must know no provider by name, so its tests don't either. The Meta
spec's own halves are tested in test_meta_inbound.py (wire shape) and
test_connectivity_ingress.py (owner resolution); the URL Meta is actually
given is proven in test_ingress_integration.py.

Two promises carry the file: the bays are the only unauthenticated routes
in the service, so most tests refuse something; and a STORE failure is 503
— the one answer that doesn't lose the provider's letter forever.
"""

from typing import List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.crm.connectivity import api as connectivity_api
from app.crm.record import api as record_api, ingress
from app.crm.record.schemas import EventIn

RAW = b'{"any": "body"}'


class _FakeSpec:
    """A controllable IngressSpec stand-in registered under "fake"."""

    def __init__(self):
        """Test double."""
        self.verify_ok = True
        self.challenge_answer: Optional[str] = "echo-me"
        self.letters: List[EventIn] = [_letter()]
        self.verified: list = []
        self.enveloped: list = []

    def verify(self, raw, headers) -> bool:
        """Test double."""
        self.verified.append(raw)
        return self.verify_ok

    async def envelope(self, headers, body) -> List[EventIn]:
        """Test double."""
        self.enveloped.append(body)
        return self.letters

    def challenge(self, params) -> Optional[str]:
        """Test double."""
        return self.challenge_answer


def _letter(external_id="x-1") -> EventIn:
    """One resolved letter as a spec's envelope returns it."""
    return EventIn(
        merchant_id="shop",
        source="fake",
        topic="message.status",
        external_id=external_id,
        payload={"k": "v"},
    )


@pytest.fixture
def spec(monkeypatch) -> _FakeSpec:
    """A fake bay in the slot, and the spine patched to record filings."""
    fake = _FakeSpec()
    monkeypatch.setitem(ingress.INGRESS, "fake", fake)
    return fake


@pytest.fixture
def spine(monkeypatch) -> list:
    """Captures every letter the door files."""
    filed: list = []

    async def _ingest(**kwargs):
        """Test double: the store accepts."""
        filed.append(kwargs)
        return f"ev-{len(filed)}"

    monkeypatch.setattr(record_api, "ingest_event", _ingest)
    return filed


@pytest.fixture
def client(spec, spine) -> TestClient:
    """The webhook router over the fake bay."""
    app = FastAPI()
    app.include_router(record_api.webhook_router, prefix="/ingest/webhooks")
    return TestClient(app)


URL = "/ingest/webhooks/fake"


def test_a_verified_body_is_filed_and_answered_200(client, spec, spine) -> None:
    """A verified body is filed and answered 200."""
    assert client.post(URL, content=RAW).status_code == 200
    assert spec.verified == [RAW]
    assert len(spine) == 1
    assert spine[0]["external_id"] == "x-1" and spine[0]["merchant_id"] == "shop"


def test_an_unknown_provider_is_a_404_before_the_body_is_read(
    client, spec, spine
) -> None:
    # The slot is a closed map: no bay, no bytes buffered, nothing learned.
    """An unknown provider is a 404 before the body is read."""
    assert client.post("/ingest/webhooks/nobody", content=RAW).status_code == 404
    assert spec.verified == [] and spine == []


def test_a_failed_verification_is_403_and_files_nothing(client, spec, spine) -> None:
    """A failed verification is 403 and files nothing."""
    spec.verify_ok = False
    response = client.post(URL, content=RAW)
    assert response.status_code == 403
    # No detail: a caller who cannot sign has not earned an explanation.
    assert response.content == b""
    assert spine == []


def test_a_verified_non_object_body_is_400(client, spec, spine) -> None:
    """A verified non-object body is 400."""
    assert client.post(URL, content=b"[1,2,3]").status_code == 400
    assert spec.enveloped == [] and spine == []


def test_a_store_failure_is_503_never_a_silent_200(client, spec, monkeypatch) -> None:
    # Finding 3: the provider retries a 503 with the same ids and dedupe
    # makes that safe; a 200 on a dropped letter loses it forever.
    """A store failure is 503, never a silent 200."""

    async def _down(**kwargs):
        """Test double: the store raised."""
        raise RuntimeError("pool down")

    monkeypatch.setattr(record_api, "ingest_event", _down)
    assert client.post(URL, content=RAW).status_code == 503


def test_a_duplicate_is_still_200(client, spec, monkeypatch) -> None:
    # None = the dedupe UNIQUE already holds it; asking for a resend would
    # not help.
    """A duplicate is still 200."""

    async def _duplicate(**kwargs):
        """Test double: the spine already holds it."""
        return None

    monkeypatch.setattr(record_api, "ingest_event", _duplicate)
    assert client.post(URL, content=RAW).status_code == 200


def test_an_oversized_body_is_refused_while_streaming(
    client, spec, spine, monkeypatch
) -> None:
    """An oversized body is refused while streaming.

    Even a VALID signature does not buy an unbounded read: the cap fires
    during the read, before verification — buffering an arbitrary body for
    an unauthenticated caller is the cost the route must not pay.
    """
    monkeypatch.setattr(record_api, "MAX_LETTER_BYTES", 64)
    assert client.post(URL, content=b"x" * 65).status_code == 413
    assert spec.verified == [] and spine == []


def test_the_handshake_echoes_in_plain_text(client, spec) -> None:
    """The handshake echoes in plain text."""
    response = client.get(URL)
    assert response.status_code == 200
    # Echoed raw — the provider compares the body, so quotes would fail it.
    assert response.text == "echo-me"


def test_a_refused_handshake_hides_the_route(client, spec) -> None:
    # 404, not 403: a different answer for a wrong token than for a missing
    # bay tells an unauthenticated caller they found something to guess at.
    """A refused handshake hides the route."""
    spec.challenge_answer = None
    assert client.get(URL).status_code == 404
    assert client.get("/ingest/webhooks/nobody").status_code == 404


def test_only_the_webhook_router_is_unauthenticated() -> None:
    """The carve-out is the webhook router and nothing else.

    Both directions matter: auth appearing on the provider bays locks the
    provider out and silently stops every event, and auth going missing
    from any /connectors, /ingest or /catalog route would expose the door
    to anyone. The walk reads DECLARED dependencies — a check awaited on a
    handler's first line is invisible to it, which is the point: a route
    without its auth dependency is a BLOCKER (design/ingest-doors).
    """

    def dependency_names(route) -> list:
        """The route's declared dependencies, by function name."""
        dependant = getattr(route, "dependant", None)
        return [
            d.call.__name__
            for d in (dependant.dependencies if dependant else [])
            if getattr(d, "call", None) is not None
        ]

    def carries(route, name: str) -> bool:
        """Whether the dependency tree declares ``name`` at any depth."""

        def walk(dependant) -> bool:
            for d in dependant.dependencies:
                call = getattr(d, "call", None)
                if call is not None and call.__name__ == name:
                    return True
                if walk(d):
                    return True
            return False

        return walk(route.dependant)

    for route in record_api.webhook_router.routes:
        assert dependency_names(route) == [], getattr(route, "path", route)
    # Every /connectors route declares the tenancy door (merchant_scope),
    # which itself depends on the merchant-facing RBAC dependency — so the
    # walk looks one level in: the door, and the auth inside it.
    for route in connectivity_api.router.routes:
        assert carries(route, "get_current_user_with_rbac"), getattr(
            route, "path", route
        )
    # Every /ingest door (the envelope and the S2S schema registration)
    # declares the s2s verifier — by body or by query, one of the two
    # dependencies that call verify_s2s_caller.
    assert record_api.ingest_router.routes, "the ingest router has doors"
    for route in record_api.ingest_router.routes:
        assert carries(route, "verified_caller") or carries(
            route, "verified_merchant_caller"
        ), getattr(route, "path", route)
    # Every /catalog and /schemas console route is the admin's (ADR 0007
    # phase 1); merchant users take merchant_scope when "Your events" ships.
    assert record_api.catalog_router.routes, "the catalog router has routes"
    for route in record_api.catalog_router.routes:
        assert carries(route, "crm_admin_user"), getattr(route, "path", route)


def test_a_callback_carrying_no_letters_is_still_200(client, spec, spine) -> None:
    # Account-review noise and unknown objects ride the same webhook; a 200
    # with nothing filed is the right answer, not an error.
    """A callback carrying no letters is still 200."""
    spec.letters = []
    assert client.post(URL, content=RAW).status_code == 200
    assert spine == []
