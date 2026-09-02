"""The send door: the token, the registry, the route, and the timeout.

Every test here is about a refusal. That is deliberate — the accepted path is
one line, and everything that makes this seam worth having is what it declines
to do when something is missing.
"""

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.crm.connectivity import (
    accounts as accounts_module,
    send as send_module,
    template_reads as template_reads_module,
)
from app.crm.connectivity.db.queries.binding import (
    binding_by_id_query,
    primary_binding_query,
)
from app.crm.connectivity.providers import ADAPTERS, adapter_for
from app.crm.connectivity.providers.base import ChannelAdapter
from app.crm.connectivity.providers.whatsapp.adapter import MetaWhatsAppAdapter
from app.crm.connectivity.schemas.connector import ChannelBinding, ConnectorInstallation
from app.crm.connectivity.schemas.message import (
    CredentialBundle,
    QueuedMessage,
    SendOutcome,
    SendRoute,
    SendToken,
)
from app.crm.connectivity.schemas.template import ApprovedTemplate
from app.crm.connectivity.send import (
    REASON_GATE_REFUSED,
    REASON_INSTALLATION_UNHEALTHY,
    REASON_NO_ADAPTER,
    REASON_NO_BINDING,
    REASON_NO_CREDENTIAL,
    REASON_NO_INSTALLATION,
    REASON_SEND_ERROR,
    REASON_TIMEOUT,
    resolve_send_route,
    send,
    token_grants,
)
from app.crm.connectivity.status import BINDING_ACTIVE, TEMPLATE_APPROVED
from app.crm.shared.redact import mask_address, mask_digit_runs
from app.schemas import Credential, CredentialType
from scripts.check_crm_boundaries import TABLE_OWNERS

# One file: the door and the pipe are meaningless apart, so they are created
# together rather than leaving a numbered window with half a schema.
CONNECTOR_MIGRATION = Path(
    "app/database/migrations/060_create_crm_connector_tables.sql"
)


def _ddl(path: Path = CONNECTOR_MIGRATION) -> str:
    """The migration with comment prose stripped: a structural assertion that
    passes on the paragraph explaining an absence proves nothing."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def _table_ddl(table: str) -> str:
    """Just one table's slice of the shared migration.

    Without this, an assertion meant for the binding would pass on the
    installation's DDL sitting in the same file — which is the one thing
    merging two tables into one migration could quietly cost us.
    """
    body = _ddl()
    start = body.index(f"CREATE TABLE {table} (")
    nxt = body.find("CREATE TABLE ", start + 1)
    return body[start:] if nxt == -1 else body[start:nxt]


def _message(**overrides) -> QueuedMessage:
    """A queued message for tests; keyword overrides replace any field."""
    fields = dict(
        id="m-1",
        merchant_id="shop",
        customer_id="c-1",
        channel="whatsapp",
        sent_to_address="+919876543210",
        source_kind="transactional",
        purpose_key="order_update",
        template_id="order_update_v1",
        variables={"1": "Priya"},
        dedupe_key="evt-1",
        attempt=1,
        next_attempt_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return QueuedMessage(**fields)


def _token(message: QueuedMessage) -> SendToken:
    """A granted send token naming exactly this message."""
    return SendToken(
        message_id=message.id, purpose_key=message.purpose_key, granted=True
    )


def _binding(**overrides) -> ChannelBinding:
    """An active channel binding for tests; overrides replace any field."""
    fields = dict(
        id="b-1",
        merchant_id="shop",
        channel="whatsapp",
        installation_id="i-1",
        address="1234567890",
        capabilities={},
        is_primary=True,
        status="active",
    )
    fields.update(overrides)
    return ChannelBinding(**fields)


def _installation(**overrides) -> ConnectorInstallation:
    """A healthy connector installation for tests."""
    fields = dict(
        id="i-1",
        merchant_id="shop",
        connector_key="whatsapp",
        external_account_id="waba-1",
        credential_id="cred-1",
        status="healthy",
    )
    fields.update(overrides)
    return ConnectorInstallation(**fields)


def _credential(**overrides) -> Credential:
    """A live vault row as get_credential_by_id(mask=False) returns it."""
    fields = dict(
        id="cred-1",
        reseller_id=None,
        name="wa-bundle",
        credential_type=CredentialType.CUSTOM,
        value={"system_user_token": "tok"},
        is_active=True,
    )
    fields.update(overrides)
    return Credential(**fields)


def _patch_credential(monkeypatch, credential) -> None:
    """Route resolution's vault read, without a database."""

    async def _get(credential_id, mask=True, raise_errors=False):
        """Test double: the seeded vault credential."""
        # Pinned: the adapter needs the real secret, never the API's mask —
        # and the send path must ask for errors raised, or an outage on this
        # one read folds into None and terminally blocks the message.
        assert mask is False
        assert raise_errors is True
        return credential

    monkeypatch.setattr(accounts_module, "get_credential_by_id", _get)


