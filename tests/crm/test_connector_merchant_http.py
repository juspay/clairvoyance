"""merchant_http (enh A/03, the ruled shape): a merchant's own endpoint as a
connector — the door validates the address once, the one verb `request`
carries the run's facts out and the declared answer back, and every hop
crosses the egress guard. No network: the guard's DNS and the request
itself are stubbed at the seam actions.py calls. Installation lookup, action
dispatch, workflow execution and publish validation live here too; real-DB
cases remain in test_installation_route_integration.py.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, cast
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

import app.crm.connectivity.db.accessors.installation as accessor
import app.crm.connectivity.providers.merchant_http.actions as face_module
import app.crm.connectivity.providers.shopify.via_nautilus as via_nautilus
import app.crm.outreach.nodes.action as action_node
import app.crm.outreach.plans as plans
from app.core.security import ssrf
from app.crm.connectivity import actions as actions_module
from app.crm.connectivity.actions import perform_action
from app.crm.connectivity.connectors import CONNECTORS
from app.crm.connectivity.contracts import action_declares, action_names
from app.crm.connectivity.db.queries.installation import (
    installation_for_connector_query,
    upsert_installation_query,
)
from app.crm.connectivity.providers.base import ActionError, ConnectorHandshakeError
from app.crm.connectivity.providers.merchant_http.actions import Request, RequestArgs
from app.crm.connectivity.providers.merchant_http.onboard import (
    MerchantHttpOnboarder,
    OnboardMerchantHttpRequest,
    normalised_account_id,
    normalised_base_url,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.connectivity.schemas.message import CredentialBundle
from app.crm.connectivity.status import INSTALLATION_DISABLED, INSTALLATION_USABLE
from app.crm.outreach.db import DbTxn
from app.crm.outreach.nodes.action import execute as execute_action
from app.crm.outreach.nodes.context import run_facts
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import (
    EnrollmentRun,
    Workflow,
    WorkflowDefinition,
    WorkflowNode,
)
from app.crm.record.catalog import code_entries
from app.crm.record.schemas import CatalogField
from tests.crm.doubles import patch_accessors, stub_http

# --- the registry ------------------------------------------------------------


def test_the_connector_is_registered_with_one_verb_and_no_channel() -> None:
    spec = CONNECTORS["merchant_http"]
    assert spec.channel is None and spec.templates is None
    assert action_names("merchant_http") == ["request"]
    assert action_declares(
        "merchant_http",
        "request",
        {"path": "/x", "facts": {"payment_link": "data.link"}},
    ) == ["payment_link"]
    assert action_declares("merchant_http", "request", {"path": "/x"}) == []
    assert action_declares("shopify", "add_tag", {"order_id": "1", "tags": ["a"]}) == []


# --- the door ----------------------------------------------------------------


def test_the_base_url_is_normalised_and_only_https() -> None:
    assert (
        normalised_base_url("HTTPS://Api.Example.com/v1/")
        == "https://api.example.com/v1"
    )
    for bad, why in (
        ("http://api.example.com", "https"),
        ("https://api.example.com/x?y=1", "query"),
        ("https://user:pw@api.example.com", "credentials"),
        ("https://{host}/x", "placeholders"),
        ("https:///x", "host"),
    ):
        with pytest.raises(ValueError, match=why):
            normalised_base_url(bad)


def test_the_external_account_id_is_a_plain_stable_name() -> None:
    """A door is addressed by its STABLE merchant-chosen name — trimmed, no
    placeholders, no URL/id-looking characters. That is what lets a base-URL
    edit stay an in-place update on the SAME row: the name a plan pins keeps
    resolving forever, so editing the URL never forces a republish."""
    assert normalised_account_id("  Payments  ") == "Payments"
    assert normalised_account_id("credit-line") == "credit-line"
    for bad, why in (
        ("https://api.example.com/v1", "/"),  # a base URL is never a name
        ("{host}", "placeholder"),
        ("a b", "/"),
        ("a:b", ":"),
    ):
        with pytest.raises(ValueError, match=why):
            normalised_account_id(bad)

    # A name is REQUIRED at the schema boundary — a door with no addressable
    # name is not a doorway anyone could pin a plan to.
    with pytest.raises(ValidationError, match="name"):
        OnboardMerchantHttpRequest(
            merchant_id="m1", name="", base_url="https://a.example.com"
        )


def test_the_auth_header_and_value_come_together() -> None:
    OnboardMerchantHttpRequest(
        merchant_id="m1", name="payments", base_url="https://a.example.com"
    )
    OnboardMerchantHttpRequest(
        merchant_id="m1",
        name="payments",
        base_url="https://a.example.com",
        auth_header="Authorization",
        auth_value="Bearer t",
    )
    with pytest.raises(ValidationError, match="together"):
        OnboardMerchantHttpRequest(
            merchant_id="m1",
            name="payments",
            base_url="https://a.example.com",
            auth_header="X-Key",
        )


def test_an_auth_header_must_be_a_clean_http_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CWE-113: a malformed ``auth_header`` must not reach the wire — a space,
    a newline, a ``:`` or any other non-token character would either be rejected
    by aiohttp at send time (loudly, from deep inside the action) or could be
    read as a second header. Refuse at the console instead."""
    for bad in (
        "X Key",
        "X\r\nSet-Cookie: x=1",
        "X:y",
        "X\tBeta",
    ):
        with pytest.raises(ValidationError, match="header name"):
            OnboardMerchantHttpRequest(
                merchant_id="m1",
                name="payments",
                base_url="https://a.example.com",
                auth_header=bad,
                auth_value="Bearer t",
            )
    # Legitimate token names still pass.
    OnboardMerchantHttpRequest(
        merchant_id="m1",
        name="payments",
        base_url="https://a.example.com",
        auth_header="X-Api-Key",
        auth_value="sk_live_1",
    )


