"""Connector onboarding: the registry, the handshake, and the two rows.

No database and no network. The accessors and the provider face are doubles,
so what is under test is the ORDER of the steps and what each one refuses —
which is where every defect in this path has actually lived.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest
from pydantic import ValidationError

from app.crm.connectivity import (
    accounts as accounts_module,
    onboarding as onboarding_module,
)
from app.crm.connectivity.channels import CHANNELS
from app.crm.connectivity.connectors import (
    CONNECTORS,
    ConnectorSpec,
    connector_for,
    connector_for_channel,
    sending_connectors,
)
from app.crm.connectivity.db import UniqueViolation
from app.crm.connectivity.onboarding import (
    OnboardingError,
    ResubscribeRefused,
    UnknownConnectorError,
    disconnect,
    onboard,
)
from app.crm.connectivity.providers import ADAPTERS
from app.crm.connectivity.providers.whatsapp.onboard import (
    OnboardWhatsappRequest,
    WhatsappOnboarder,
    WhatsappOnboardingError,
)
from app.crm.connectivity.schemas.connector import (
    ChannelBinding,
    ConnectorInstallation,
    InstallationRead,
    OnboardResult,
)
from tests.crm.doubles import (
    FakeInstallationAccessor,
    stub_graph,
)

# --- the registry -----------------------------------------------------------


def test_every_connector_serves_a_registered_channel() -> None:
    """CONNECTORS ⊆ CHANNELS.

    The pin every registry in this module carries. A connector whose channel
    is unknown to channels.py has no gate_handle_kind, and the suppression
    gate fails closed on it — so onboarding would happily build a door that
    can never send.
    """
    assert {s.channel for s in sending_connectors().values()} <= set(CHANNELS)


def test_every_connector_channel_has_an_adapter() -> None:
    """A connector a merchant can onboard must be one we can send on."""
    assert {s.channel for s in sending_connectors().values()} <= set(ADAPTERS)


def test_every_spec_knows_the_key_it_is_filed_under() -> None:
    """The spec carries its own key so one lookup answers both "which
    provider" and "what is it called" — an installation row is keyed by
    connector_key, and without this the caller scans the registry a second
    time to re-derive what it just found."""
    assert all(key == spec.key for key, spec in CONNECTORS.items())


def test_the_registry_is_the_vocabulary() -> None:
    """An unknown key resolves to nothing — the dict IS the list of
    connectors, so asking for one that is not in it is asking for something
    that does not exist."""
    assert connector_for("whatsapp") is not None
    assert connector_for("telegram") is None
    assert connector_for_channel("whatsapp") is not None


async def test_an_unknown_connector_is_refused_by_name() -> None:
    """Its own exception type, so the route can answer 404 rather than 400."""
    with pytest.raises(UnknownConnectorError):
        await onboard("shop", "telegram", {"merchant_id": "shop"})


# --- the WhatsApp handshake -------------------------------------------------


def _meta_stub(
    expires_in: Optional[int] = 5184000,
    subscribe_ok: bool = True,
    subscribe_silent: bool = False,
    phone_pages: int = 1,
):
    """Meta, answering the four onboarding calls the way Meta answers them."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Test double: canned Graph responses by path."""
        path = request.url.path
        if path.endswith("/oauth/access_token"):
            # Both exchanges POST their credentials in the BODY now, so the
            # long-lived one is identified from there — sniffing the URL is
            # exactly what stopped working when the app secret moved out of it.
            form = request.read().decode()
            body: Dict[str, Any] = {"access_token": "tok"}
            if "fb_exchange_token" in form and expires_in is not None:
                body["expires_in"] = expires_in
            return httpx.Response(200, json=body)
        if path.endswith("/phone_numbers"):
            # Meta pages this list. PN1 deliberately sits on the LAST page.
            cursor = int(request.url.params.get("after", "0") or 0)
            last = cursor >= phone_pages - 1
            body: Dict[str, Any] = {
                "data": [{"id": "PN1"}] if last else [{"id": f"OTHER{cursor}"}]
            }
            if not last:
                body["paging"] = {
                    "next": f"{request.url.scheme}://{request.url.host}"
                    f"{path}?after={cursor + 1}"
                }
            return httpx.Response(200, json=body)
        if path.endswith("/subscribed_apps"):
            if subscribe_silent:
                # Meta answering 200 with no `success` — the case that used
                # to escape the handler and abort the whole onboarding.
                return httpx.Response(200, json={})
            if subscribe_ok:
                return httpx.Response(200, json={"success": True})
            return httpx.Response(400, json={"error": {"code": 100, "message": "nope"}})
        return httpx.Response(404, json={})

    return handler


def _request(**overrides) -> OnboardWhatsappRequest:
    fields = dict(
        merchant_id="shop",
        code="signup-code",
        waba_id="waba-1",
        phone_number_id="PN1",
        display_label="Main line",
    )
    fields.update(overrides)
    return OnboardWhatsappRequest(**fields)


