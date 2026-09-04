"""connectivity/ingress.py: owners resolved, letters shaped for the spine.

The envelope's own half, over fake accessors: which merchant owns each
letter's owner, what a letter no merchant owns costs, the door heartbeat the
same lookup pays for, and the stamps the corpus requires (schema_version from
the app's API version, source from the body). Meta's wire shape itself is
test_meta_inbound.py's; the record door that calls this spec is
test_ingress_door.py's.
"""

from typing import Optional

import pytest

from app.core.config.static import META_WHATSAPP_GRAPH_VERSION
from app.crm.connectivity import ingress as ingress_module
from app.crm.connectivity.schemas.connector import (
    ChannelBinding,
    ConnectorInstallation,
)
from app.crm.connectivity.topics import TOPIC_STATUS, TOPIC_TEMPLATE_STATUS

OUR_NUMBER = "812345678901234"
OTHER_NUMBER = "812999999999999"
WABA = "waba-77"
TS = 1788177600


def _binding(merchant="shop", address=OUR_NUMBER) -> ChannelBinding:
    """An active binding for one of our numbers."""
    return ChannelBinding(
        id="b-1",
        merchant_id=merchant,
        channel="whatsapp",
        installation_id="i-1",
        address=address,
        status="active",
    )


def _installation(merchant="shop") -> ConnectorInstallation:
    """A connected account holding the WABA."""
    return ConnectorInstallation(
        id="i-1",
        merchant_id=merchant,
        connector_key="whatsapp",
        external_account_id=WABA,
        status="healthy",
    )


class _Fakes:
    """Fake accessors that record every lookup."""

    def __init__(self, bindings=None, installations=None):
        """Test double."""
        self.bindings = bindings if bindings is not None else {OUR_NUMBER: _binding()}
        self.installations = (
            installations if installations is not None else {WABA: _installation()}
        )
        self.binding_lookups: list = []
        self.installation_lookups: list = []
        self.stamped: list = []
        self.stamp_error: Optional[Exception] = None

    async def get_binding_for_inbound(
        self, channel, address
    ) -> Optional[ChannelBinding]:
        """Test double."""
        self.binding_lookups.append((channel, address))
        return self.bindings.get(address)

    async def get_installation_for_inbound(
        self, connector_key, external_account_id
    ) -> Optional[ConnectorInstallation]:
        """Test double."""
        self.installation_lookups.append((connector_key, external_account_id))
        return self.installations.get(external_account_id)

    async def stamp_last_event_at(self, merchant_id, installation_id) -> None:
        """Test double: the door's traffic heartbeat."""
        if self.stamp_error is not None:
            raise self.stamp_error
        self.stamped.append((merchant_id, installation_id))


@pytest.fixture
def fakes(monkeypatch) -> _Fakes:
    """The envelope over fake accessors."""
    doubles = _Fakes()
    monkeypatch.setattr(ingress_module, "binding_accessor", doubles)
    monkeypatch.setattr(ingress_module, "installation_accessor", doubles)
    return doubles


def _status(wamid="wamid.OUT", state="delivered") -> dict:
    """A delivery receipt item."""
    return {"id": wamid, "status": state, "timestamp": str(TS)}


def _value(number=OUR_NUMBER, **overrides) -> dict:
    """One "messages" value."""
    fields = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": number}}
    fields.update(overrides)
    return fields


def _body(*changes, waba=WABA) -> dict:
    """Meta's envelope; each change is (field, value)."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": waba,
                "time": TS,
                "changes": [{"field": f, "value": v} for f, v in changes],
            }
        ],
    }


async def test_a_receipt_is_filed_under_whoever_owns_the_number(fakes) -> None:
    """A receipt is filed under whoever owns the number."""
    letters = await ingress_module.META_INGRESS.envelope(
        {}, _body(("messages", _value(statuses=[_status()])))
    )
    assert len(letters) == 1
    letter = letters[0]
    assert letter.merchant_id == "shop"
    assert letter.source == "whatsapp"
    assert letter.topic == TOPIC_STATUS
    assert letter.external_id == "wamid.OUT:delivered"


async def test_schema_version_is_the_apps_webhook_api_version(fakes) -> None:
    # Canon T13 col 5: stamped at ingest from the source's own version,
    # never inferred later — what makes replay survivable.
    """Schema version is the app's webhook API version."""
    letters = await ingress_module.META_INGRESS.envelope(
        {}, _body(("messages", _value(statuses=[_status()])))
    )
    assert letters[0].schema_version == META_WHATSAPP_GRAPH_VERSION