def test_an_auth_value_may_not_carry_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request splitting: CR/LF (and other control characters) in the value would
    let one header stop and a forged one begin when the request is built."""
    for bad in ("Bearer t\r\nSet-Cookie: admin=1", "a\nb", "Bearer\t\rd", "x\x00y"):
        with pytest.raises(ValidationError, match="control characters"):
            OnboardMerchantHttpRequest(
                merchant_id="m1",
                name="payments",
                base_url="https://a.example.com",
                auth_header="Authorization",
                auth_value=bad,
            )


def test_a_credential_may_not_claim_a_reserved_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``host`` would aim the egress guard at another wall; ``idempotency-key``
    and ``accept`` are slots the request action itself issues. A credential must
    never be allowed to clobber them, whatever the case."""
    for reserved in ("Host", "host", "IDEMPOTENCY-Key", "Accept", "accept"):
        with pytest.raises(ValidationError, match="reserved"):
            OnboardMerchantHttpRequest(
                merchant_id="m1",
                name="payments",
                base_url="https://a.example.com",
                auth_header=reserved,
                auth_value="Bearer t",
            )


def _public_dns(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (ip, port or 443))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(ssrf, "_ALLOW_PRIVATE_EGRESS", False)


def test_onboarding_refuses_an_address_the_guard_would_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cheap check, at the console, so a merchant hears it while typing
    rather than parking every run that names the connector."""
    onboarder = MerchantHttpOnboarder()
    _public_dns(monkeypatch, ip="10.0.0.5")
    request = OnboardMerchantHttpRequest(
        merchant_id="m1", name="internal", base_url="https://internal.example.com"
    )
    with pytest.raises(ConnectorHandshakeError, match="base_url refused"):
        asyncio.run(onboarder.gather(request))

    _public_dns(monkeypatch)
    request = OnboardMerchantHttpRequest(
        merchant_id="m1",
        name="credit-line",
        base_url="https://api.example.com/v1",
        auth_header="Authorization",
        auth_value="Bearer t",
        display_label="Credit line API",
    )
    result = asyncio.run(onboarder.gather(request))
    # The door's provider id is its STABLE NAME — what a plan addresses,
    # never the base URL. The URL lives in door_data, so editing it is an
    # in-place change to the same row and no plan has to republish.
    assert result.external_account_id == "credit-line"
    assert result.door_data == {"base_url": "https://api.example.com/v1"}
    assert result.address is None and result.health_level == "healthy"
    assert result.bundle == {"auth_header": "Authorization", "auth_value": "Bearer t"}
    assert onboarder.identify(request) == ("credit-line", None)


# --- the verb's contract -----------------------------------------------------


def test_the_path_is_a_path_and_facts_are_plain_names() -> None:
    RequestArgs(path="/credit-line/link", body={"customer_id": "{customer_id}"})
    for bad in ("credit-line", "https://x.com/a", "//x"):
        with pytest.raises(ValidationError):
            RequestArgs(path=bad)
    with pytest.raises(ValidationError, match="identifier"):
        RequestArgs(path="/x", facts={"payment link": "data.link"})
    assert RequestArgs(path="/x").method == "POST"


def test_path_rejects_dot_segments_that_traverse_out_of_the_prefix() -> None:
    """CWE-22: the path is concatenated onto the onboarded base URL
    (``base_url + path``), and a ``..`` in the middle lets the merchant's server
    normalise it back OUT of the configured prefix — ``/v1/../admin`` lands on
    ``/admin``. A dot segment is not a path an author may name, so it is refused
    here, at the args boundary, before a run ever forms the URL."""
    for bad in ("/../admin", "/a/../b", "/./a", "/a/../../etc/passwd", "/../"):
        with pytest.raises(ValidationError, match="dot segment"):
            RequestArgs(path=bad)
    # A plain segment that merely *contains* a dot is fine — it is not a
    # traversal primitive.
    RequestArgs(path="/v1.2/hook", body={"k": "v"})


# --- performing it -----------------------------------------------------------


class _ResponseBody:
    """Like aiohttp's StreamReader: read(n) returns what one network chunk
    buffered (at most n), and b"" only at EOF — never the whole body at once."""

    def __init__(self, payload: bytes, chunk_size: int) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self._pos = 0
        self.consumed = 0

    async def read(self, n: int = -1) -> bytes:
        size = self._chunk_size if n < 0 else min(n, self._chunk_size)
        chunk = self._payload[self._pos : self._pos + size]
        self._pos += len(chunk)
        self.consumed += len(chunk)
        return chunk


class _Response:
    def __init__(
        self,
        status: int,
        payload: bytes,
        charset: Optional[str] = None,
        chunk_size: int = 7,
    ) -> None:
        self.status = status
        self.charset = charset
        self.content = _ResponseBody(payload, chunk_size)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    status: int = 200,
    text: str = '{"data": {"link": "https://pay.example.com/abc", "n": 3}}',
    *,
    body: Optional[bytes] = None,
    charset: Optional[str] = None,
    bundle: Optional[Dict[str, Any]] = None,
    raises: Optional[BaseException] = None,
    responses: Optional[List[_Response]] = None,
) -> List[Dict[str, Any]]:
    """Stub the seam: the guarded request (no network) and the vault read.
    Each response is appended to ``responses`` so a test can inspect reads."""
    sent: List[Dict[str, Any]] = []
    payload = text.encode("utf-8") if body is None else body

    @asynccontextmanager
    async def fake_request(session, method, url, **kwargs):
        sent.append({"method": method, "url": url, **kwargs})
        if raises is not None:
            raise raises
        response = _Response(status, payload, charset)
        if responses is not None:
            responses.append(response)
        yield response

    @asynccontextmanager
    async def fake_session(**kwargs):
        yield object()

    async def fake_bundle(installation):
        return CredentialBundle(values=bundle or {})

    monkeypatch.setattr(face_module, "ssrf_safe_request", fake_request)
    monkeypatch.setattr(face_module, "create_aiohttp_session", fake_session)
    monkeypatch.setattr(face_module, "bundle_for", fake_bundle)
    return sent


def _door(credential: Optional[str] = "cred-1") -> ConnectorInstallation:
    return ConnectorInstallation(
        id="inst-1",
        merchant_id="m1",
        connector_key="merchant_http",
        external_account_id="credit-line",
        door_data={"base_url": "https://api.example.com/v1"},
        display_label="Credit line API",
        credential_id=credential,
        status="active",
    )


_CTX = {"run_id": "run-1", "node_id": "get-link"}
_ARGS = RequestArgs(
    path="/credit-line/link",
    body={"customer_id": "FK1"},
    facts={"payment_link": "data.link", "count": "data.n"},
)


def test_the_request_carries_the_auth_header_and_the_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(
        monkeypatch, bundle={"auth_header": "Authorization", "auth_value": "Bearer t"}
    )
    result = asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))
    assert result == {
        "ok": True,
        "status": 200,
        "facts": {"payment_link": "https://pay.example.com/abc", "count": 3},
    }
    (call,) = sent
    assert call["method"] == "POST"
    assert call["url"] == "https://api.example.com/v1/credit-line/link"
    assert call["json"] == {"customer_id": "FK1"}
    assert call["headers"]["Authorization"] == "Bearer t"
    assert call["headers"]["Idempotency-Key"] == "run-1:get-link:1"
    # the guard is told which host is allowed: a redirect off it loses the auth
    assert call["allowed_host_suffixes"] == ["api.example.com"]


def test_an_ipv6_literal_base_url_derives_the_full_host_for_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allow-list suffix must be what the egress guard compares against —
    the bare IPv6 hostname, not a ":"-split fragment. ``urlsplit(...).hostname``
    yields ``2001:db8::1`` for ``https://[2001:db8::1]``; a naive ``base_url.split(":")``
    would truncate it to ``[2001`` and the guard would refuse the initial hop."""
    sent = _install(monkeypatch)
    door = ConnectorInstallation(
        id="inst-1",
        merchant_id="m1",
        connector_key="merchant_http",
        external_account_id="ipv6",
        door_data={"base_url": "https://[2001:db8::1]/v1"},
        display_label="IPv6 API",
        credential_id=None,
        status="active",
    )
    asyncio.run(Request().perform("m1", door, _ARGS, _CTX))
    call = sent[0]
    assert call["allowed_host_suffixes"] == ["2001:db8::1"]
    assert call["url"] == "https://[2001:db8::1]/v1/credit-line/link"