async def test_an_expiry_becomes_a_real_deadline(monkeypatch) -> None:
    """expires_in must survive the handshake.

    canon T11 calls token_expires_at the only thing between us and every Meta
    connector dying silently every sixty days: the refresh job watches
    non-NULL rows, so a discarded expiry is a door that claims to hold a
    permanent credential and goes dark on day sixty with a green light.
    """
    stub_graph(monkeypatch, _meta_stub(expires_in=5184000))
    result = await WhatsappOnboarder().gather(_request())
    assert result.token_expires_at is not None
    expected = datetime.now(timezone.utc) + timedelta(seconds=5184000)
    assert abs((result.token_expires_at - expected).total_seconds()) < 60


async def test_no_expiry_stays_an_honest_null(monkeypatch) -> None:
    """Some tokens genuinely never expire, and NULL has to keep meaning that
    rather than 'we forgot to look'."""
    stub_graph(monkeypatch, _meta_stub(expires_in=None))
    result = await WhatsappOnboarder().gather(_request())
    assert result.token_expires_at is None


async def test_the_token_never_rides_the_query_string(monkeypatch) -> None:
    """A bearer token in a URL reaches proxy logs and browser history."""
    seen = stub_graph(monkeypatch, _meta_stub())
    await WhatsappOnboarder().gather(_request())
    for request in seen:
        assert "access_token=" not in str(request.url)
    subscribes = [r for r in seen if r.url.path.endswith("/subscribed_apps")]
    assert subscribes and subscribes[0].headers["authorization"] == "Bearer tok"


async def test_a_number_not_on_the_account_is_refused(monkeypatch) -> None:
    """Both ids arrive from a page we do not control; binding a number that
    is not on the account would build a door onto someone else's endpoint."""
    stub_graph(monkeypatch, _meta_stub())
    with pytest.raises(WhatsappOnboardingError):
        await WhatsappOnboarder().gather(_request(phone_number_id="PN-OTHER"))


async def test_a_failed_subscription_reports_a_lower_rung(monkeypatch) -> None:
    """Not a refusal: the account is real and the token works. What is
    missing is the event stream, which is a DEGRADED door with a why — and
    refusing outright would leave the merchant with a spent signup code."""
    stub_graph(monkeypatch, _meta_stub(subscribe_ok=False))
    result = await WhatsappOnboarder().gather(_request())
    assert result.health_level == "authenticated"
    assert result.health_why


# --- onboarding's four steps ------------------------------------------------


class _FakeBindingAccessor:
    """Stands in for db/accessors/binding."""

    def __init__(self, existing=None, has_primary=False):
        """Test double."""
        self.existing = existing
        self.has_primary = has_primary
        self.upserts: List[tuple] = []

    async def peek_binding_by_address(self, merchant_id, channel, address):
        """Test double: the pre-check's glance at the same row."""
        return self.existing

    async def get_binding_by_address(self, txn, merchant_id, channel, address):
        """Test double: the seeded binding at that address."""
        return self.existing

    async def has_active_primary_binding(self, txn, merchant_id, channel):
        """Test double: whether a default route already exists."""
        return self.has_primary

    async def upsert_binding(self, txn, *args):
        """Test double: record the pipe that would be written."""
        self.upserts.append(args)
        return _binding()