async def test_a_template_letter_is_owned_through_the_waba(fakes) -> None:
    # Finding 2: no phone_number_id on these — the WABA's installation names
    # the merchant.
    """A template letter is owned through the WABA."""
    letters = await ingress_module.META_INGRESS.envelope(
        {},
        _body(
            (
                "message_template_status_update",
                {"event": "APPROVED", "message_template_id": "t-1"},
            )
        ),
    )
    assert len(letters) == 1
    assert letters[0].merchant_id == "shop"
    assert letters[0].topic == TOPIC_TEMPLATE_STATUS
    assert fakes.installation_lookups == [("whatsapp", WABA)]


async def test_an_owner_costs_one_lookup_however_many_letters(fakes) -> None:
    """An owner costs one lookup however many letters."""
    letters = await ingress_module.META_INGRESS.envelope(
        {},
        _body(
            ("messages", _value(statuses=[_status(wamid=f"w{n}") for n in range(5)]))
        ),
    )
    assert len(letters) == 5
    assert fakes.binding_lookups == [("whatsapp", OUR_NUMBER)]


async def test_one_callback_can_carry_two_merchants(monkeypatch) -> None:
    # Meta batches across accounts, so tenancy is per owner, never per
    # request.
    """One callback can carry two merchants."""
    doubles = _Fakes(
        bindings={
            OUR_NUMBER: _binding(),
            OTHER_NUMBER: _binding(merchant="other", address=OTHER_NUMBER),
        }
    )
    monkeypatch.setattr(ingress_module, "binding_accessor", doubles)
    monkeypatch.setattr(ingress_module, "installation_accessor", doubles)
    letters = await ingress_module.META_INGRESS.envelope(
        {},
        _body(
            ("messages", _value(statuses=[_status()])),
            ("messages", _value(number=OTHER_NUMBER, statuses=[_status(wamid="w2")])),
        ),
    )
    assert {letter.merchant_id for letter in letters} == {"shop", "other"}


async def test_an_unowned_number_files_nothing_and_spares_its_neighbours(
    fakes,
) -> None:
    # Filing it under any merchant would be a cross-tenant leak — and it
    # must not cost the letters beside it, which belong to someone else.
    """An unowned number files nothing and spares its neighbours."""
    letters = await ingress_module.META_INGRESS.envelope(
        {},
        _body(
            ("messages", _value(number="nobodys", statuses=[_status(wamid="wX")])),
            ("messages", _value(statuses=[_status()])),
        ),
    )
    assert [letter.external_id for letter in letters] == ["wamid.OUT:delivered"]


async def test_an_unowned_waba_drops_only_its_own_letters(monkeypatch) -> None:
    """An unowned WABA drops only its own letters."""
    doubles = _Fakes(installations={})
    monkeypatch.setattr(ingress_module, "binding_accessor", doubles)
    monkeypatch.setattr(ingress_module, "installation_accessor", doubles)
    letters = await ingress_module.META_INGRESS.envelope(
        {},
        _body(
            ("messages", _value(statuses=[_status()])),
            ("message_template_status_update", {"event": "APPROVED"}),
        ),
    )
    assert [letter.topic for letter in letters] == [TOPIC_STATUS]