def test_a_door_without_a_credential_sends_no_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(monkeypatch)
    asyncio.run(Request().perform("m1", _door(credential=None), _ARGS, _CTX))
    assert "Authorization" not in sent[0]["headers"]


def test_a_credential_cannot_clobber_a_reserved_header_even_when_hand_edited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: onboarding refuses reserved header names, but a row
    written past that check must still not be allowed to overwrite the slots the
    action issues itself — the credential header is silently dropped."""
    for name in ("Host", "idempotency-key", "Accept"):
        sent = _install(
            monkeypatch, bundle={"auth_header": name, "auth_value": "smuggled"}
        )
        asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))
        headers = {k.lower(): v for k, v in sent[0]["headers"].items()}
        assert headers.get(name.lower()) != "smuggled"
        # The action's own slot survives.
        assert headers["idempotency-key"] == "run-1:get-link:1"


def test_a_get_sends_query_and_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _install(monkeypatch, text='{"data": {"link": "L"}}')
    args = RequestArgs(
        path="/link",
        method="GET",
        query={"c": "FK1"},
        facts={"payment_link": "data.link"},
    )
    asyncio.run(Request().perform("m1", _door(), args, _CTX))
    assert sent[0]["params"] == {"c": "FK1"} and sent[0]["json"] is None


def test_no_door_is_a_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    with pytest.raises(ActionError, match="no merchant_http endpoint"):
        asyncio.run(Request().perform("m1", None, _ARGS, _CTX))


def test_failures_are_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx, a missing declared fact, a non-JSON answer and an egress refusal
    are DEFECTS (the `failed` arrow, or a parked run); 5xx, 429 and the
    network are BAD MOMENTS for the walker's ladder."""
    _install(monkeypatch, status=404, text="no such customer")
    with pytest.raises(ActionError, match="refused \\(404\\)"):
        asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))

    _install(monkeypatch, text='{"data": {}}')
    with pytest.raises(ActionError, match="no scalar at 'data.link'"):
        asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))

    _install(monkeypatch, text='{"data": {"link": {"nested": 1}, "n": 3}}')
    with pytest.raises(ActionError, match="no scalar"):
        asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))

    _install(monkeypatch, text="<html>")
    with pytest.raises(ActionError, match="not JSON"):
        asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))

    _install(monkeypatch, raises=ssrf.SSRFError("Blocked egress to private address"))
    with pytest.raises(ActionError, match="egress refused"):
        asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))

    for status in (429, 502):
        _install(monkeypatch, status=status, text="later")
        with pytest.raises(RuntimeError, match=str(status)):
            asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))

    _install(monkeypatch, raises=asyncio.TimeoutError())
    with pytest.raises(RuntimeError, match="unreachable"):
        asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))


def test_an_answer_with_no_declared_facts_is_just_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fire-and-forget notification: no facts asked, the body is not read."""
    _install(monkeypatch, status=204, text="")
    result = asyncio.run(
        Request().perform("m1", _door(), RequestArgs(path="/nudged"), _CTX)
    )
    assert result == {"ok": True, "status": 204, "facts": {}}


def test_an_oversized_answer_is_a_defect_not_a_buffered_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: ``response.text()`` read the whole body into memory, bounded only by
    the 20s timeout — a hostile or broken endpoint could stream gigabytes and OOM
    the walker, stalling other merchants' runs until the lease expired and
    re-sent the request. Now the read is capped: one byte over the ceiling is a
    DEFECT (ActionError), so the action node parks instead of retrying a body
    that will not shrink."""
    _install(
        monkeypatch,
        status=200,
        body=b"x" * (face_module.MAX_RESPONSE_BYTES + 1),
        text='{"data": {"n": 1}}',  # shadowed by `body`
    )
    with pytest.raises(ActionError, match="answer too large"):
        asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))

    # A body at or under the ceiling parses as before — the cap is a REFUSAL of
    # the pathological stream, not a smaller allowance for good answers.
    _install(
        monkeypatch, text='{"data": {"link": "https://pay.example.com/abc", "n": 3}}'
    )
    result = asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))
    assert result["facts"] == {
        "payment_link": "https://pay.example.com/abc",
        "count": 3,
    }