def _installation_read(**overrides) -> InstallationRead:
    fields = dict(
        id="i-1",
        merchant_id="shop",
        connector_key="whatsapp",
        external_account_id="waba-1",
        display_label="Main line",
        status="healthy",
        installed_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return InstallationRead(**fields)


def _healthy_route(status: str = "healthy") -> ConnectorInstallation:
    """The route-shaped installation the pre-check reads (not the console
    shape) — status is the only field it looks at."""
    return ConnectorInstallation(
        id="i-1",
        merchant_id="shop",
        connector_key="whatsapp",
        external_account_id="waba-1",
        credential_id="cred-1",
        status=status,
    )


def _binding(**overrides) -> ChannelBinding:
    fields = dict(
        id="b-1",
        merchant_id="shop",
        channel="whatsapp",
        installation_id="i-1",
        address="PN1",
        is_primary=True,
        status="active",
    )
    fields.update(overrides)
    return ChannelBinding(**fields)


class _StubOnboarder:
    """A ConnectorOnboarder that reports a scripted result."""

    def __init__(self, result: OnboardResult):
        """Test double."""
        self.result = result
        self.gathered = 0
        self.revoked: List[str] = []

    def identify(self, request):
        """Test double: the ids the request already carries."""
        return getattr(request, "waba_id", None), getattr(
            request, "phone_number_id", None
        )

    async def gather(self, request):
        """Test double: the scripted handshake outcome."""
        self.gathered += 1
        return self.result

    async def resubscribe(self, bundle, external_account_id):
        """Test double: the port verb exists; these tests never call it."""

    async def revoke(self, bundle, external_account_id):
        """Test double: record that the provider was told."""
        self.revoked.append(external_account_id)


def _result(**overrides) -> OnboardResult:
    fields: Dict[str, Any] = dict(
        external_account_id="waba-1",
        address="PN1",
        display_label="Main line",
        bundle={"system_user_token": "tok"},
        health_level="subscribed",
        health_why="no receiver yet",
    )
    fields.update(overrides)
    return OnboardResult(**fields)


class _Merchant:
    reseller_id = "reseller-1"


class _Credential:
    id = "cred-1"
    is_active = True
    value = {"system_user_token": "tok"}


def _patch_onboarding(
    monkeypatch,
    *,
    result=None,
    installations=None,
    bindings=None,
    onboarder=None,
    channel="whatsapp",
):
    """Wire onboarding.py to doubles: merchant, vault, accessors, atom.

    The ONE place a ConnectorSpec is built. A test that needs a different
    onboarder passes one in rather than assembling its own spec — otherwise
    every field the registry grows has to be added to every test body, and
    the type checker is the only thing that notices when it isn't.
    """
    onboarder = onboarder or _StubOnboarder(result or _result())
    spec = ConnectorSpec(
        key="whatsapp",
        source="whatsapp",
        channel=channel,
        onboarder=onboarder,
        templates=CONNECTORS["whatsapp"].templates,
        request_model=OnboardWhatsappRequest,
    )
    monkeypatch.setattr(onboarding_module, "connector_for", lambda key: spec)

    async def _merchants(ids):
        """Test double: one known merchant."""
        return [_Merchant()], None

    async def _by_name(reseller_id, name, mask=True):
        """Test double: no existing credential, so the create path runs."""
        return None

    async def _create(reseller_id, name, kind, value, description=None):
        """Test double: the vault write."""
        return _Credential()

    async def _atomically(fn, *args):
        """Test double: run the atom body against a None handle."""
        return await fn(None, *args)

    monkeypatch.setattr(onboarding_module, "get_merchants_by_ids", _merchants)
    monkeypatch.setattr(onboarding_module, "get_credential_by_name", _by_name)
    monkeypatch.setattr(onboarding_module, "create_credential", _create)
    monkeypatch.setattr(onboarding_module, "atomically", _atomically)
    monkeypatch.setattr(
        onboarding_module,
        "installation_accessor",
        installations or FakeInstallationAccessor(upsert_returns=_installation_read()),
    )
    monkeypatch.setattr(
        onboarding_module, "binding_accessor", bindings or _FakeBindingAccessor()
    )
    return onboarder


async def test_the_merchant_is_checked_before_the_code_is_spent(monkeypatch) -> None:
    """An Embedded Signup code is single-use.

    Spending it on a merchant_id that does not exist means the merchant has
    to redo the whole signup, and the provider is left subscribed to an
    account we then refused.
    """
    onboarder = _patch_onboarding(monkeypatch)

    async def _no_merchants(ids):
        """Test double: the merchant does not exist."""
        return [], None

    monkeypatch.setattr(onboarding_module, "get_merchants_by_ids", _no_merchants)
    with pytest.raises(OnboardingError):
        await onboard(
            "ghost",
            "whatsapp",
            {
                "merchant_id": "ghost",
                "code": "c",
                "waba_id": "w",
                "phone_number_id": "p",
            },
        )
    # The handshake never ran, so the code is still usable.
    assert onboarder.gathered == 0


@pytest.mark.parametrize(
    "level,expected",
    [
        ("subscribed", "healthy"),
        ("authenticated", "degraded"),
        ("configured", "connecting"),
    ],
)
async def test_the_status_is_derived_from_the_health_rung(
    monkeypatch, level, expected
) -> None:
    """The light must agree with the sentence under it.

    canon T11: status is the traffic light, health_detail the reason, and a
    rung below healthy carries a why AND a non-green light. Writing 'healthy'
    on a door whose subscription failed means receipts and inbound STOPs
    never arrive while the screen says everything is fine.
    """
    installations = FakeInstallationAccessor(upsert_returns=_installation_read())
    _patch_onboarding(
        monkeypatch,
        result=_result(health_level=level, health_why="because"),
        installations=installations,
    )
    await onboard("shop", "whatsapp", _request().model_dump())
    assert installations.written is not None
    assert installations.written["status"] == expected


async def test_the_expiry_reaches_the_row(monkeypatch) -> None:
    """What the handshake learned about the token's life has to be written,
    or the refresh job never sees this door."""
    expires = datetime.now(timezone.utc) + timedelta(days=60)
    installations = FakeInstallationAccessor(upsert_returns=_installation_read())
    _patch_onboarding(
        monkeypatch,
        result=_result(token_expires_at=expires),
        installations=installations,
    )
    await onboard("shop", "whatsapp", _request().model_dump())
    assert installations.written is not None
    assert installations.written["token_expires_at"] == expires


async def test_a_disabled_connection_is_not_resurrected(monkeypatch) -> None:
    """'disabled' is an ops decision, and pressing connect again must not
    undo it. The upsert's WHERE declines and returns nothing; the refusal
    names what to do about it."""
    _patch_onboarding(
        monkeypatch, installations=FakeInstallationAccessor(upsert_returns=None)
    )
    with pytest.raises(OnboardingError, match="disabled"):
        await onboard("shop", "whatsapp", _request().model_dump())


async def test_a_retired_endpoint_is_refused(monkeypatch) -> None:
    """canon T12: a retired pipe surrendered its address, and the provider
    may have recycled that number to somebody else. Resurrecting it would
    point this merchant's sends at a stranger's endpoint."""
    _patch_onboarding(
        monkeypatch,
        bindings=_FakeBindingAccessor(existing=_binding(status="retired")),
    )
    with pytest.raises(OnboardingError, match="retired"):
        await onboard("shop", "whatsapp", _request().model_dump())


async def test_a_second_number_does_not_steal_the_default_route(monkeypatch) -> None:
    """Connecting another number is not a decision to make it the default."""
    bindings = _FakeBindingAccessor(has_primary=True)
    _patch_onboarding(monkeypatch, bindings=bindings)
    await onboard("shop", "whatsapp", _request().model_dump())
    assert bindings.upserts[0][-1] is False


async def test_the_first_number_becomes_the_default_route(monkeypatch) -> None:
    """A merchant with one pipe has a default whether they chose one or not."""
    bindings = _FakeBindingAccessor(has_primary=False)
    _patch_onboarding(monkeypatch, bindings=bindings)
    await onboard("shop", "whatsapp", _request().model_dump())
    assert bindings.upserts[0][-1] is True


# --- disconnect -------------------------------------------------------------


class _DisconnectAccessor:
    """Stands in for db/accessors/installation during a disconnect."""

    def __init__(self, installation=None, revoked=None):
        """Test double."""
        self.installation = installation
        self.revoked = revoked
        self.calls: List[str] = []

    async def get_installation(self, merchant_id, installation_id):
        """Test double: the seeded route-shaped installation."""
        return self.installation

    async def revoke_installation(self, txn, merchant_id, installation_id):
        """Test double: record the revoke."""
        self.calls.append("revoke")
        return self.revoked


class _PauseAccessor:
    """Stands in for db/accessors/binding during a disconnect."""

    def __init__(self):
        """Test double."""
        self.paused = False

    async def pause_bindings_for_installation(self, txn, merchant_id, installation_id):
        """Test double: record that the pipes were paused."""
        self.paused = True
        return [_binding(status="paused", is_primary=False)]


async def test_disconnect_tells_the_provider_before_it_touches_the_rows(
    monkeypatch,
) -> None:
    """A merchant who left must stop generating events.

    Without the unsubscribe the provider keeps delivering webhooks for a
    revoked door, and every one of them is attributed to a connection that
    no longer wants them.
    """
    onboarder = _patch_onboarding(monkeypatch)
    installations = _DisconnectAccessor(
        installation=ConnectorInstallation(
            id="i-1",
            merchant_id="shop",
            connector_key="whatsapp",
            external_account_id="waba-1",
            credential_id="cred-1",
            status="healthy",
        ),
        revoked=_installation_read(status="revoked"),
    )
    bindings = _PauseAccessor()
    monkeypatch.setattr(onboarding_module, "installation_accessor", installations)
    monkeypatch.setattr(onboarding_module, "binding_accessor", bindings)

    async def _credential(credential_id, mask=True, raise_errors=False):
        """Test double: the vault read behind the revoke."""
        return _Credential()

    monkeypatch.setattr(accounts_module, "get_credential_by_id", _credential)

    result = await disconnect("shop", "i-1")
    assert result is not None and result.status == "revoked"
    assert onboarder.revoked == ["waba-1"]
    assert bindings.paused is True


async def test_disconnecting_someone_elses_connection_is_a_flat_no(
    monkeypatch,
) -> None:
    """Unknown id and another tenant's id are one answer — the second must
    not be distinguishable, or the endpoint enumerates installations."""
    _patch_onboarding(monkeypatch)
    monkeypatch.setattr(
        onboarding_module, "installation_accessor", _DisconnectAccessor()
    )
    assert await disconnect("shop", "i-999") is None


# --- what the handshake may and may not tell the caller ---------------------


async def test_a_providers_own_refusal_reaches_the_merchant(monkeypatch) -> None:
    """Its message is written FOR them — "that number is not on this
    account" is the whole value of the 400."""

    class _Refuses(_StubOnboarder):
        async def gather(self, request):
            """Test double: the provider's declared refusal."""
            raise WhatsappOnboardingError(
                "phone number PN9 is not on this WhatsApp Business Account"
            )

    _patch_onboarding(monkeypatch, onboarder=_Refuses(_result()))
    with pytest.raises(OnboardingError, match="not on this WhatsApp Business Account"):
        await onboard("shop", "whatsapp", _request().model_dump())


async def test_an_unexpected_exception_does_not_reach_the_caller(
    monkeypatch,
) -> None:
    """A bug's text is an internal detail. It is logged in full and answered
    with one fixed sentence — a driver message in an API response tells the
    caller nothing they can act on and everyone else about our internals."""

    class _Explodes(_StubOnboarder):
        async def gather(self, request):
            """Test double: a bug, not a refusal."""
            raise KeyError("internal_pool_handle_7f3a")

    _patch_onboarding(monkeypatch, onboarder=_Explodes(_result()))
    with pytest.raises(OnboardingError) as caught:
        await onboard("shop", "whatsapp", _request().model_dump())
    assert "internal_pool_handle_7f3a" not in str(caught.value)
    assert str(caught.value) == "could not complete the connector handshake"


async def test_racing_the_default_route_is_answered_not_a_500(monkeypatch) -> None:
    """The partial unique index is what actually keeps a merchant to one
    default route, and it holds. What was wrong was the ANSWER: an unhandled
    violation is a 500 for a caller who did nothing wrong."""
    _patch_onboarding(monkeypatch)

    async def _races(fn, *args):
        """Test double: the index refuses the second primary."""
        raise UniqueViolation("crm_channel_binding_primary_uq")

    monkeypatch.setattr(onboarding_module, "atomically", _races)
    with pytest.raises(OnboardingError, match="try again"):
        await onboard("shop", "whatsapp", _request().model_dump())


# --- canon T11: a light that is not green carries its reason ---------------


def test_a_degraded_result_without_a_reason_is_refused() -> None:
    """Structural, not a comment. A door that comes back amber with nothing
    in `why` gives the connections screen a colour and no reason."""
    with pytest.raises(ValidationError):
        OnboardResult(
            external_account_id="waba-1",
            address="PN1",
            health_level="authenticated",
            health_why="   ",
        )


def test_a_healthy_result_needs_no_reason() -> None:
    OnboardResult(external_account_id="waba-1", address="PN1", health_level="healthy")


async def test_an_unconfirmed_subscription_degrades_rather_than_aborting(
    monkeypatch,
) -> None:
    """Meta answering 200 without `success` used to raise out of gather() and
    abort onboarding — leaving the merchant with a spent one-time code and no
    connection. The account is real and its token works; only the event
    stream is missing, which is a degraded door with a why."""
    stub_graph(monkeypatch, _meta_stub(subscribe_silent=True))
    result = await WhatsappOnboarder().gather(_request())
    assert result.health_level == "authenticated"
    assert result.health_why


async def test_a_number_on_a_later_page_is_still_found(monkeypatch) -> None:
    """Meta returns 25 numbers a page. Without following the cursor, a
    business with more than one page has a legitimate number refused as "not
    on this account" — a wrong answer, not a slow one."""
    stub_graph(monkeypatch, _meta_stub(phone_pages=3))
    result = await WhatsappOnboarder().gather(_request())
    assert result.external_account_id == "waba-1"


# --- no secret rides a URL --------------------------------------------------


async def test_the_app_secret_never_rides_the_query_string(monkeypatch) -> None:
    """The twin of the bearer-token test, for the worse leak.

    A system-user token is one merchant's account for ~60 days; the app
    secret is every merchant's, and it is what verifies inbound webhooks.
    Both OAuth exchanges POST it in the body — a query string reaches proxy
    access logs, browser history and every intermediary's request log.
    """
    seen = stub_graph(monkeypatch, _meta_stub())
    await WhatsappOnboarder().gather(_request())
    for request in seen:
        assert "client_secret=" not in str(request.url)
        assert "client_id=" not in str(request.url)
    exchanges = [r for r in seen if r.url.path.endswith("/oauth/access_token")]
    assert exchanges and all(r.method == "POST" for r in exchanges)
    assert b"client_secret" in exchanges[0].read()


# --- the refusals that must land before the one-shot code is spent ----------


async def test_a_disabled_connection_refuses_before_the_code_is_spent(
    monkeypatch,
) -> None:
    """Discovering this inside the atom means the merchant has already burned
    a signup code and Meta is already subscribed to an account we then
    refuse. The atom keeps its check — that one is race-safe — but this is
    the cheap answer."""
    onboarder = _patch_onboarding(
        monkeypatch,
        installations=FakeInstallationAccessor(
            _installation_read(), existing_account=_healthy_route("disabled")
        ),
    )
    with pytest.raises(OnboardingError, match="disabled"):
        await onboard("shop", "whatsapp", _request().model_dump())
    assert onboarder.gathered == 0


async def test_a_retired_endpoint_refuses_before_the_code_is_spent(
    monkeypatch,
) -> None:
    onboarder = _patch_onboarding(
        monkeypatch, bindings=_FakeBindingAccessor(existing=_binding(status="retired"))
    )
    with pytest.raises(OnboardingError, match="retired"):
        await onboard("shop", "whatsapp", _request().model_dump())
    assert onboarder.gathered == 0


# --- a connector that does not send ----------------------------------------


async def test_a_door_with_no_channel_onboards_without_a_binding(
    monkeypatch,
) -> None:
    """Canon T11's vocabulary includes shopify, zendesk and juspay — doors
    with no pipe. A Shopify OAuth install is a COMPLETE onboarding with
    nothing to bind, so the atom must not try."""
    bindings = _FakeBindingAccessor()
    _patch_onboarding(monkeypatch, bindings=bindings, channel=None)
    installation = await onboard("shop", "shopify", _request().model_dump())
    assert installation is not None
    assert bindings.upserts == [], "a door with no channel writes no pipe"


async def test_a_door_with_no_channel_needs_no_address_at_all(monkeypatch) -> None:
    """The shape follows the same rule the atom does.

    A Shopify install has no endpoint to report, and before this it had to
    invent one to satisfy a required field that the channel-less path then
    never read. An invented value that nothing reads is the kind of thing a
    later reader trusts.
    """
    bindings = _FakeBindingAccessor()
    _patch_onboarding(
        monkeypatch, result=_result(address=None), bindings=bindings, channel=None
    )
    installation = await onboard("shop", "shopify", _request().model_dump())
    assert installation is not None
    assert bindings.upserts == []


async def test_a_channel_connector_returning_no_endpoint_is_refused(
    monkeypatch,
) -> None:
    """The mirror of the test above: optional in the SHAPE, still required
    for a connector that carries a channel.

    Binding an empty address would write a row that looks like a live route
    and matches nothing, so every send would resolve to it and fail — a dead
    pipe behind a green light, which is the failure onboarding exists to
    prevent.
    """
    bindings = _FakeBindingAccessor()
    _patch_onboarding(
        monkeypatch, result=_result(address=None), bindings=bindings, channel="whatsapp"
    )
    with pytest.raises(OnboardingError, match="no endpoint to bind"):
        await onboard("shop", "whatsapp", _request().model_dump())
    assert bindings.upserts == [], "nothing is bound when the endpoint is missing"


def test_the_send_side_pins_only_cover_connectors_that_send() -> None:
    """A data connector has no adapter and no CHANNELS entry; asserting over
    the whole registry would make adding one fail two tests about sending."""
    assert all(s.channel is not None for s in sending_connectors().values())


# --- the route itself -------------------------------------------------------
#
# The logic above is covered; these three pin what the DOOR does, which no
# other test touches: who is turned away, and how early.


def _client(monkeypatch, user_merchants=("shop",), role="user"):
    """A TestClient with the auth dependency overridden — everything else on
    the route (the tenancy check, the registry lookup, validation) runs for
    real."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
    from app.crm.connectivity import api as connectivity_api

    app = FastAPI()
    app.include_router(connectivity_api.router, prefix="/connectors")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role=role, merchant_ids=list(user_merchants), username="tester"
    )
    return TestClient(app)


def test_route_refuses_a_foreign_merchant_before_it_looks_up_the_connector(
    monkeypatch,
) -> None:
    """403 lands before anything is read or resolved — including the registry.
    A caller outside the tenancy must not even learn which connectors exist."""
    looked_up = []
    monkeypatch.setattr(
        onboarding_module,
        "connector_for",
        lambda key: looked_up.append(key),  # noqa: ARG005
    )
    res = _client(monkeypatch).post(
        "/connectors/whatsapp/onboard",
        json={
            "merchant_id": "rival",
            "code": "c",
            "waba_id": "w",
            "phone_number_id": "p",
        },
    )
    assert res.status_code == 403
    assert looked_up == [], "the registry was consulted before the tenancy check"


def test_route_answers_404_for_a_connector_that_does_not_exist(monkeypatch) -> None:
    """The CONNECTORS dict IS the vocabulary, so an unknown key is 'no such
    thing', not 'bad request'."""
    res = _client(monkeypatch).post(
        "/connectors/telegram/onboard",
        json={
            "merchant_id": "shop",
            "code": "c",
            "waba_id": "w",
            "phone_number_id": "p",
        },
    )
    assert res.status_code == 404


def test_route_answers_422_for_a_body_the_connector_cannot_validate(
    monkeypatch,
) -> None:
    """The body is untyped at the door and typed by the registry one line
    later — a missing field is that model's 422, and the detail must not echo
    the one-shot code back."""
    res = _client(monkeypatch).post(
        "/connectors/whatsapp/onboard",
        json={
            "merchant_id": "shop",
            "code": "super-secret-code",
            "phone_number_id": "p",
        },
    )
    assert res.status_code == 422
    assert "waba_id" in res.text
    assert "super-secret-code" not in res.text


# --- resubscribe: the recovery door (disconnect's opposite verb) -------------


def _patch_resubscribe(
    monkeypatch,
    *,
    installation="default",
    bundle="default",
    onboarder=None,
    spec="default",
):
    """Wire onboarding.resubscribe to doubles; returns the recorders."""
    provider_calls: List[tuple] = []
    health_stamps: List[dict] = []

    class _ResubOnboarder:
        """Test double: records the provider call instead of making it."""

        def identify(self, request):
            """Test double: unused here."""
            return (None, None)

        async def gather(self, request):
            """Test double: unused here."""
            raise NotImplementedError

        async def revoke(self, bundle, external_account_id):
            """Test double: unused here."""

        async def resubscribe(self, bundle, external_account_id):
            """Test double."""
            provider_calls.append((bundle, external_account_id))

    row = (
        _healthy_route(status="degraded") if installation == "default" else installation
    )

    async def _get_installation(merchant_id, installation_id):
        """Test double: the seeded installation, tenancy applied upstream."""
        return row

    monkeypatch.setattr(
        onboarding_module.installation_accessor,
        "get_installation",
        _get_installation,
    )

    async def _bundle_for(installation):
        """Test double: the vault read."""
        if bundle == "default":
            from app.crm.connectivity.schemas.message import CredentialBundle

            return CredentialBundle(values={"system_user_token": "tok"})
        if isinstance(bundle, Exception):
            raise bundle
        return bundle

    monkeypatch.setattr(onboarding_module.accounts, "bundle_for", _bundle_for)

    if spec == "default":
        built = ConnectorSpec(
            key="whatsapp",
            source="whatsapp",
            channel="whatsapp",
            onboarder=onboarder or _ResubOnboarder(),
            templates=CONNECTORS["whatsapp"].templates,
            request_model=OnboardWhatsappRequest,
        )
    else:
        built = spec
    monkeypatch.setattr(onboarding_module, "connector_for", lambda key: built)

    async def _atomically(fn, *args):
        """Test double: run the atom body with a dummy handle."""
        return await fn(None, *args)

    monkeypatch.setattr(onboarding_module, "atomically", _atomically)

    async def _update_health(
        txn, merchant_id, installation_id, *, status, health_detail
    ):
        """Test double: record the re-stamp and answer like the accessor."""
        health_stamps.append(
            {
                "merchant_id": merchant_id,
                "installation_id": installation_id,
                "status": status,
                "health_detail": health_detail,
            }
        )
        return _installation_read(status=status)

    monkeypatch.setattr(
        onboarding_module.installation_accessor,
        "update_installation_health",
        _update_health,
    )
    return provider_calls, health_stamps


async def test_resubscribe_calls_the_provider_with_the_stored_bundle(
    monkeypatch,
) -> None:
    """Resubscribe calls the provider with the stored bundle."""
    provider_calls, _ = _patch_resubscribe(monkeypatch)
    result = await onboarding_module.resubscribe("m-1", "i-1")
    assert result is not None
    assert len(provider_calls) == 1
    bundle, account_id = provider_calls[0]
    assert bundle.secret("system_user_token") == "tok"
    assert account_id == _healthy_route().external_account_id


async def test_a_successful_resubscribe_restamps_health(monkeypatch) -> None:
    # Finding 5: a recovery that leaves the row 'degraded' keeps every send
    # refusing, and a why that is now false contradicts canon T11.
    """A successful resubscribe restamps health."""
    import json as _json

    _, health_stamps = _patch_resubscribe(monkeypatch)
    result = await onboarding_module.resubscribe("m-1", "i-1")
    assert result is not None and result.status == "healthy"
    assert len(health_stamps) == 1
    stamp = health_stamps[0]
    assert stamp["status"] == "healthy"
    detail = _json.loads(stamp["health_detail"])
    assert detail["level"] == "subscribed" and detail["why"] is None
    assert detail["checked_at"]


async def test_a_foreign_tenants_installation_is_simply_not_found(
    monkeypatch,
) -> None:
    """A foreign tenant's installation is simply not found."""
    _patch_resubscribe(monkeypatch, installation=None)

    async def _none(merchant_id, installation_id):
        """Test double: not this merchant's row."""
        return None

    monkeypatch.setattr(
        onboarding_module.installation_accessor, "get_installation", _none
    )
    assert await onboarding_module.resubscribe("m-1", "i-1") is None


async def test_a_connector_without_the_verb_is_refused_before_the_vault(
    monkeypatch,
) -> None:
    """A connector without the verb is refused before the vault."""
    vault_reads: List[str] = []
    _patch_resubscribe(monkeypatch, spec=None)
    monkeypatch.setattr(onboarding_module, "connector_for", lambda key: None)

    async def _recording_bundle(installation):
        """Test double: must never be reached."""
        vault_reads.append(installation.id)

    monkeypatch.setattr(onboarding_module.accounts, "bundle_for", _recording_bundle)
    with pytest.raises(OnboardingError) as caught:
        await onboarding_module.resubscribe("m-1", "i-1")
    assert "no webhook subscription" in str(caught.value)
    assert vault_reads == []


async def test_unusable_credentials_are_a_refusal_not_a_crash(monkeypatch) -> None:
    """Unusable credentials are a refusal, not a crash."""
    _patch_resubscribe(monkeypatch, bundle=accounts_module.AccountError("gone"))
    with pytest.raises(OnboardingError) as caught:
        await onboarding_module.resubscribe("m-1", "i-1")
    assert "credentials" in str(caught.value)


async def test_a_vault_outage_is_not_reported_as_a_refusal(monkeypatch) -> None:
    # An outage must surface as an incident (the route's 500), never as
    # "reconnect your account" advice for an account whose credentials are
    # fine.
    """A vault outage is not reported as a refusal."""
    _patch_resubscribe(monkeypatch, bundle=ConnectionError("pool down"))
    with pytest.raises(ConnectionError):
        await onboarding_module.resubscribe("m-1", "i-1")


async def test_a_provider_refusal_passes_through_in_its_own_words(
    monkeypatch,
) -> None:
    """A provider refusal passes through in its own words."""

    class _Refusing:
        """Test double: the provider said no, in its own words."""

        def identify(self, request):
            """Test double: unused here."""
            return (None, None)

        async def gather(self, request):
            """Test double: unused here."""
            raise NotImplementedError

        async def revoke(self, bundle, external_account_id):
            """Test double: unused here."""

        async def resubscribe(self, bundle, external_account_id):
            """Test double."""
            raise WhatsappOnboardingError("that number is not on this account")

    _, health_stamps = _patch_resubscribe(monkeypatch, onboarder=_Refusing())
    with pytest.raises(OnboardingError) as caught:
        await onboarding_module.resubscribe("m-1", "i-1")
    assert "that number is not on this account" in str(caught.value)
    # And no re-stamp: a refused recovery must not turn the light green.
    assert health_stamps == []


async def test_the_whatsapp_face_translates_graph_errors(monkeypatch) -> None:
    # Rule 11: GraphError never leaves the package; its detail does.
    """The whatsapp face translates graph errors."""
    from app.crm.connectivity.providers.meta.graph import GraphError
    from app.crm.connectivity.providers.whatsapp import onboard as onboard_module
    from app.crm.connectivity.schemas.message import CredentialBundle

    async def _graph_refuses(waba_id, token):
        """Test double: Meta refused at the wire."""
        raise GraphError("(#200) permissions error", code="200")

    monkeypatch.setattr(onboard_module, "subscribe", _graph_refuses)
    with pytest.raises(WhatsappOnboardingError) as caught:
        await WhatsappOnboarder().resubscribe(
            CredentialBundle(values={"system_user_token": "tok"}), "waba-1"
        )
    assert "(#200) permissions error" in str(caught.value)
    assert isinstance(caught.value.__cause__, GraphError)


async def test_the_whatsapp_face_refuses_a_bundle_without_a_token(
    monkeypatch,
) -> None:
    """The whatsapp face refuses a bundle without a token."""
    from app.crm.connectivity.providers.whatsapp import onboard as onboard_module
    from app.crm.connectivity.schemas.message import CredentialBundle

    calls: List[tuple] = []

    async def _record(waba_id, token):
        """Test double: must never be reached."""
        calls.append((waba_id, token))

    monkeypatch.setattr(onboard_module, "subscribe", _record)
    with pytest.raises(WhatsappOnboardingError) as caught:
        await WhatsappOnboarder().resubscribe(CredentialBundle(values={}), "waba-1")
    assert "access token" in str(caught.value)
    assert calls == []


# --- the subscribe route ------------------------------------------------------


def _client(monkeypatch, user_merchants=("shop",)):
    """A TestClient with auth overridden — tenancy and error mapping run for
    real."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
    from app.crm.connectivity import api as connectivity_api

    app = FastAPI()
    app.include_router(connectivity_api.router, prefix="/connectors")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="user", merchant_ids=list(user_merchants), username="tester"
    )
    return TestClient(app)


def test_the_subscribe_route_reports_a_refusal_as_409(monkeypatch) -> None:
    # Understood and deliberately declined — with a sentence the merchant
    # can act on, not a 400's "you asked wrong".
    """The subscribe route reports a refusal as 409."""
    from app.crm.connectivity import contracts

    async def refuses(merchant_id, installation_id):
        """Test double: the door refused — the declared type, so the one
        translator answers 409 (a plain OnboardingError is the caller's
        mistake, 400)."""
        raise ResubscribeRefused(
            "This account's credentials are missing or unreadable."
        )

    monkeypatch.setattr(contracts, "resubscribe", refuses)
    response = _client(monkeypatch).post(
        "/connectors/installations/i-1/subscribe", params={"merchant_id": "shop"}
    )
    assert response.status_code == 409
    assert "credentials" in response.json()["detail"]


def test_the_subscribe_route_hides_a_foreign_installation_as_404(
    monkeypatch,
) -> None:
    """The subscribe route hides a foreign installation as 404."""
    from app.crm.connectivity import contracts

    async def not_found(merchant_id, installation_id):
        """Test double: not this merchant's row."""
        return None

    monkeypatch.setattr(contracts, "resubscribe", not_found)
    response = _client(monkeypatch).post(
        "/connectors/installations/i-1/subscribe", params={"merchant_id": "shop"}
    )
    assert response.status_code == 404


def test_the_subscribe_route_echoes_what_was_subscribed(monkeypatch) -> None:
    """The subscribe route echoes what was subscribed."""
    from app.crm.connectivity import contracts

    async def succeeds(merchant_id, installation_id):
        """Test double: the door subscribed and re-stamped health."""
        return _installation_read(status="healthy")

    monkeypatch.setattr(contracts, "resubscribe", succeeds)
    response = _client(monkeypatch).post(
        "/connectors/installations/i-1/subscribe", params={"merchant_id": "shop"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["subscribed"] is True and body["installation_id"] == "i-1"


def test_the_subscribe_route_refuses_a_foreign_merchant_before_the_door(
    monkeypatch,
) -> None:
    """The subscribe route refuses a foreign merchant before the door."""
    from app.crm.connectivity import contracts

    reached: List[str] = []

    async def records(merchant_id, installation_id):
        """Test double: must never be reached."""
        reached.append(merchant_id)

    monkeypatch.setattr(contracts, "resubscribe", records)
    response = _client(monkeypatch, user_merchants=("someone-else",)).post(
        "/connectors/installations/i-1/subscribe", params={"merchant_id": "shop"}
    )
    assert response.status_code == 403
    assert reached == []