def _route(**overrides) -> SendRoute:
    """A fully resolved SendRoute for tests."""
    fields = dict(
        installation=_installation(),
        binding=_binding(),
        bundle=CredentialBundle(values={"system_user_token": "tok"}),
    )
    fields.update(overrides)
    return SendRoute(**fields)


#: What the registry answers for an approved template, unless a test seeds
#: something else. One row = one language = sendable.
_APPROVED = [ApprovedTemplate(id="t-1", name="order_update_v1", language="en_US")]


class _FakeAccessor:
    """Stands in for every db/accessors module send.py reads, so the door is
    testable without a database — which is the whole reason routes are
    resolved in one place.

    One double for three modules on purpose: the point under test is the
    ORDER of the resolver's reads and what it refuses on, and splitting it
    into three doubles would only make each test say which of them to seed.
    """

    def __init__(self, binding=None, installation=None, approved=None):
        """Test double."""
        self._binding = binding
        self._installation = installation
        self._approved = _APPROVED if approved is None else approved
        self.writes: list = []

    async def get_binding(self, merchant_id, channel, binding_id):
        """Test double: the seeded binding, or a hang/None per scenario."""
        return self._binding

    async def get_installation(self, merchant_id, installation_id):
        """Test double: the seeded installation."""
        return self._installation

    async def approved_templates_for_send(
        self, merchant_id, channel, provider_account_ref, name
    ):
        """Test double: the seeded registry answer for this template name."""
        return self._approved

    def __getattr__(self, name):
        """Anything beyond the reads above is recorded, not raised.

        Only reached when normal lookup fails, so the reads keep working. This
        turns "the send path called something else" into a readable assertion
        about WHAT it called, rather than an AttributeError that a reader has
        to decode.
        """

        async def _record(*args, **kwargs):
            """Record any non-read accessor call as a write."""
            self.writes.append(name)
            return True

        return _record


def _patch_accessors(monkeypatch, fake) -> None:
    """Point every accessor the send path reaches at one double.

    The registry read moved to templates.py (it owns crm_channel_template's
    reads), so the double is installed there rather than on send.py — the
    resolver's behaviour under test is unchanged either way.
    """
    for name in ("binding_accessor", "installation_accessor"):
        monkeypatch.setattr(send_module, name, fake)
    monkeypatch.setattr(template_reads_module, "template_accessor", fake)


@pytest.fixture
def happy_accessor(monkeypatch) -> _FakeAccessor:
    """Test double."""
    fake = _FakeAccessor(binding=_binding(), installation=_installation())
    _patch_accessors(monkeypatch, fake)
    _patch_credential(monkeypatch, _credential())
    return fake


# --- the registry is the channel vocabulary ---------------------------------


def test_every_registered_adapter_answers_to_its_own_key() -> None:
    """Every registered adapter answers to its own key."""
    # The registry key and the adapter's channel must agree, or send() would
    # look up "whatsapp" and hand the message to something else entirely.
    for key, adapter in ADAPTERS.items():
        assert adapter.channel == key
        assert isinstance(adapter, ChannelAdapter)


def test_whatsapp_is_the_only_shipped_channel() -> None:
    """Whatsapp is the only shipped channel."""
    # Not a tautology: it fails the day someone registers a half-built
    # adapter, which is exactly when a review should notice. It also pins the
    # decision that there is no stand-in adapter — local runs point the Graph
    # base URL at a stub and exercise the real one.
    assert set(ADAPTERS) == {"whatsapp"}
    assert isinstance(adapter_for("whatsapp"), MetaWhatsAppAdapter)


def test_an_unknown_channel_returns_none_rather_than_raising() -> None:
    """An unknown channel returns none rather than raising."""
    # The caller must be able to record a terminal reason on the row; an
    # exception here would take the whole worker pass instead.
    assert adapter_for("sms") is None
    assert adapter_for("") is None


def test_masking_is_channel_keyed_and_reveals_the_last_four_at_most() -> None:
    """Masking is channel keyed and reveals the last four at most."""
    # The channel picks the rule — an address is only maskable by knowing
    # what kind of thing it is. WhatsApp uses the pre-existing mask_phone,
    # so these are the exact renderings connectivity's log lines carry.
    assert mask_address("+919876543210", "whatsapp") == "******3210"
    assert mask_address("", "whatsapp") == "****"
    # A short value is fully masked, never revealed whole behind a prefix.
    assert mask_address("12", "whatsapp") == "****"
    assert mask_address("9198", "whatsapp") == "****"