def test_a_chunked_answer_is_read_to_eof_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: one ``content.read(n)`` returns only what is buffered, so a JSON
    answer arriving in several chunks was truncated and misread as "not JSON".
    The double emits 7-byte chunks; the whole body must still parse — including
    one exactly at the ceiling."""
    responses: List[_Response] = []
    text = '{"data": {"link": "https://pay.example.com/abc", "n": 3}}'
    _install(monkeypatch, text=text, responses=responses)
    result = asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))
    assert result["facts"]["payment_link"] == "https://pay.example.com/abc"
    assert responses[0].content.consumed == len(text.encode())

    prefix = '{"data": {"link": "L", "n": 3}, "pad": "'
    exact = prefix + "x" * (face_module.MAX_RESPONSE_BYTES - len(prefix) - 2) + '"}'
    assert len(exact.encode()) == face_module.MAX_RESPONSE_BYTES
    _install(monkeypatch, text=exact)
    result = asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))
    assert result["facts"] == {"payment_link": "L", "count": 3}


def test_an_oversized_bad_moment_is_still_retried_and_reads_only_an_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429/5xx is classified BEFORE the size cap: an oversized error page is
    still a bad moment for the retry ladder, never a defect, and only the
    excerpt the error message carries is read off the wire."""
    for status in (429, 503):
        responses: List[_Response] = []
        _install(
            monkeypatch,
            status=status,
            body=b"e" * (face_module.MAX_RESPONSE_BYTES * 2),
            responses=responses,
        )
        with pytest.raises(RuntimeError, match=f"answered {status}"):
            asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))
        assert responses[0].content.consumed <= face_module._EXCERPT + 1


def test_the_idempotency_key_carries_the_visit_so_a_revisit_is_a_new_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: the Idempotency-Key was only `run:node`, so a run that REVISITED the
    square (create link -> wait -> no payment -> create link) sent the same key
    and a merchant honouring it replayed the FIRST answer — an expired payment
    link. Now the key is `run:node:visit`: a lease retry of the same visit keeps
    its key (the counter's write was the one lost), but a revisit — the counter
    having persisted — sends a new one."""
    sent = _install(monkeypatch)

    # The walker re-runs the SAME visit (lease retry): the counter is absent or
    # unchanged, so the key re-derives identically.
    asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))
    asyncio.run(
        Request().perform(
            "m1",
            _door(),
            _ARGS,
            {"run_id": "run-1", "node_id": "get-link", "node_visits": 1},
        )
    )
    assert sent[0]["headers"]["Idempotency-Key"] == "run-1:get-link:1"
    assert sent[1]["headers"]["Idempotency-Key"] == "run-1:get-link:1"

    # A revisit advances the counter -> a NEW key, so the merchant's record is
    # not replayed for a brand-new write.
    asyncio.run(
        Request().perform(
            "m1",
            _door(),
            _ARGS,
            {"run_id": "run-1", "node_id": "get-link", "node_visits": 2},
        )
    )
    assert sent[2]["headers"]["Idempotency-Key"] == "run-1:get-link:2"


def test_a_credential_that_will_not_decrypt_is_a_defect_not_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: `bundle_for` raises AccountError (a plain Exception) for a missing,
    deactivated or undecryptable credential, and the action node parks only on
    ActionError — so a dead credential walked the whole retry ladder, re-sending
    a request whose credential will not change. Convert it so the run parks."""
    from app.crm.connectivity import accounts

    async def broken_bundle(door):
        raise accounts.AccountError(
            "this connection's credential is missing or unreadable"
        )

    _install(monkeypatch)
    monkeypatch.setattr(face_module, "bundle_for", broken_bundle)
    with pytest.raises(ActionError, match="credential unusable"):
        asyncio.run(Request().perform("m1", _door(), _ARGS, _CTX))


# --- installation lookup (no database) ---------------------------------------


_ROW: Dict[str, Any] = {
    "id": "00000000-0000-0000-0000-000000009001",
    "merchant_id": "m1",
    "connector_key": "merchant_http",
    "external_account_id": "payments",
    "door_data": {"base_url": "https://pay.example.com/v1"},
    "display_label": "Payments API",
    "credential_id": None,
    "status": "healthy",
    "token_expires_at": None,
}


def _pool(rows: List[dict]):
    """A stand-in for crm_connection(): an async context manager yielding a
    conn whose fetchrow returns the first stubbed row, or None when empty."""

    class _Conn:
        async def fetchrow(self, *a, **k):
            return dict(rows[0]) if rows else None

    class _Pool:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a, **k):
            return None

    return _Pool


def test_pinned_external_account_id_is_passed_to_the_query_verbatim(
    monkeypatch,
) -> None:
    """A plan's pinned external_account_id reaches the SQL builder UNCHANGED, as plain
    text — not a UUID, not a URL — so the `external_account_id = $3` branch runs for the
    named door. This is the whole change of issue 3: the pin is a name and a
    base-URL edit is an in-place update on the same row, so the name keeps
    resolving."""
    seen: List[Optional[str]] = []

    def _query(merchant_id: str, connector_key: str, external_account_id=None):
        seen.append(external_account_id)
        return "SELECT 1", [merchant_id, connector_key, external_account_id]

    monkeypatch.setattr(accessor, "installation_for_connector_query", _query)
    monkeypatch.setattr(accessor, "crm_connection", _pool([dict(_ROW)]))

    door = asyncio.run(
        accessor.get_installation_for_connector(
            "m1", "merchant_http", external_account_id="payments"
        )
    )
    assert seen == ["payments"]
    assert isinstance(door, ConnectorInstallation)
    assert door.external_account_id == "payments"
    assert door.door_data["base_url"] == "https://pay.example.com/v1"


