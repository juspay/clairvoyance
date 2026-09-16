"""merchant_http (enh A/03, the ruled shape): a merchant's own endpoint as a
connector — the door validates the address once, the one verb `request`
carries the run's facts out and the declared answer back, and every hop
crosses the egress guard. No network: the guard's DNS and the request
itself are stubbed at the seam actions.py calls.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import pytest
from pydantic import ValidationError

import app.crm.connectivity.providers.merchant_http.actions as face_module
from app.core.security import ssrf
from app.crm.connectivity.connectors import CONNECTORS
from app.crm.connectivity.contracts import action_declares, action_names
from app.crm.connectivity.providers.base import ActionError, ConnectorHandshakeError
from app.crm.connectivity.providers.merchant_http.actions import Request, RequestArgs
from app.crm.connectivity.providers.merchant_http.onboard import (
    MerchantHttpOnboarder,
    OnboardMerchantHttpRequest,
    normalised_base_url,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.connectivity.schemas.message import CredentialBundle

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


def test_the_auth_header_and_value_come_together() -> None:
    OnboardMerchantHttpRequest(merchant_id="m1", base_url="https://a.example.com")
    OnboardMerchantHttpRequest(
        merchant_id="m1",
        base_url="https://a.example.com",
        auth_header="Authorization",
        auth_value="Bearer t",
    )
    with pytest.raises(ValidationError, match="together"):
        OnboardMerchantHttpRequest(
            merchant_id="m1", base_url="https://a.example.com", auth_header="X-Key"
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
        merchant_id="m1", base_url="https://internal.example.com"
    )
    with pytest.raises(ConnectorHandshakeError, match="base_url refused"):
        asyncio.run(onboarder.gather(request))

    _public_dns(monkeypatch)
    request = OnboardMerchantHttpRequest(
        merchant_id="m1",
        base_url="https://api.example.com/v1",
        auth_header="Authorization",
        auth_value="Bearer t",
        display_label="Credit line API",
    )
    result = asyncio.run(onboarder.gather(request))
    assert result.external_account_id == "https://api.example.com/v1"
    assert result.address is None and result.health_level == "healthy"
    assert result.bundle == {"auth_header": "Authorization", "auth_value": "Bearer t"}
    assert onboarder.identify(request) == ("https://api.example.com/v1", None)


# --- the verb's contract -----------------------------------------------------


def test_the_path_is_a_path_and_facts_are_plain_names() -> None:
    RequestArgs(path="/credit-line/link", body={"customer_id": "{customer_id}"})
    for bad in ("credit-line", "https://x.com/a", "//x"):
        with pytest.raises(ValidationError):
            RequestArgs(path=bad)
    with pytest.raises(ValidationError, match="identifier"):
        RequestArgs(path="/x", facts={"payment link": "data.link"})
    assert RequestArgs(path="/x").method == "POST"


# --- performing it -----------------------------------------------------------


class _Response:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text


def _install(
    monkeypatch: pytest.MonkeyPatch,
    status: int = 200,
    text: str = '{"data": {"link": "https://pay.example.com/abc", "n": 3}}',
    bundle: Optional[Dict[str, Any]] = None,
    raises: Optional[BaseException] = None,
) -> List[Dict[str, Any]]:
    """Stub the seam: the guarded request (no network) and the vault read."""
    sent: List[Dict[str, Any]] = []

    @asynccontextmanager
    async def fake_request(session, method, url, **kwargs):
        sent.append({"method": method, "url": url, **kwargs})
        if raises is not None:
            raise raises
        yield _Response(status, text)

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
        external_account_id="https://api.example.com/v1",
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
    assert call["headers"]["Idempotency-Key"] == "run-1:get-link"
    # the guard is told which host is allowed: a redirect off it loses the auth
    assert call["allowed_host_suffixes"] == ["api.example.com"]


def test_a_door_without_a_credential_sends_no_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(monkeypatch)
    asyncio.run(Request().perform("m1", _door(credential=None), _ARGS, _CTX))
    assert "Authorization" not in sent[0]["headers"]


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