def test_a_channel_without_a_masking_rule_reveals_nothing() -> None:
    """A channel without a masking rule reveals nothing."""
    # Fail closed: a new channel must opt in to showing ANY part of its
    # addresses. Revealing by default would leak until somebody remembered
    # redact.py exists.
    assert mask_address("+919876543210", "carrier_pigeon") == "****"
    assert mask_address("someone@example.com", "email") == "****"


def test_digit_runs_long_enough_to_be_a_number_are_masked() -> None:
    """Digit runs long enough to be a number are masked."""
    # For text WE did not write: a provider's error message may echo the
    # value it rejected. Short runs — error codes, dates, counts — survive,
    # because a log stripped of every number stops being useful.
    assert (
        mask_digit_runs("Invalid parameter: to=919812345670")
        == "Invalid parameter: to=****"
    )
    assert mask_digit_runs("(#131049) rate limit hit") == "(#131049) rate limit hit"
    assert mask_digit_runs("") == ""


# --- the token: a grant names one message -----------------------------------


def test_a_token_for_another_message_grants_nothing() -> None:
    """A token for another message grants nothing."""
    # The failure this prevents: one grant reused across a claimed batch,
    # authorising customers it never named.
    message = _message()
    assert token_grants(_token(message), message) is True
    assert token_grants(_token(message), _message(id="m-2")) is False


def test_a_token_for_another_purpose_grants_nothing() -> None:
    """A token for another purpose grants nothing."""
    # Consent is granted per purpose, so a marketing send may not ride an
    # order-update grant.
    message = _message()
    assert token_grants(_token(message), _message(purpose_key="promotions")) is False


def test_an_ungranted_token_is_refused() -> None:
    """An ungranted token is refused."""
    message = _message()
    ungranted = SendToken(
        message_id=message.id, purpose_key=message.purpose_key, granted=False
    )
    assert token_grants(ungranted, message) is False


def test_send_refuses_before_touching_an_adapter(monkeypatch) -> None:
    """Send refuses before touching an adapter."""
    # No accessor is patched in: reaching the route lookup would hit the real
    # database layer and come back as 'send_error', so the reason asserted
    # here also proves the token check runs FIRST.
    message = _message()
    stranger = SendToken(message_id="m-2", purpose_key="order_update", granted=True)
    outcome = asyncio.run(send(stranger, message))
    # 'blocked', not 'failed': this is OUR refusal, not the provider's.
    assert outcome.status == "blocked"
    assert outcome.reason == REASON_GATE_REFUSED
    assert outcome.retryable is False


# --- fail closed: every missing piece refuses, none retry --------------------


async def test_an_unserved_channel_is_terminal() -> None:
    """An unserved channel is terminal."""
    message = _message(channel="carrier_pigeon")
    outcome = await send(_token(message), message)
    assert outcome.reason == REASON_NO_ADAPTER
    assert outcome.retryable is False


@pytest.mark.parametrize(
    "accessor_kwargs,credential,expected",
    [
        # Never connected, paused, retired, or another tenant's row: the
        # accessor returns nothing for all of them, and one reason covers it.
        (dict(binding=None), None, REASON_NO_BINDING),
        (dict(binding=_binding(), installation=None), None, REASON_NO_INSTALLATION),
        (
            dict(binding=_binding(), installation=_installation(credential_id=None)),
            None,
            REASON_NO_CREDENTIAL,
        ),
        # Vault row gone, deactivated via the credentials API, or its value
        # would not decrypt ({}): all the same "broken connection" answer.
        (
            dict(binding=_binding(), installation=_installation()),
            None,
            REASON_NO_CREDENTIAL,
        ),
        (
            dict(binding=_binding(), installation=_installation()),
            _credential(is_active=False),
            REASON_NO_CREDENTIAL,
        ),
        (
            dict(binding=_binding(), installation=_installation()),
            _credential(value={}),
            REASON_NO_CREDENTIAL,
        ),
    ],
)
async def test_a_broken_route_refuses_and_never_retries(
    monkeypatch, accessor_kwargs, credential, expected
) -> None:
    """A broken route refuses and never retries."""
    _patch_accessors(monkeypatch, _FakeAccessor(**accessor_kwargs))
    _patch_credential(monkeypatch, credential)
    message = _message()
    outcome = await send(_token(message), message)
    # 'blocked', not 'failed': a missing route is US refusing (T16 col 12).
    assert outcome.status == "blocked"
    assert outcome.reason == expected
    # Retrying cannot conjure a connection the merchant never made.
    assert outcome.retryable is False