def test_absent_external_account_id_takes_the_newest_door_branch(monkeypatch) -> None:
    """No pinned name -> the connector's newest usable door branch; the value
    passed to the builder is None, unchanged behaviour."""
    seen: List[Optional[str]] = []

    def _query(merchant_id: str, connector_key: str, external_account_id=None):
        seen.append(external_account_id)
        return "SELECT 1", [merchant_id, connector_key, external_account_id]

    monkeypatch.setattr(accessor, "installation_for_connector_query", _query)
    monkeypatch.setattr(accessor, "crm_connection", _pool([dict(_ROW)]))

    door = asyncio.run(accessor.get_installation_for_connector("m1", "merchant_http"))
    assert seen == [None]
    assert door is not None and door.external_account_id == "payments"


def test_account_selection_uses_existing_tenant_connector_and_status_guards() -> None:
    query, values = installation_for_connector_query("m1", "merchant_http", "payments")
    assert "merchant_id = $1" in query
    assert "connector_key = $2" in query
    assert "external_account_id = $3" in query
    assert "status = ANY($4::text[])" in query
    assert "ORDER BY" not in query
    assert values == ["m1", "merchant_http", "payments", sorted(INSTALLATION_USABLE)]
    assert "door_name" not in query


def test_account_upsert_preserves_identity_and_only_updates_address_data() -> None:
    query, values = upsert_installation_query(
        "m1",
        "merchant_http",
        "payments",
        '{"base_url":"https://new.example.com"}',
        "Payments",
        None,
        "healthy",
        None,
        "{}",
    )
    assert "ON CONFLICT (merchant_id, connector_key, external_account_id)" in query
    assert "door_data = EXCLUDED.door_data" in query
    assert "external_account_id =" not in query
    assert "door_name" not in query
    assert "status <> $10" in query
    assert len(values) == 10
    assert values[2] == "payments"
    assert values[3] == '{"base_url":"https://new.example.com"}'
    assert values[-1] == INSTALLATION_DISABLED


def test_base_url_edits_keep_the_same_external_account_id(monkeypatch) -> None:
    _public_dns(monkeypatch)
    onboarder = MerchantHttpOnboarder()
    results = [
        asyncio.run(
            onboarder.gather(
                OnboardMerchantHttpRequest(
                    merchant_id="m1",
                    name="payments",
                    base_url=url,
                )
            )
        )
        for url in ("https://old.example.com/v1", "https://new.example.com/v2")
    ]
    assert [result.external_account_id for result in results] == [
        "payments",
        "payments",
    ]
    assert [result.door_data["base_url"] for result in results] == [
        "https://old.example.com/v1",
        "https://new.example.com/v2",
    ]
    assert all("door_name" not in result.model_dump() for result in results)


@pytest.mark.parametrize("name", ["", " ", "\t"])
def test_onboarding_refuses_a_blank_account_name(name: str) -> None:
    with pytest.raises(ValidationError, match="name"):
        OnboardMerchantHttpRequest(
            merchant_id="m1", name=name, base_url="https://api.example.com"
        )


def test_no_row_reads_as_no_usable_door(monkeypatch) -> None:
    """No matching row (a name nobody onboarded, a withdrawn door, a name from
    another tenant) — the query builder is reached, the fetch yields nothing,
    and None is the fail-closed answer the action root turns into a refusal."""
    monkeypatch.setattr(
        accessor,
        "installation_for_connector_query",
        lambda m, c, external_account_id=None: (
            "SELECT 1",
            [m, c, external_account_id],
        ),
    )
    monkeypatch.setattr(accessor, "crm_connection", _pool([]))

    door = asyncio.run(
        accessor.get_installation_for_connector(
            "m1", "merchant_http", external_account_id="no-such-door"
        )
    )
    assert door is None


# --- action dispatch and door selection -------------------------------------


SECRET = "shhh"


RELAY = "https://nautilus.example/apps/breeze-buddy/webhooks/clairvoyance"


def _installation(credential_id: Optional[str] = None) -> ConnectorInstallation:
    return ConnectorInstallation(
        id="inst-1",
        merchant_id="m1",
        connector_key="shopify",
        external_account_id="acme.myshopify.com",
        display_label="Acme",
        credential_id=credential_id,
        status="healthy",
    )


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment's one relay address and the shared signing key."""
    monkeypatch.setattr(via_nautilus, "NAUTILUS_WEBHOOK_URL", RELAY)
    monkeypatch.setattr(via_nautilus, "ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY", SECRET)


async def test_an_action_node_may_pin_which_door_to_use(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    """The one transport an action node may name: when a merchant holds
    several base_urls on merchant_http, the plan says WHICH door by its STABLE
    name, and that name is exactly what reaches the accessor — not the
    connector's newest."""
    seen: List[Optional[str]] = []

    async def _get(
        merchant_id: str, connector_key: str, external_account_id: Optional[str] = None
    ) -> ConnectorInstallation:
        seen.append(external_account_id)
        return _installation()

    monkeypatch.setattr(actions_module, "get_installation_for_connector", _get)
    stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(200, json={"ok": True})
    )

    await perform_action(
        "m1",
        "shopify",
        "add_tag",
        {"order_id": "1", "tags": ["a"]},
        {"run_id": "r-1", "node_id": "tag-1"},
        "payments",
    )
    assert seen == ["payments"]


async def test_an_action_without_a_pinned_door_asks_the_connector_default(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    """Absent external_account_id on the node, the connector's newest usable door
    is chosen — perform_action forwards None, unchanged behaviour."""
    seen: List[Optional[str]] = []

    async def _get(
        merchant_id: str, connector_key: str, external_account_id: Optional[str] = None
    ) -> ConnectorInstallation:
        seen.append(external_account_id)
        return _installation()

    monkeypatch.setattr(actions_module, "get_installation_for_connector", _get)
    stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(200, json={"ok": True})
    )

    await perform_action(
        "m1",
        "shopify",
        "add_tag",
        {"order_id": "1", "tags": ["a"]},
        {"run_id": "r-1", "node_id": "tag-1"},
    )
    assert seen == [None]