async def test_owners_are_looked_up_by_channel_and_connector_key_never_source(
    monkeypatch,
) -> None:
    # The three words coincide for Meta; an SMS aggregator's face would say
    # source "msg91", channel "sms", connector_key "msg91". The binding is
    # keyed by the CHANNEL and the installation by the CONNECTOR KEY — a
    # root that reached for `source` would look up nothing for it.
    """Owners are looked up by channel and connector_key, never by source."""
    from app.crm.connectivity.schemas.ingress import (
        OWNER_ACCOUNT,
        OWNER_ENDPOINT,
        ProviderLetter,
    )

    doubles = _Fakes(
        bindings={"SENDER-ID": _binding(address="SENDER-ID")},
        installations={"acct-9": _installation()},
    )
    monkeypatch.setattr(ingress_module, "binding_accessor", doubles)
    monkeypatch.setattr(ingress_module, "installation_accessor", doubles)

    def letter(kind: str, owner: str, topic: str) -> ProviderLetter:
        return ProviderLetter(
            owner_kind=kind,
            owner_id=owner,
            source="msg91",
            channel="sms",
            connector_key="msg91",
            topic=topic,
            external_id=f"{owner}:{topic}",
            payload={},
            occurred_at=None,
            schema_version="v1",
        )

    out = await ingress_module.resolve_letters(
        [
            letter(OWNER_ENDPOINT, "SENDER-ID", TOPIC_STATUS),
            letter(OWNER_ACCOUNT, "acct-9", TOPIC_TEMPLATE_STATUS),
        ]
    )
    assert [l.merchant_id for l in out] == ["shop", "shop"]
    assert [l.source for l in out] == ["msg91", "msg91"]
    assert doubles.binding_lookups == [("sms", "SENDER-ID")]
    assert doubles.installation_lookups == [("msg91", "acct-9")]


async def test_the_spec_is_assembled_from_the_meta_face() -> None:
    """The spec is assembled from the meta face."""
    from app.crm.connectivity.providers.meta import inbound

    assert ingress_module.META_INGRESS.verify is inbound.verify_signature
    assert ingress_module.META_INGRESS.challenge is inbound.handshake_challenge


# ---------------------------------------------------------------------------
# The door's traffic heartbeat (canon T11 col 10)
# ---------------------------------------------------------------------------


async def test_an_owned_letter_stamps_the_door_it_arrived_through(fakes) -> None:
    """The column exists to catch a subscription that silently dropped, and
    that failure is only visible as this stamp ceasing to advance — so every
    kind of letter has to move it, not just the ones a consumer reads."""
    await ingress_module.META_INGRESS.envelope(
        {}, _body(("messages", _value(statuses=[_status()])))
    )
    assert fakes.stamped == [("shop", "i-1")]


async def test_a_template_letter_stamps_the_same_door(fakes) -> None:
    """A WABA-owned letter resolves through the installation directly."""
    await ingress_module.META_INGRESS.envelope(
        {},
        _body(("message_template_status_update", {"message_template_id": "T-1"})),
    )
    assert fakes.stamped == [("shop", "i-1")]


async def test_one_stamp_per_door_however_many_letters(fakes) -> None:
    """Grouped by owner, like the lookup it rides on: a callback carrying
    four receipts for one number is one heartbeat, not four writes."""
    await ingress_module.META_INGRESS.envelope(
        {},
        _body(
            (
                "messages",
                _value(
                    statuses=[
                        _status(wamid="wamid.A", state="sent"),
                        _status(wamid="wamid.A", state="delivered"),
                        _status(wamid="wamid.B", state="read"),
                    ]
                ),
            )
        ),
    )
    assert fakes.stamped == [("shop", "i-1")]


async def test_a_letter_nobody_owns_stamps_nothing(monkeypatch) -> None:
    """No owner, no door: there is nothing to say traffic arrived on."""
    doubles = _Fakes(bindings={}, installations={})
    monkeypatch.setattr(ingress_module, "binding_accessor", doubles)
    monkeypatch.setattr(ingress_module, "installation_accessor", doubles)
    letters = await ingress_module.META_INGRESS.envelope(
        {}, _body(("messages", _value(statuses=[_status()])))
    )
    assert letters == []
    assert doubles.stamped == []


async def test_a_failed_stamp_never_costs_the_letter(fakes) -> None:
    """Bookkeeping, not the point of the request. Failing the callback over a
    heartbeat would make Meta retry a letter we already understood — and the
    retry would fail exactly the same way."""
    fakes.stamp_error = RuntimeError("pool is gone")
    letters = await ingress_module.META_INGRESS.envelope(
        {}, _body(("messages", _value(statuses=[_status()])))
    )
    assert len(letters) == 1
    assert letters[0].topic == TOPIC_STATUS