async def test_a_vault_outage_retries_instead_of_blocking(monkeypatch) -> None:
    """A vault outage retries instead of blocking."""
    # None from the vault means "row gone/dead" and is rightly terminal above;
    # a raising read is the pool blipping, and the same blip one line earlier
    # (on get_binding) already retries. One transient failure, one answer.
    _patch_accessors(
        monkeypatch, _FakeAccessor(binding=_binding(), installation=_installation())
    )

    async def _outage(credential_id, mask=True, raise_errors=False):
        """Test double: the pool dies mid-read."""
        raise ConnectionError("pool exhausted")

    monkeypatch.setattr(accounts_module, "get_credential_by_id", _outage)
    message = _message()
    outcome = await send(_token(message), message)
    assert outcome.status == "failed"
    assert outcome.reason == REASON_SEND_ERROR
    assert outcome.retryable is True


@pytest.mark.parametrize("status", ["degraded", "revoked", "disabled"])
async def test_an_unhealthy_installation_refuses(monkeypatch, status) -> None:
    """An unhealthy installation refuses."""
    # Each of these states was chosen by somebody or by a prior failure; a
    # send must not quietly ignore it.
    _patch_accessors(
        monkeypatch,
        _FakeAccessor(binding=_binding(), installation=_installation(status=status)),
    )
    _patch_credential(monkeypatch, _credential())
    message = _message()
    outcome = await send(_token(message), message)
    assert outcome.reason == REASON_INSTALLATION_UNHEALTHY


async def test_a_connecting_installation_refuses(happy_accessor) -> None:
    """A connecting installation refuses — fail closed, no exceptions."""
    # Onboarding (#1038) verifies against the Graph API and writes rows as
    # 'healthy' directly, so 'connecting' is an unproven connection and
    # there is no first-send bootstrap to earn it a fail-open.
    happy_accessor._installation = _installation(status="connecting")
    route = await resolve_send_route("shop", "whatsapp", None)
    assert route == REASON_INSTALLATION_UNHEALTHY


async def test_a_resolved_route_carries_the_whole_context(happy_accessor) -> None:
    """A resolved route carries the whole context."""
    route = await resolve_send_route("shop", "whatsapp", None, "order_update_v1")
    assert isinstance(route, SendRoute)
    assert route.binding.address == "1234567890"
    assert route.bundle.secret("system_user_token") == "tok"
    # And the registry row it was approved as, which is the whole reason the
    # adapter no longer reads a language off the binding.
    assert route.template is not None
    assert route.template.language == "en_US"


def test_both_binding_lookups_are_pinned_to_their_channel() -> None:
    """Both binding lookups are pinned to their channel."""
    # binding_id on a message is a bare uuid with no FK, so a row could name
    # a binding of a DIFFERENT channel — and without the filter that
    # binding's address would reach the message's adapter as if it were its
    # own kind of endpoint. A mismatch must be 'no route', exactly like
    # naming another tenant's row, never a wrong-endpoint send.
    named_sql, named_values = binding_by_id_query("shop", "b-1", "whatsapp")
    assert "channel = $3" in named_sql
    assert named_values == ["shop", "b-1", "whatsapp", BINDING_ACTIVE]
    primary_sql, primary_values = primary_binding_query("shop", "whatsapp")
    assert "channel = $2" in primary_sql
    assert primary_values == ["shop", "whatsapp", BINDING_ACTIVE]


# --- the timeout no adapter can forget --------------------------------------


class _HangingAdapter(ChannelAdapter):
    channel = "whatsapp"

    async def deliver(self, message, route):
        """Test double: adapter deliver with a scripted behaviour."""
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


async def test_a_hung_provider_becomes_a_retryable_failure(
    monkeypatch, happy_accessor
) -> None:
    """A hung provider becomes a retryable failure."""
    # The lease, not politeness, is why this exists: a send that outlives its
    # claim gets the row reassigned mid-flight and the customer gets two.
    monkeypatch.setattr(send_module, "adapter_for", lambda channel: _HangingAdapter())
    monkeypatch.setattr(send_module, "CRM_MESSAGE_SEND_TIMEOUT_SECONDS", 0.05)
    message = _message()
    outcome = await send(_token(message), message)
    assert outcome.status == "failed"
    assert outcome.reason == REASON_TIMEOUT
    assert outcome.retryable is True


async def test_a_hung_route_lookup_is_bounded_by_the_same_deadline(
    monkeypatch,
) -> None:
    """A hung route lookup is bounded by the same deadline."""

    # The lease does not care WHERE a send hangs: a stalled connection pool
    # during the route's reads outlives the claim exactly like a hung
    # provider, with the same reassigned row and the same double send. One
    # deadline covers both.
    class _HangingAccessor:
        async def get_binding(self, merchant_id, channel, binding_id):
            """Test double: the seeded binding, or a hang/None per scenario."""
            await asyncio.sleep(3600)

    monkeypatch.setattr(send_module, "binding_accessor", _HangingAccessor())
    monkeypatch.setattr(send_module, "CRM_MESSAGE_SEND_TIMEOUT_SECONDS", 0.05)
    message = _message()
    outcome = await send(_token(message), message)
    assert outcome.status == "failed"
    assert outcome.reason == REASON_TIMEOUT
    assert outcome.retryable is True