async def test_a_pinned_door_that_resolves_to_nothing_fails_closed(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    """A foreign-door name, a door of the wrong connector, or a withdrawn one
    all read the same: the accessor answers None and the verb refuses, so a
    leak from another merchant can never act through its door."""

    async def _get(
        merchant_id: str, connector_key: str, external_account_id: Optional[str] = None
    ) -> Optional[ConnectorInstallation]:
        return None

    monkeypatch.setattr(actions_module, "get_installation_for_connector", _get)
    seen = stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(200, json={"ok": True})
    )

    with pytest.raises(ActionError, match="no usable 'shopify' connection"):
        await perform_action(
            "m1",
            "shopify",
            "add_tag",
            {"order_id": "1", "tags": ["a"]},
            {"run_id": "r-1", "node_id": "tag-1"},
            "not-m1s-payments",
        )
    assert seen == []


# --- workflow execution: facts, door names, and visit keys ------------------


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


_DEFINITION = WorkflowDefinition(
    entry={"topic": "checkout.initiated"},
    nodes=[
        {
            "id": "tag-vip",
            "type": "action",
            "connector": "shopify",
            "action": "add_tag",
            "args": {"order_id": "{id}", "tags": ["vip"]},
        }
    ],
    edges=[],
    goals=[{"topics": ["order.placed"]}],
)


def _run(context: Dict[str, Any]) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node="tag-vip",
        wake_at=NOW,
        entered_at=NOW - timedelta(minutes=5),
        exited_at=None,
        exit_reason=None,
        context=context,
        enrollment_key="c-1",
        attempts=1,
        last_error=None,
    )


def _node(**overrides: Any) -> WorkflowNode:
    fields: Dict[str, Any] = {
        "id": "tag-vip",
        "type": "action",
        "connector": "shopify",
        "action": "add_tag",
        "args": {"order_id": "{id}", "tags": ["vip"]},
    }
    fields.update(overrides)
    return WorkflowNode(**fields)


def _install_action(
    monkeypatch: pytest.MonkeyPatch, calls: List[Dict[str, Any]], **kw: Any
):
    """Stand in for connectivity's contract, the only thing this node calls."""

    async def fake_perform(
        merchant_id: str,
        connector_key: str,
        action: str,
        args: Dict[str, Any],
        context: Dict[str, Any],
        external_account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        calls.append(
            {
                "merchant_id": merchant_id,
                "connector": connector_key,
                "action": action,
                "args": args,
                "context": context,
                "external_account_id": external_account_id,
            }
        )
        if "raises" in kw:
            raise kw["raises"]
        # Connectivity returns {ok, status, facts}; the facts are the
        # response values the author declared readable (a merchant_http
        # `facts` map), and the whole result is the marker too.
        return {"ok": True, "facts": kw.get("facts", {})}

    monkeypatch.setattr(action_node, "perform_action", fake_perform)


async def test_a_node_that_names_a_door_carries_its_name_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one piece of transport an action node may name: when a merchant
    holds several base_urls on merchant_http, WHICH door is a plan's call, so
    external_account_id must reach the connector untouched (connectivity validates it).
    It is a STABLE name, never the row id or the base URL — a base-URL edit
    on the same door therefore needs no republish."""
    calls: List[Dict[str, Any]] = []
    _install_action(monkeypatch, calls)
    run = _run({"id": "1"})

    await execute_action(
        run,
        _node(
            connector="merchant_http",
            action="request",
            external_account_id="payments",
        ),
        _DEFINITION,
    )

    assert calls[0]["connector"] == "merchant_http"
    assert calls[0]["external_account_id"] == "payments"


async def test_declared_facts_reach_a_later_square(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason an action carries `facts` at all: the response values the
    author declared readable become run facts under their own names, so a
    later square's `{payment_link}` (a send variable, an args placeholder)
    resolves — the marker stays under `action_<node>`, the values ride atop
    the context for the rest of the run."""
    calls: List[Dict[str, Any]] = []
    _install_action(monkeypatch, calls, facts={"payment_link": "https://pay.example/l"})
    run = _run({"id": "order-1"})
    patch = await execute_action(run, _node(), _DEFINITION)

    written = run_facts({**run.context, **patch})
    assert written["payment_link"] == "https://pay.example/l"
    # The marker is still there for whoever triages the run…
    assert patch["action_tag-vip"] == {
        "ok": True,
        "facts": {"payment_link": "https://pay.example/l"},
    }
    # …but stays out of the facts a template or payload may touch.
    assert "action_tag-vip" not in written


async def test_a_fact_that_shadows_the_walkers_own_bookkeeping_is_never_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version predating the publish guard (catalog_laws refuses the name)
    must not be able to overwrite `phone` or `source_event_id`: the write
    path refuses bookkeeping names even though the publish guard is the
    place the author hears the collision."""
    calls: List[Dict[str, Any]] = []
    _install_action(monkeypatch, calls, facts={"phone": "+1-555"})
    run = _run({"id": "1", "phone": "keep-me"})
    patch = await execute_action(run, _node(), _DEFINITION)

    # The fraudulent fact never reaches the write path, so the run's own
    # `phone` (a bookkeeping key) is untouched when the patch is applied.
    assert "phone" not in patch
    assert {**run.context, **patch}["phone"] == "keep-me"


async def test_the_visit_counter_drives_a_new_idempotency_key_on_revisit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idempotency key's third part, maintained HERE (the square owns the
    run's context) so every connector gets per-visit keys. A lease retry of the
    SAME visit — the write that would advance the counter was the one lost — re-
    derives the same number; a revisit, the counter having landed, advances."""
    calls: List[Dict[str, Any]] = []
    _install_action(monkeypatch, calls)

    # First visit: no counter in the run's context -> visit 1.
    run = _run({"id": "1"})
    patch = await execute_action(run, _node(), _DEFINITION)
    assert patch["action_visits_tag-vip"] == 1
    assert calls[0]["context"]["node_visits"] == 1
    # The counter is bookkeeping: a template never sees it.
    assert "action_visits_tag-vip" not in run_facts({**run.context, **patch})

    # Lease retry: the counter's write did not land, so the run's context is
    # unchanged -> visit 1 again, the SAME idempotency key the merchant already
    # answered.
    calls.clear()
    run = _run({"id": "1"})
    patch = await execute_action(run, _node(), _DEFINITION)
    assert calls[0]["context"]["node_visits"] == 1
    assert patch["action_visits_tag-vip"] == 1

    # Revisit: the counter PERSISTED (the write landed), the run looped back ->
    # visit 2, a NEW key, so a merchant honouring idempotency does not replay
    # the first answer (e.g. an expired payment link) for a brand-new write.
    calls.clear()
    run = _run({"id": "1", action_node._visits_key("tag-vip"): 1})
    patch = await execute_action(run, _node(), _DEFINITION)
    assert calls[0]["context"]["node_visits"] == 2
    assert patch["action_visits_tag-vip"] == 2


# --- workflow publishing and declared response facts ------------------------


PUBLISH_NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)


