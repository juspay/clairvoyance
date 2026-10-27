"""Xyne as a connector: connecting an app (``xyne:<appId>``).

The request model, reading the app id from the token, and the one provider
call that proves the token, through doubles.stub_http on the shared Xyne
client. No database and no network; what onboarding.py does with the result
(the vault, the 409, the lock) is pinned in test_connectors_onboarding.
"""

import base64
import json
from typing import Any, Dict

import httpx
import pytest
from pydantic import ValidationError

from app.crm.connectivity.connectors import CONNECTORS
from app.crm.connectivity.providers.base import ConnectorHandshakeError
from app.crm.connectivity.providers.xyne import client as xyne_client, onboard
from app.crm.connectivity.providers.xyne.onboard import (
    OnboardXyneRequest,
    XyneOnboarder,
    token_app_id,
)
from tests.crm.doubles import stub_http


def _jwt(claims: Dict[str, Any]) -> str:
    """An unsigned-looking JWT carrying these claims — the shape of a Xyne
    API token, whose payload onboarding reads (never its signature)."""

    def segment(value: Dict[str, Any]) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'HS256'})}.{segment(claims)}.c2ln"


APP_ID = "app_hire01"
TOKEN = _jwt({"appId": APP_ID, "userId": "u-1"})
WEBHOOK_SECRET = "a-webhook-secret-of-length"
BASE = "https://spaces.xyne.juspay.net"
#: The token check: a read behind Xyne's app-token authenticator.
CHECK_URL = f"{BASE}/api/apps/channel/list?limit=1&joined=true"


def _request(**overrides: Any) -> OnboardXyneRequest:
    fields: Dict[str, Any] = {
        "merchant_id": "pragathi",
        "api_token": TOKEN,
        "webhook_secret": WEBHOOK_SECRET,
        "base_url": BASE,
    }
    fields.update(overrides)
    return OnboardXyneRequest(**fields)


def _accepts(monkeypatch):
    """Xyne accepting the token; the requests made are returned."""
    return stub_http(monkeypatch, xyne_client, lambda r: httpx.Response(200, json=[]))


# --- the registry ------------------------------------------------------------


def test_xyne_is_a_registered_door_with_no_pipe() -> None:
    spec = CONNECTORS["xyne"]
    assert spec.source == "xyne"
    assert spec.channel is None
    assert spec.templates is None
    assert spec.request_model is OnboardXyneRequest
    assert spec.actions == {}


def test_xyne_accounts_are_exclusive_to_one_merchant() -> None:
    assert CONNECTORS["xyne"].exclusive_account is True


# --- the onboarding request --------------------------------------------------


def test_the_base_url_is_required_there_is_no_default() -> None:
    fields = _request().model_dump()
    del fields["base_url"]
    with pytest.raises(ValidationError, match="base_url"):
        OnboardXyneRequest(**fields)


@pytest.mark.parametrize(
    "base_url,stored",
    [
        ("https://spaces.xyne.juspay.net/", BASE),
        ("https://other.xyne.juspay.net", "https://other.xyne.juspay.net"),
    ],
)
def test_a_xyne_host_is_accepted(base_url, stored) -> None:
    assert _request(base_url=base_url).base_url == stored


@pytest.mark.parametrize(
    "base_url",
    [
        "http://spaces.xyne.juspay.net",  # not https
        "https://evil.example.com",  # not a Xyne host
        "https://xyne.juspay.net.evil.com",  # suffix trick
        "https://user:pw@spaces.xyne.juspay.net",  # credentials in the URL
        "https://spaces.xyne.juspay.net/api",  # a path
        "https://spaces.xyne.juspay.net#",  # an empty fragment
        "https://spaces.xyne.juspay.net?",  # an empty query
        "https://spaces.xyne.juspay.net/#",  # empty fragment after the slash
        "https://spaces.xyne.juspay.net?x=1",  # a query
        "https://169.254.169.254",  # cloud metadata address
    ],
)
def test_any_other_base_url_is_refused(base_url) -> None:
    with pytest.raises(ValidationError):
        _request(base_url=base_url)


def test_a_short_webhook_secret_is_refused() -> None:
    with pytest.raises(ValidationError):
        _request(webhook_secret="short")


def test_the_app_id_is_not_a_field_the_caller_can_type() -> None:
    """The app id comes from the token alone, so it cannot disagree with it:
    a typed one is not even read."""
    assert "app_id" not in OnboardXyneRequest.model_fields
    typed = OnboardXyneRequest(**{**_request().model_dump(), "app_id": "other"})
    assert XyneOnboarder().identify(typed) == (APP_ID, None)


def test_secrets_never_reach_a_repr() -> None:
    text = repr(_request())
    assert TOKEN not in text and WEBHOOK_SECRET not in text


# --- the app id, read from the token -----------------------------------------


def test_the_app_id_claim_is_read_from_the_token() -> None:
    assert token_app_id(TOKEN) == APP_ID