async def test_an_escaping_exception_becomes_the_default_outcome(
    monkeypatch, happy_accessor
) -> None:
    """An escaping exception becomes the default outcome."""

    # The default case. Adapters classify their own failures, so anything
    # raising out of one is an escape from classification — it must land as
    # an outcome on the row rather than as an exception the worker has to
    # guess about, and it retries because we cannot know whether the
    # provider saw it.
    class _Raises(ChannelAdapter):
        channel = "whatsapp"

        async def deliver(self, message, route):
            """Test double: adapter deliver with a scripted behaviour."""
            raise ValueError("classification escape")

    monkeypatch.setattr(send_module, "adapter_for", lambda channel: _Raises())
    message = _message()
    outcome = await send(_token(message), message)
    assert outcome.status == "failed"
    assert outcome.reason == REASON_SEND_ERROR
    assert outcome.retryable is True


def test_a_whole_batch_of_worst_case_sends_fits_inside_the_claim_lease() -> None:
    """A whole batch of worst case sends fits inside the claim lease."""
    # Two inequalities, one law. A timeout above the lease would be no
    # timeout at all — and a BATCH that outlasts the lease is worse: sends
    # are serial, so rows claimed at the head of a pass sit in 'sending'
    # while the tail is worked, and another pod's sweep re-sends the tail
    # a first pod still holds. That duplicate is a real message to a real
    # person; no outcome guard can undo it.
    from app.core.config.static import (
        CRM_DISPATCH_BATCH,
        CRM_DISPATCH_STALE_MINUTES,
        CRM_MESSAGE_SEND_TIMEOUT_SECONDS,
    )

    lease_seconds = CRM_DISPATCH_STALE_MINUTES * 60
    assert CRM_MESSAGE_SEND_TIMEOUT_SECONDS < lease_seconds
    # The whole-batch bound. The 2× is no longer just margin: each message
    # may burn one full timeout in the gate probe (its own wait_for in
    # dispatch._gate) and another in send(), so 2× IS the worst case — at
    # the defaults 20 × 20s × 2 = 800s against 900s, leaving 100s for pass
    # overhead (DB reads, backoff writes). Nudge one dial and the others
    # must follow.
    assert CRM_DISPATCH_BATCH * CRM_MESSAGE_SEND_TIMEOUT_SECONDS * 2 <= lease_seconds


# --- the send path reads the route tables, it never writes them -------------


async def test_a_credential_refusal_does_not_touch_connection_state(
    monkeypatch, happy_accessor
) -> None:
    """An expired token fails the row with the provider's code and stops there.

    Marking the connection degraded belongs to the channel module. Two owners
    for one lifecycle is how a status ends up flapping between a health probe
    that says 'healthy' and a send path that says 'degraded'.
    """

    class _RejectsCredentials(ChannelAdapter):
        channel = "whatsapp"

        async def deliver(self, message, route):
            """Test double: adapter deliver with a scripted behaviour."""
            return SendOutcome(status="failed", reason="190")

    monkeypatch.setattr(
        send_module, "adapter_for", lambda channel: _RejectsCredentials()
    )
    message = _message()
    outcome = await send(_token(message), message)
    assert outcome.reason == "190"
    assert outcome.retryable is False
    # The reason on the row is the whole signal the channel module needs.
    assert happy_accessor.writes == []


# --- the credential bundle keeps secrets out of logs ------------------------


def test_a_bundle_does_not_print_its_secrets() -> None:
    """A bundle does not print its secrets."""
    # The cheapest guard against the mistake that ships a token to a log
    # aggregator: an f-string of the bundle must reveal nothing.
    bundle = CredentialBundle(values={"system_user_token": "SUPER_SECRET"})
    assert "SUPER_SECRET" not in repr(bundle)
    assert "SUPER_SECRET" not in f"{bundle}"


def test_a_missing_or_blank_secret_reads_as_absent() -> None:
    """A missing or blank secret reads as absent."""
    bundle = CredentialBundle(values={"system_user_token": "", "app_secret": None})
    assert bundle.secret("system_user_token") is None
    assert bundle.secret("app_secret") is None
    assert bundle.secret("nope") is None


# --- the route tables --------------------------------------------------------


def test_the_route_tables_are_owned_by_connectivity() -> None:
    """The route tables are owned by connectivity."""
    assert TABLE_OWNERS["crm_connector_installation"] == "connectivity"
    assert TABLE_OWNERS["crm_channel_binding"] == "connectivity"
    assert TABLE_OWNERS["crm_message"] == "connectivity"