def _definition(**overrides):
    base = {
        "entry": {
            "topic": "checkout.initiated",
            "reenter": True,
            "cooldown_hours": 0,
        },
        "nodes": [
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {"id": "rescue-call", "type": "call", "template_id": "tpl-1"},
        ],
        "edges": [["wait-30m", "rescue-call"]],
        "goal": {"topics": ["order.placed"]},
    }
    base.update(overrides)
    return base


_COD = {
    "entry": {
        "topic": "orders/create",
        "where": [{"field": "payload.gateway", "op": "is", "value": "COD"}],
    },
    "nodes": [
        {
            "id": "ask",
            "type": "wait",
            "topics": ["button.reply"],
            "key": "button_id",
            "minutes": 60,
        },
        {"id": "confirm", "type": "wait", "minutes": 1},
        {"id": "call", "type": "wait", "minutes": 1},
    ],
    "edges": [["ask", "confirm", "YES"], ["ask", "call", "timeout"]],
    "goal": {"topics": ["order.confirmed"]},
}


_TERSE_DRAFT = {
    "entry": {"topic": "checkout.initiated"},
    "nodes": [{"id": "w", "type": "wait", "minutes": 30}],
    "edges": [],
    "goal": {"topics": ["order.placed"]},
}


def _workflow(status: str, definition) -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="plan",
        status=status,
        version=0 if definition is None else 1,
        created_by=None,
        created_at=PUBLISH_NOW,
        updated_at=PUBLISH_NOW,
        definition=definition,
        draft=_TERSE_DRAFT,
    )


class _PublishAccessor:
    """The accessor slice _publish_in_txn touches: a live plan with a draft
    waiting, no occupied squares, and apply_publish recording whether the
    copy happened."""

    def __init__(self, draft: Dict[str, Any]) -> None:
        self.draft = draft
        self.published = False
        self.locked: List[List[Tuple[str, str]]] = []
        self.order: List[str] = []

    async def workflow_for_publish(self, conn: Any, m: str, w: str) -> Workflow:
        return _workflow("live", _TERSE_DRAFT).model_copy(update={"draft": self.draft})

    async def occupied_nodes(self, conn: Any, m: str, w: str) -> List[str]:
        return []

    async def lock_templates_shared(
        self, conn: Any, m: str, templates: List[Tuple[str, str]]
    ) -> None:
        self.locked.append(list(templates))
        self.order.append("lock")

    async def apply_publish(self, conn: Any, m: str, w: str) -> Workflow:
        self.published = True
        self.order.append("publish")
        return _workflow("live", self.draft)

    async def insert_version(self, conn: Any, *args: Any) -> None:
        return None  # phase 11's row; pinned by test_workflow_versions.py

    async def repin_open_runs(self, conn: Any, *args: Any) -> int:
        return 0


async def _publish(accessor: _PublishAccessor) -> Workflow:
    return await plans._publish_in_txn(cast(DbTxn, object()), "m1", "wf-1", None)


def _orders_create_catalog() -> Dict[str, CatalogField]:
    entry = next(e for e in code_entries() if e.topic == "orders/create")
    return {f.path: f for f in entry.fields}


_INSTALL_PLAN: Dict[str, Any] = {
    "entry": {"topic": "checkouts/update", "reenter": True, "cooldown_hours": 0},
    "nodes": [
        {"id": "wait-30m", "type": "wait", "minutes": 30},
        {
            "id": "hit-credit-line",
            "type": "action",
            "connector": "merchant_http",
            "action": "request",
            "external_account_id": "credit-line",
            "args": {"path": "/credit-line/link", "body": {"customer_id": "FK1"}},
        },
    ],
    "edges": [["wait-30m", "hit-credit-line"]],
    "goal": {"topics": ["orders/create"]},
    "purpose_key": "sales.credit_line.link",
}