@pytest.mark.parametrize(
    "token",
    [
        _jwt({"userId": "u-1"}),  # no appId claim
        _jwt({"appId": 123}),  # not a string
        _jwt({"appId": ""}),  # empty
        _jwt({"appId": "../etc"}),  # not a plain token (it rides in a URL path)
        _jwt({"appId": "a/b"}),
        _jwt({"appId": "x" * 129}),  # too long
        "xyne-api-token",  # not a JWT at all
        "x.y",  # two segments
        "a.%%%.c",  # payload not base64
        "a.WzFd.c",  # payload a JSON list, not claims
    ],
)
def test_a_token_naming_no_usable_app_reads_none(token) -> None:
    assert token_app_id(token) is None


def test_identify_knows_the_app_before_the_handshake() -> None:
    assert XyneOnboarder().identify(_request()) == (APP_ID, None)


# --- the handshake -----------------------------------------------------------


async def test_a_good_token_becomes_a_healthy_door_with_its_bundle(
    monkeypatch,
) -> None:
    seen = _accepts(monkeypatch)
    result = await XyneOnboarder().gather(_request())

    (call,) = seen
    assert call.method == "GET"
    assert str(call.url) == CHECK_URL
    assert call.headers["authorization"] == f"Bearer {TOKEN}"
    assert result.external_account_id == APP_ID
    assert result.address is None
    assert result.health_level == "healthy"
    assert result.display_label == f"Xyne app {APP_ID}"
    # Everything onboarding.py vaults, encrypted: secrets AND the base URL.
    assert result.bundle == {
        onboard.API_TOKEN: TOKEN,
        onboard.WEBHOOK_SECRET: WEBHOOK_SECRET,
        onboard.BASE_URL: BASE,
    }


async def test_the_merchants_label_is_kept(monkeypatch) -> None:
    _accepts(monkeypatch)
    result = await XyneOnboarder().gather(_request(display_label="Hiring bot"))
    assert result.display_label == "Hiring bot"


async def test_a_token_naming_no_app_is_refused_before_any_call(monkeypatch) -> None:
    seen = _accepts(monkeypatch)
    with pytest.raises(ConnectorHandshakeError, match="names no app"):
        await XyneOnboarder().gather(_request(api_token="xyne-api-token"))
    assert seen == []


@pytest.mark.parametrize(
    "code,body",
    [
        # What Xyne's authenticator answers for a bad or forged token.
        (401, {"error": "Unauthorized", "message": "Authentication failed"}),
        (403, {"error": "Forbidden"}),  # any 403 that is not a permission refusal
        (403, None),  # no readable body
    ],
)
async def test_a_refused_token_is_a_handshake_error(monkeypatch, code, body) -> None:
    stub_http(
        monkeypatch,
        xyne_client,
        lambda r: httpx.Response(code, json=body) if body else httpx.Response(code),
    )
    with pytest.raises(ConnectorHandshakeError, match="refused this API token"):
        await XyneOnboarder().gather(_request())


async def test_a_token_lacking_only_the_permission_is_genuine(monkeypatch) -> None:
    """Xyne checks permissions only after its authenticator verified the
    token, so 403 missing_permission proves the token is real: connected."""
    stub_http(
        monkeypatch,
        xyne_client,
        lambda r: httpx.Response(
            403,
            json={
                "error": "missing_permission",
                "required": "channels:read",
                "granted": ["chat:write"],
            },
        ),
    )
    result = await XyneOnboarder().gather(_request())
    assert result.external_account_id == APP_ID and result.health_level == "healthy"


@pytest.mark.parametrize("code", [400, 404])
async def test_any_other_refusal_is_a_handshake_error(monkeypatch, code) -> None:
    stub_http(monkeypatch, xyne_client, lambda r: httpx.Response(code, text="nope"))
    with pytest.raises(ConnectorHandshakeError, match="refused the token check"):
        await XyneOnboarder().gather(_request())


@pytest.mark.parametrize("code", [302, 429, 500, 503])
async def test_an_unreachable_xyne_writes_no_door(monkeypatch, code) -> None:
    stub_http(monkeypatch, xyne_client, lambda r: httpx.Response(code))
    with pytest.raises(ConnectorHandshakeError, match="could not be reached"):
        await XyneOnboarder().gather(_request())


async def test_a_network_failure_writes_no_door(monkeypatch) -> None:
    def _down(request):
        raise httpx.ConnectError("no route", request=request)

    stub_http(monkeypatch, xyne_client, _down)
    with pytest.raises(ConnectorHandshakeError, match="could not be reached"):
        await XyneOnboarder().gather(_request())


async def test_a_redirect_is_never_followed(monkeypatch) -> None:
    """A 30x must not carry the bearer token to wherever it points."""
    seen = stub_http(
        monkeypatch,
        xyne_client,
        lambda r: httpx.Response(302, headers={"location": "https://evil.example"}),
    )
    with pytest.raises(ConnectorHandshakeError):
        await XyneOnboarder().gather(_request())
    assert [str(r.url) for r in seen] == [CHECK_URL]


async def test_the_token_never_appears_in_a_refusal(monkeypatch) -> None:
    """Xyne's error text is passed to the merchant — but never our token."""
    stub_http(
        monkeypatch,
        xyne_client,
        lambda r: httpx.Response(400, text=f"bad token {TOKEN}"),
    )
    with pytest.raises(ConnectorHandshakeError) as raised:
        await XyneOnboarder().gather(_request())
    assert TOKEN not in str(raised.value)