def test_the_vault_is_read_through_its_own_accessor() -> None:
    """The vault is read through its own accessor."""
    # Table-ownership law: `credentials` belongs to app/database, so the one
    # sanctioned seam is its accessor — no vault SQL in connectivity's
    # builders, and send.py's read really is app/database's function.
    # The split put one file per table under db/queries/; the law is about
    # all of them, so the assertion walks the folder rather than naming a
    # file a future table would quietly escape. Matching on SQL position
    # rather than the bare word: prose may say "credentials" (it is the name
    # of the thing these tables point AT), a statement may not name it.
    vault_in_sql = re.compile(
        r'(?:FROM|INTO|UPDATE|JOIN)\s+"?credentials"?', re.IGNORECASE
    )
    for builder in Path("app/crm/connectivity/db/queries").glob("*.py"):
        assert not vault_in_sql.search(builder.read_text()), builder
    assert (
        accounts_module.get_credential_by_id.__module__
        == "app.database.accessor.breeze_buddy.credentials"
    )


def test_one_home_for_the_account_policy() -> None:
    """The usable-states set and the vault sequence live in accounts.py only.

    Two definitions of one policy is a policy that changes in one place and
    not the other — the day a degraded door may register templates but not
    send, the miss would be silent. Same reason the registry read belongs to
    templates.py: send.py owning a second read on crm_channel_template would
    be a second answer to "is this approved".
    """
    send_src = Path("app/crm/connectivity/send.py").read_text()
    templates_src = Path("app/crm/connectivity/templates.py").read_text()
    for src in (send_src, templates_src):
        assert "get_credential_by_id" not in src
        assert '!= "healthy"' not in src and 'frozenset({"healthy"})' not in src
    assert "template_accessor" not in send_src
    assert accounts_module.USABLE_INSTALLATION_STATES == frozenset({"healthy"})


def test_both_tables_are_created_together() -> None:
    """Both tables are created together."""
    # They are meaningless apart — a binding has a foreign key into an
    # installation — so splitting them would leave a numbered window in which
    # the schema is half-built.
    sql = _ddl()
    assert "CREATE TABLE crm_connector_installation (" in sql
    assert "CREATE TABLE crm_channel_binding (" in sql
    # And the door must come first, or the pipe's FK has nothing to reference.
    assert sql.index("CREATE TABLE crm_connector_installation (") < sql.index(
        "CREATE TABLE crm_channel_binding ("
    )


def test_a_credential_in_use_cannot_be_deleted() -> None:
    """A credential in use cannot be deleted."""
    # Without this, deleting a secret through the credentials API silently
    # breaks every send for that merchant, and the only symptom arrives much
    # later as "credential missing" on failed rows.
    sql = _table_ddl("crm_connector_installation")
    assert "REFERENCES credentials (id) ON DELETE RESTRICT" in sql
    # Never CASCADE: deleting a secret must not delete the record that a
    # merchant ever connected. Disconnecting is a status change.
    assert "ON DELETE CASCADE" not in _ddl()


def test_an_installation_never_stores_a_secret() -> None:
    """An installation never stores a secret."""
    # The whole point of the credential_id pointer. A column named for a token
    # here would mean secrets in every backup of this table.
    sql = _table_ddl("crm_connector_installation")
    for forbidden in ("access_token", "system_user_token", "secret", "password"):
        assert forbidden not in sql, forbidden
    assert "credential_id       uuid" in sql


def test_connector_key_and_channel_have_no_check() -> None:
    """Connector key and channel have no check."""
    # The migration-027 scar: vocabulary in a CHECK made a new channel a
    # migration. Both columns must stay plain text.
    assert "CHECK (connector_key" not in _ddl()
    assert "CHECK (channel" not in _ddl()


def test_lifecycle_states_are_closed_enums() -> None:
    """Lifecycle states are closed enums."""
    # Status is not vocabulary: it changes only when the lifecycle does.
    installation = _table_ddl("crm_connector_installation")
    assert "CHECK (status IN (" in installation
    for state in ("connecting", "healthy", "degraded", "revoked", "disabled"):
        assert f"'{state}'" in installation, state
    binding = _table_ddl("crm_channel_binding")
    assert "CHECK (status IN (" in binding
    for state in ("active", "paused", "retired"):
        assert f"'{state}'" in binding, state
    # Sliced per table on purpose: without it, the binding's states would be
    # "found" in the installation's CHECK sitting in the same file.
    assert "'connecting'" not in binding


def test_there_is_no_stored_expired_state() -> None:
    """There is no stored expired state."""
    # Expiry is the predicate token_expires_at < now(). A stored copy is
    # correct only until the clock passes it.
    assert "'expired'" not in _ddl()