@pytest.mark.asyncio
async def test_publish_refuses_an_action_node_whose_pinned_door_resolves_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publish-time half of door pinning: a plan that names a door must
    name a door OF THIS MERCHANT under that connector that is still usable —
    otherwise the first sign is a parked run hours later."""
    accessor = _PublishAccessor(_INSTALL_PLAN)
    patch_accessors(monkeypatch, plans, accessor)

    async def no_door(
        merchant_id: str, connector_key: str, external_account_id: Optional[str] = None
    ) -> None:
        return None

    monkeypatch.setattr(plans, "get_installation_for_connector", no_door)
    with pytest.raises(plans.WorkflowValidationError) as refused:
        await _publish(accessor)
    assert refused.value.problems == [
        "action node hit-credit-line: external_account_id credit-line names no usable "
        "door of this merchant on connector 'merchant_http'"
    ]
    assert accessor.published is False


@pytest.mark.asyncio
async def test_publish_proceeds_when_the_pinned_door_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _PublishAccessor(_INSTALL_PLAN)
    patch_accessors(monkeypatch, plans, accessor)

    async def one_door(
        merchant_id: str, connector_key: str, external_account_id: Optional[str] = None
    ) -> ConnectorInstallation:
        return ConnectorInstallation(
            id="00000000-0000-0000-0000-000000009001",
            merchant_id="m1",
            connector_key="merchant_http",
            external_account_id="credit-line",
            door_data={"base_url": "https://api.example.com/v1"},
            display_label="Credit line API",
            credential_id="cred-1",
            status="healthy",
        )

    monkeypatch.setattr(plans, "get_installation_for_connector", one_door)
    published = await _publish(accessor)
    assert accessor.published is True and published.status == "live"


def test_a_malformed_external_account_id_is_refused_at_the_schema_boundary() -> None:
    """A door is addressed by its STABLE merchant-chosen name — never a URL,
    never a placeholder, never a row id. A base_url or a '{placeholder}' is
    not a doorway anyone could name, so the schema refuses it BEFORE publish
    or check_draft can forward it to a lookup that silently matches nothing —
    that would otherwise surface as a parked run later, or a plan that names
    a door it can never resolve."""
    malformed = _definition(
        nodes=[
            {
                "id": "hit-credit-line",
                "type": "action",
                "connector": "merchant_http",
                "action": "request",
                "external_account_id": "https://api.example.com/v1",
                "args": {"path": "/credit-line/link", "body": {"customer_id": "FK1"}},
            }
        ],
        edges=[],
    )
    problems = validate_definition(malformed)
    assert problems and "definition shape invalid" in problems[0]
    assert "external_account_id" in problems[0]

    # A real stable name on the same shape is not a shape complaint any more —
    # it falls through to the graph laws (a plan with one node and no edges is
    # refused later, never as a shape error here).
    good = {
        **malformed,
        "nodes": [
            {
                **malformed["nodes"][0],
                "external_account_id": "credit-line",
            }
        ],
    }
    assert not any("definition shape invalid" in p for p in validate_definition(good))


def test_a_later_square_may_name_a_fact_an_action_declares() -> None:
    """The action is the only verb whose output RESTARTS the run's facts:
    the response values an action declares it will write (merchant_http's
    `facts` map) are run-produced, never catalog-declared, so a LATER
    square's `{payment_link}` is admitted by reading the connector's
    `action_declares` — without it, `{payment_link}` published cleanly and
    parked on the first run, exactly the failure the send-side check exists
    to prevent."""
    catalogs: plans.Catalogs = {"orders/create": _orders_create_catalog()}

    # Two actions: the first's request declares `payment_link`, the second
    # (a send) asks for it. The chain must publish.
    def _doc(send_variables: Dict[str, str]) -> Dict[str, Any]:
        return {
            **_COD,
            "purpose_key": "utility.order.receipt",
            "nodes": [
                {
                    "id": "link",
                    "type": "action",
                    "connector": "merchant_http",
                    "action": "request",
                    "args": {
                        "path": "/v2/payment-links",
                        "method": "POST",
                        "body": {"amount": "{id}"},
                        "facts": {"payment_link": "data.link"},
                    },
                },
                {
                    "id": "receipt",
                    "type": "send",
                    "channel": "whatsapp",
                    "template": "t",
                    "variables": send_variables,
                },
            ],
            "edges": [["link", "receipt"]],
        }

    # A send's blank right-hand side may name the action's declared fact.
    assert validate_definition(_doc({"1": "payment_link"}), catalogs=catalogs) == []

    # So may a LATER ACTION's args placeholder.
    later_action = {
        **_doc({}),
        "nodes": [
            _doc({})["nodes"][0],
            {
                "id": "mark",
                "type": "action",
                "connector": "shopify",
                "action": "add_tag",
                "args": {"order_id": "{payment_link}", "tags": ["paid"]},
            },
        ],
        "edges": [["link", "mark"]],
    }
    assert validate_definition(later_action, catalogs=catalogs) == []

    # A name nobody declares is still refused — the allow-list was not
    # thrown open, only extended with the action's honest answers.
    problems = validate_definition(_doc({"1": "payment_lik"}), catalogs=catalogs)
    assert any(
        "'payment_lik' is not a declared variable field" in p for p in problems
    ), problems

    # …and an action that declares NO facts (Shopify's tag) admits nothing.
    problems = (
        validate_definition(
            {
                **later_action,
                "nodes": [
                    {
                        "id": "tag",
                        "type": "action",
                        "connector": "shopify",
                        "action": "add_tag",
                        "args": {"order_id": "{id}", "tags": ["x"]},
                    },
                    {
                        "id": "send-after",
                        "type": "send",
                        "channel": "whatsapp",
                        "template": "t",
                        "variables": {"1": "tagged_fact"},
                    },
                ],
                "edges": [["tag", "send-after"]],
            },
            catalogs=catalogs,
        )
        != []
    )


def test_an_action_may_not_declare_a_fact_named_like_walker_bookkeeping() -> None:
    """A declared fact is merged at the run's TOP level (nodes/action.py), so
    a name that shadows the walker's own bookkeeping would overwrite `phone`
    or `source_event_id` on a live run. Refused at publish where an author
    can still edit — the write path refuses the same name for versions that
    predate this guard."""
    catalogs: plans.Catalogs = {"orders/create": _orders_create_catalog()}
    doc = {
        **_COD,
        "nodes": [
            {
                "id": "link",
                "type": "action",
                "connector": "merchant_http",
                "action": "request",
                "args": {
                    "path": "/v2/payment-links",
                    "facts": {"phone": "data.phone"},
                },
            }
        ],
        "edges": [],
    }
    problems = validate_definition(doc, catalogs=catalogs)
    assert any("shadows the walker's own bookkeeping" in p for p in problems), problems