def test_these_tables_carry_no_read_only_indexes() -> None:
    """These tables carry no read only indexes."""
    # Configuration tables: one row per merchant per account, one per endpoint.
    # At that size a sequential scan is sub-millisecond, so every index here
    # has to earn its write cost by being a CORRECTNESS rule. The send path is
    # served entirely by the unique ones — (merchant_id, connector_key) is a
    # prefix of the account index, and the primary-pipe lookup IS the partial
    # unique. A plain CREATE INDEX appearing here means someone added a read
    # optimisation these tables do not need.
    plain = re.findall(r"CREATE INDEX (\w+)", _ddl())
    assert plain == [], plain


def test_every_merchant_scoped_unique_index_leads_with_merchant() -> None:
    """Every merchant scoped unique index leads with merchant."""
    # One exception, named here rather than skipped silently: a law with an
    # anonymous exception is not a law. It is asserted on its own below.
    EXCEPTION = "crm_channel_binding_address_uq"
    found = re.findall(r"CREATE UNIQUE INDEX (\w+)\s+ON \w+ \(([^)]+)\)", _ddl())
    assert len(found) >= 4, found
    for name, columns in found:
        if name == EXCEPTION:
            continue
        assert columns.strip().startswith("merchant_id"), name


def test_one_live_address_belongs_to_one_merchant() -> None:
    """One live address belongs to one merchant."""
    # The deliberate exception to merchant-first, and the reason phase 3 can
    # resolve a merchant from an inbound receipt at all.
    sql = _table_ddl("crm_channel_binding")
    assert "crm_channel_binding (channel, address)" in sql
    # Retired rows are excluded so a recycled number can live again elsewhere.
    assert "WHERE status <> 'retired'" in sql


def test_a_merchant_has_at_most_one_primary_pipe_per_channel() -> None:
    """A merchant has at most one primary pipe per channel."""
    sql = _table_ddl("crm_channel_binding")
    assert "crm_channel_binding (merchant_id, channel)" in sql
    assert "WHERE is_primary" in sql


def test_a_binding_cannot_hang_off_another_tenants_installation() -> None:
    """A binding cannot hang off another tenants installation."""
    # Composite FK, same protection crm_message gives its customer link.
    sql = _table_ddl("crm_channel_binding")
    assert "FOREIGN KEY (merchant_id, installation_id)" in sql
    assert "REFERENCES crm_connector_installation (merchant_id, id)" in sql


# --- the T23 send-time template lookup (ADR 0011) ---------------------------
#
# A non-approved template must be refused BEFORE the provider call. Without
# this, a pending or rejected name goes to Meta and fails there — the wrong
# side of the wire, where it costs an attempt, a rate-limit budget and a
# support ticket instead of a manifest row with an honest reason.


class _CountingAdapter(ChannelAdapter):
    """Records whether the send path ever reached a provider."""

    channel = "whatsapp"

    def __init__(self):
        """Test double."""
        self.calls = 0

    async def deliver(self, message, route):
        """Test double: count the call that should never happen."""
        self.calls += 1
        return SendOutcome(status="accepted", provider_message_id="wamid.1")


@pytest.fixture
def counting_adapter(monkeypatch) -> _CountingAdapter:
    """Test double."""
    adapter = _CountingAdapter()
    monkeypatch.setattr(send_module, "adapter_for", lambda channel: adapter)
    return adapter


async def test_an_approved_template_passes_to_the_adapter(
    monkeypatch, happy_accessor, counting_adapter
) -> None:
    """One approved row, so exactly one language — the send may proceed."""
    message = _message()
    outcome = await send(_token(message), message)
    assert outcome.status == "accepted"
    assert counting_adapter.calls == 1


@pytest.mark.parametrize(
    "approved,why",
    [
        ([], "never registered, pending, rejected or deleted"),
        (
            [
                ApprovedTemplate(id="t-1", name="order_update_v1", language="en_US"),
                ApprovedTemplate(id="t-2", name="order_update_v1", language="hi_IN"),
            ],
            "approved in more than one language",
        ),
    ],
)
async def test_a_template_that_is_not_one_approved_row_is_refused(
    monkeypatch, counting_adapter, approved, why
) -> None:
    """Zero rows and many rows are one fact from the sender's side.

    The ambiguous case earns the same refusal rather than a default: a
    manifest row carries the template NAME and no language, so picking one
    locale would be guessing which language a customer reads — and the wrong
    guess sends an unreadable message under a merchant's name.
    """
    _patch_accessors(
        monkeypatch,
        _FakeAccessor(
            binding=_binding(), installation=_installation(), approved=approved
        ),
    )
    _patch_credential(monkeypatch, _credential())
    message = _message()
    outcome = await send(_token(message), message)
    assert outcome.status == "blocked", why
    assert outcome.reason == "template_not_approved"
    assert outcome.retryable is False
    # The provider never saw it, which is the whole point.
    assert counting_adapter.calls == 0


async def test_a_message_naming_no_template_never_reaches_a_provider(
    monkeypatch, happy_accessor, counting_adapter
) -> None:
    """Refused in the resolver rather than in the adapter, so the reason is
    the same for every channel instead of one per adapter."""
    message = _message(template_id=None)
    outcome = await send(_token(message), message)
    assert outcome.status == "blocked"
    assert outcome.reason == "template_missing"
    assert counting_adapter.calls == 0


async def test_the_route_carries_the_registrys_language_not_the_bindings(
    monkeypatch,
) -> None:
    """Which locale a template was APPROVED in is a fact about the template.

    The binding's capabilities blob used to answer this, and could disagree
    with what the provider actually approved — the interim the manifest
    adapter's own docstring named.
    """
    _patch_accessors(
        monkeypatch,
        _FakeAccessor(
            binding=_binding(capabilities={"template_language": "de_DE"}),
            installation=_installation(),
            approved=[
                ApprovedTemplate(id="t-1", name="order_update_v1", language="hi_IN")
            ],
        ),
    )
    _patch_credential(monkeypatch, _credential())
    route = await resolve_send_route("shop", "whatsapp", None, "order_update_v1")
    assert isinstance(route, SendRoute)
    assert route.template is not None
    assert route.template.language == "hi_IN"


# --- the registry gate is a CHANNELS fact, not a send.py assumption ---------


def test_whether_a_channel_registers_templates_is_the_registrys_answer() -> None:
    """WhatsApp does; an unregistered channel answers True so the door fails
    CLOSED on it — the same posture the gate takes on a channel CHANNELS
    cannot describe."""
    from app.crm.connectivity.channels import registers_templates_for

    assert registers_templates_for("whatsapp") is True
    assert registers_templates_for("carrier_pigeon") is True


async def test_a_channel_that_does_not_register_templates_skips_the_registry(
    monkeypatch, counting_adapter
) -> None:
    """An email send names no registry row and must not be refused for
    lacking one. Before this the resolver assumed every channel pre-registers
    and would have blocked every such send with template_not_approved — the
    adapter's own "channel that does not pre-register" fallback was
    unreachable because the door refused first."""

    class _NeverAsked(_FakeAccessor):
        async def approved_templates_for_send(self, *args, **kwargs):
            """Test double: the registry must not be consulted at all."""
            raise AssertionError("registry read on a non-registering channel")

    _patch_accessors(
        monkeypatch, _NeverAsked(binding=_binding(), installation=_installation())
    )
    _patch_credential(monkeypatch, _credential())
    monkeypatch.setattr(send_module, "registers_templates_for", lambda channel: False)
    message = _message(template_id=None)
    outcome = await send(_token(message), message)
    assert outcome.status == "accepted"
    assert counting_adapter.calls == 1
    route = await resolve_send_route("shop", "whatsapp", None, None)
    assert isinstance(route, SendRoute)
    assert route.template is None


def test_the_lookup_is_scoped_to_the_merchants_own_account() -> None:
    """The registry's natural key includes the provider account, so one
    merchant's approved template can never be resolved for another's."""
    from app.crm.connectivity.db.queries.template import (
        approved_template_for_send_query,
    )

    sql, values = approved_template_for_send_query("shop", "whatsapp", "waba-1", "n")
    assert "merchant_id = $1" in sql
    assert "provider_account_ref = $3" in sql
    # the filter is still there, now bound rather than spelled in SQL text
    assert "status = $5" in sql
    assert values == ["shop", "whatsapp", "waba-1", "n", TEMPLATE_APPROVED]


class _ScriptedAdapter(ChannelAdapter):
    channel = "whatsapp"

    def __init__(self, outcome: SendOutcome) -> None:
        self._outcome = outcome

    async def deliver(self, message, route):
        """Test double: returns the scripted outcome, route untouched."""
        return self._outcome


async def test_an_accepted_send_names_the_pipe_it_left_on_and_a_refusal_does_not(
    monkeypatch, happy_accessor
) -> None:
    """T16 col 6: the outcome carries the binding the route resolved, so
    the dispatcher can stamp crm_message.binding_id once; a blocked outcome
    carries none — no pipe was used, and NULL is the honest row."""
    message = _message()
    accepted = SendOutcome(status="accepted", provider_message_id="wamid.T")
    monkeypatch.setattr(
        send_module, "adapter_for", lambda channel: _ScriptedAdapter(accepted)
    )
    outcome = await send(_token(message), message)
    assert outcome.status == "accepted" and outcome.binding_id == "b-1"

    blocked = SendOutcome(status="blocked", reason=REASON_GATE_REFUSED)
    monkeypatch.setattr(
        send_module, "adapter_for", lambda channel: _ScriptedAdapter(blocked)
    )
    outcome = await send(_token(message), message)
    assert outcome.status == "blocked" and outcome.binding_id is None
