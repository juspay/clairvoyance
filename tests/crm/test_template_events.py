"""The template webhook consumer, and the extractor that lets its letters
reach it.

Two halves of one path, tested together because either one alone is a
no-op. Meta decides a template's fate and pushes it; the bay files the
letter to the spine; record's pass has to recognise the letter as
merchant-level rather than quarantining it for naming no customer; and this
consumer is the only thing that ever writes provider-decided template state.
There is no timer anywhere behind it — the periodic sync was removed before
it ever ran.

The scars these pin, in the order they would bite:

* an unregistered source falls back to the flat extractor, finds no phone,
  and quarantines EVERY letter as ``no_handle`` — with the consumer wired,
  green, and never once called;
* a template letter's payload carries no WABA (Meta puts the account in the
  envelope), so anything that needs one has to get it from the ROW;
* three out-of-order guards, each on a nullable column, each of which drops
  a real approval when its NULL branch is missing — ``quality_updated_at``
  has no writer at all before this consumer, so its first letter always
  meets NULL;
* a provider stamping whole seconds against our own sub-second clock.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast

import pytest

import app.crm.record.consumers as record_consumers
import app.crm.record.extractors.whatsapp as whatsapp_extractor
import app.crm.record.workers as workers
from app.crm.connectivity import template_events as events_module
from app.crm.connectivity.connectors import CONNECTORS, connector_for_source
from app.crm.connectivity.db.queries.template import (
    apply_category_event_query,
    apply_quality_event_query,
    apply_status_event_query,
    resume_submitted_template_query,
    submitting_template_by_natural_key_query,
    template_by_provider_id_query,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.connectivity.schemas.template import TemplateRead
from app.crm.connectivity.template_events import consume_template_event
from app.crm.connectivity.topics import (
    TOPIC_ACCOUNT,
    TOPIC_INBOUND,
    TOPIC_STATUS,
    TOPIC_TEMPLATE_CATEGORY,
    TOPIC_TEMPLATE_QUALITY,
    TOPIC_TEMPLATE_STATUS,
)
from app.crm.record.db import DbTxn
from app.crm.record.extractors import EXTRACTORS
from app.crm.record.schemas import ABOUT_CUSTOMER, ABOUT_MERCHANT, RawEvent

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The extractor: whose letter is this?
# ---------------------------------------------------------------------------


def test_the_bays_source_is_registered() -> None:
    """Without this line every Meta letter falls to the flat shape."""
    assert EXTRACTORS["whatsapp"] is whatsapp_extractor.extract


def test_a_template_review_names_no_person_by_design() -> None:
    extracted = whatsapp_extractor.extract(
        {
            "event": "APPROVED",
            "message_template_id": "T-1",
            "message_template_name": "order_update",
            "message_template_language": "en_US",
        }
    )
    assert extracted.about == ABOUT_MERCHANT
    assert extracted.handles == {}


def test_an_account_notice_is_merchant_level_too() -> None:
    extracted = whatsapp_extractor.extract({"event": "VERIFIED", "phone_number": "+91"})
    assert extracted.about == ABOUT_MERCHANT


def test_a_receipt_stays_a_customer_letter_for_c6() -> None:
    """Claiming a delivery receipt is merchant-level would stamp it processed
    with a NULL customer forever — silently destroying the attribution the
    receipt walker is written to make. It keeps today's behaviour instead:
    no handle, so the pass quarantines it loudly and replayably."""
    for key in ("statuses", "messages"):
        extracted = whatsapp_extractor.extract({key: [{"id": "wamid.A"}]})
        assert extracted.about == ABOUT_CUSTOMER
        assert extracted.handles == {}


# ---------------------------------------------------------------------------
# The pass: the letter has to survive it
# ---------------------------------------------------------------------------


class _FakeSavepoint:
    async def __aenter__(self) -> "_FakeSavepoint":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeTxnImpl:
    def transaction(self) -> _FakeSavepoint:
        return _FakeSavepoint()


class _PassAccessor:
    def __init__(self) -> None:
        """Test double: what the pass did with the row."""
        self.stamped: List[Tuple[str, Optional[str]]] = []
        self.quarantined: List[Tuple[str, str]] = []
        self.detected: List[Tuple[str, str, str]] = []

    async def stamp_event(self, conn, event_id, customer_id) -> None:
        """Test double."""
        self.stamped.append((event_id, customer_id))

    async def quarantine_event(self, conn, event_id, reason) -> None:
        """Test double."""
        self.quarantined.append((event_id, reason))

    async def insert_detected_schema(self, conn, merchant_id, source, topic) -> None:
        """Test double: the catalog's "somebody should declare this" nudge,
        which every source outside SPEC_MODULES trips."""
        self.detected.append((merchant_id, source, topic))


async def test_a_template_letter_survives_the_pass_and_reaches_its_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression that makes the whole feature a no-op without failing.

    Before the extractor existed, this letter met the flat shape, produced no
    handle, and was quarantined as ``no_handle`` BEFORE any consumer ran. The
    consumer would have been registered, imported, type-checked and green,
    and Meta's approvals would simply never have arrived.
    """
    heard: List[Tuple[str, Optional[str]]] = []

    async def _consumer(event, customer_id, handles, variables) -> None:
        heard.append((event.topic, customer_id))

    monkeypatch.setattr(record_consumers, "_CONSUMERS", [_consumer])
    accessor = _PassAccessor()
    monkeypatch.setattr(workers, "accessor", accessor)

    await workers._process_one(cast(DbTxn, _FakeTxnImpl()), _event())

    assert accessor.quarantined == []
    # canon T13 col 14: processed, NULL customer, forever and correctly.
    assert accessor.stamped == [("e-1", None)]
    assert heard == [(TOPIC_TEMPLATE_STATUS, None)]


# ---------------------------------------------------------------------------
# The consumer
# ---------------------------------------------------------------------------


def _event(topic: str = TOPIC_TEMPLATE_STATUS, **overrides) -> RawEvent:
    fields: Dict[str, Any] = dict(
        id="e-1",
        merchant_id="shop",
        source="whatsapp",
        topic=topic,
        schema_version="v23.0",
        external_id="waba-1:T-1:APPROVED:1788177600",
        payload={
            "event": "APPROVED",
            "message_template_id": "T-1",
            "message_template_name": "order_update",
            "message_template_language": "en_US",
        },
        received_at=NOW,
        occurred_at=NOW,
    )
    fields.update(overrides)
    return RawEvent(**fields)


def _template(**overrides) -> TemplateRead:
    fields: Dict[str, Any] = dict(
        id="t-1",
        merchant_id="shop",
        channel="whatsapp",
        provider_account_ref="waba-1",
        name="order_update",
        language="en_US",
        provider_template_id="T-1",
        components=[{"type": "BODY", "text": "hi"}],
        status="pending",
        status_updated_at=NOW,
        quality="UNKNOWN",
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return TemplateRead(**fields)


class _FakeAccessor:
    """Stands in for db/accessors/template, recording every webhook write by
    keyword so an accessor that grows an argument turns these red."""

    def __init__(self, row: Optional[TemplateRead] = None, claimed=None, applied=True):
        """Test double."""
        self.row = row
        self.claimed = claimed
        self.applied = applied
        self.calls: List[Tuple[str, Tuple]] = []

    async def get_template_by_provider_id(self, merchant_id, provider_template_id):
        """Test double: the row, found by the provider's own id."""
        self.calls.append(("by_provider_id", (merchant_id, provider_template_id)))
        return self.row

    async def submitting_template_by_natural_key(self, *args):
        """Test double: the unconfirmed claim under this FULL natural key."""
        self.calls.append(("claim", args))
        return self.claimed

    async def apply_status_event(self, *args):
        """Test double."""
        self.calls.append(("status", args))
        return _template(status=args[3]) if self.applied else None

    async def apply_category_event(self, *args):
        """Test double."""
        self.calls.append(("category", args))
        return _template(category=args[3]) if self.applied else None

    async def apply_quality_event(self, *args):
        """Test double."""
        self.calls.append(("quality", args))
        return _template(quality=args[3]) if self.applied else None

    async def resume_submitted_template(self, *args):
        """Test double."""
        self.calls.append(("resume", args))
        return (
            _template(provider_template_id=args[3], status=args[4])
            if self.applied
            else None
        )

    def named(self, call: str) -> Tuple:
        return next(args for name, args in self.calls if name == call)

    @property
    def names(self) -> List[str]:
        return [name for name, _ in self.calls]


def _installation(account: str) -> ConnectorInstallation:
    return ConnectorInstallation(
        id=f"i-{account}",
        merchant_id="shop",
        connector_key="whatsapp",
        external_account_id=account,
        status="healthy",
    )


class _FakeInstallations:
    """Stands in for db/accessors/installation — the doors this merchant
    could have received a letter through."""

    def __init__(self, accounts=("waba-1",)):
        """Test double."""
        self.accounts = [_installation(a) for a in accounts]
        self.asked: List[Tuple] = []

    async def accounts_for_inbound(self, merchant_id, connector_key):
        """Test double."""
        self.asked.append((merchant_id, connector_key))
        return self.accounts


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    accessor: _FakeAccessor,
    accounts=("waba-1",),
) -> _FakeAccessor:
    monkeypatch.setattr(events_module, "template_accessor", accessor)
    monkeypatch.setattr(
        events_module, "installation_accessor", _FakeInstallations(accounts)
    )
    return accessor


async def test_an_approval_flips_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    accessor = _patch(monkeypatch, _FakeAccessor(_template()))
    await consume_template_event(_event(), None, None)
    merchant, template_id, account, status, occurred, reason = accessor.named("status")
    assert (merchant, template_id, status) == ("shop", "t-1", "approved")
    assert occurred == NOW


async def test_the_write_is_scoped_to_the_rows_own_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The letter names no WABA — Meta puts the account in the envelope and
    the bay stores their value verbatim — so the account in the write's WHERE
    comes from the row the provider's id found, never from the payload."""
    accessor = _patch(
        monkeypatch, _FakeAccessor(_template(provider_account_ref="waba-9"))
    )
    await consume_template_event(_event(), None, None)
    assert accessor.named("status")[2] == "waba-9"


async def test_a_rejection_carries_the_providers_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _patch(monkeypatch, _FakeAccessor(_template()))
    await consume_template_event(
        _event(
            payload={
                "event": "REJECTED",
                "message_template_id": "T-1",
                "reason": "INVALID_FORMAT",
            }
        ),
        None,
        None,
    )
    _, _, _, status, _, reason = accessor.named("status")
    assert (status, reason) == ("rejected", "INVALID_FORMAT")


async def test_an_approval_clears_a_stale_rejection_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason described components the provider has since accepted.
    Leaving it attached tells a merchant to fix something nobody objects to.

    Meta's own "NONE" string is spent in the provider face; what has to be
    true here is that the consumer WRITES the absence rather than skipping
    the column."""
    accessor = _patch(monkeypatch, _FakeAccessor(_template(rejection_reason="OLD")))
    await consume_template_event(_event(), None, None)
    assert accessor.named("status")[5] is None


async def test_a_category_letter_is_the_money_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider re-categorising MARKETING as UTILITY changes what the
    merchant is billed; the letter has to land on `category` and leave
    `submitted_category` alone (061 choice 4)."""
    accessor = _patch(monkeypatch, _FakeAccessor(_template()))
    await consume_template_event(
        _event(
            topic=TOPIC_TEMPLATE_CATEGORY,
            payload={
                "message_template_id": "T-1",
                "previous_category": "MARKETING",
                "new_category": "UTILITY",
            },
        ),
        None,
        None,
    )
    assert accessor.names == ["by_provider_id", "category"]
    assert accessor.named("category")[3] == "UTILITY"


async def test_a_quality_letter_writes_the_providers_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _patch(monkeypatch, _FakeAccessor(_template()))
    await consume_template_event(
        _event(
            topic=TOPIC_TEMPLATE_QUALITY,
            payload={"message_template_id": "T-1", "new_quality_score": "RED"},
        ),
        None,
        None,
    )
    assert accessor.named("quality")[3] == "RED"


@pytest.mark.parametrize("topic", [TOPIC_STATUS, TOPIC_INBOUND, TOPIC_ACCOUNT])
async def test_a_letter_that_is_not_ours_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, topic: str
) -> None:
    """Every consumer hears every letter; none is filtered by the registry.
    The topic is what makes one ours."""
    accessor = _patch(monkeypatch, _FakeAccessor(_template()))
    await consume_template_event(_event(topic=topic), None, None)
    assert accessor.calls == []


async def test_a_source_no_connector_serves_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same bay delivers products we do not register templates for."""
    accessor = _patch(monkeypatch, _FakeAccessor(_template()))
    await consume_template_event(_event(source="instagram"), None, None)
    assert accessor.calls == []


async def test_a_letter_naming_no_template_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _patch(monkeypatch, _FakeAccessor(_template()))
    await consume_template_event(_event(payload={"event": "APPROVED"}), None, None)
    assert accessor.calls == []


async def test_a_guard_that_refuses_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older letter arriving behind a newer one is the guard doing its
    job — the consumer returns, the row is stamped processed, nothing
    raises and nothing is retried."""
    _patch(monkeypatch, _FakeAccessor(_template(), applied=False))
    await consume_template_event(_event(), None, None)


# --- the crashed-submit resume ---------------------------------------------


async def test_an_unknown_provider_id_resumes_a_crashed_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """submit() claimed the row, POSTed, and died before recording the
    answer. Meta registered the template, so the claim can never be released
    and the exclusive claim refuses to re-take it — this letter carries the
    id submit never got to write, and stamping it is the only way that
    template becomes usable again."""
    claimed = _template(
        id="t-9",
        provider_template_id=None,
        status="submitting",
        provider_account_ref="waba-1",
    )
    accessor = _patch(monkeypatch, _FakeAccessor(None, claimed=claimed))
    await consume_template_event(_event(), None, None)

    merchant, template_id, account, provider_id, status, occurred, reason = (
        accessor.named("resume")
    )
    assert (template_id, account, provider_id, status) == (
        "t-9",
        "waba-1",
        "T-1",
        "approved",
    )


async def test_the_resume_is_scoped_to_the_account_the_letter_arrived_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim is looked up on the FULL natural key. Without the account
    the lookup can return the right template NAME on the wrong WABA."""
    accessor = _patch(
        monkeypatch,
        _FakeAccessor(None, claimed=_template(provider_template_id=None)),
        accounts=("waba-7",),
    )
    await consume_template_event(_event(), None, None)
    assert accessor.named("claim") == (
        "shop",
        "whatsapp",
        "waba-7",
        "order_update",
        "en_US",
    )


async def test_a_merchant_with_two_accounts_cannot_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect a name-only match hides. One merchant, two WABAs: the
    claim sits on the first while this letter arrives from the second about
    a template we have never seen (registered in Meta's own console). That
    is exactly ONE candidate and exactly the wrong one, and stamping a
    globally unique provider id onto it is undetectable downstream.

    The letter names no account — Meta puts it in the envelope, the bay
    stores their value verbatim, and T13 has no column to carry one — so
    with two doors open it is unknowable and the resume declines."""
    accessor = _patch(
        monkeypatch,
        _FakeAccessor(None, claimed=_template(provider_template_id=None)),
        accounts=("waba-1", "waba-2"),
    )
    await consume_template_event(_event(), None, None)
    assert accessor.names == ["by_provider_id"]


async def test_a_merchant_with_no_open_door_cannot_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every account revoked: nothing could have delivered this letter, so
    there is no account to attribute a claim to."""
    accessor = _patch(
        monkeypatch,
        _FakeAccessor(None, claimed=_template(provider_template_id=None)),
        accounts=(),
    )
    await consume_template_event(_event(), None, None)
    assert accessor.names == ["by_provider_id"]


async def test_nothing_local_to_resume_is_ordinary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template registered outside this registry, or already retired."""
    accessor = _patch(monkeypatch, _FakeAccessor(None, claimed=None))
    await consume_template_event(_event(), None, None)
    assert "resume" not in accessor.names


async def test_a_claim_already_resolved_is_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another letter resumed it first, or the submit that looked crashed
    actually completed. The CAS returns nothing and nothing raises."""
    accessor = _patch(
        monkeypatch,
        _FakeAccessor(
            None,
            claimed=_template(provider_template_id=None, status="submitting"),
            applied=False,
        ),
    )
    await consume_template_event(_event(), None, None)
    assert "resume" in accessor.names


@pytest.mark.parametrize(
    "topic,payload",
    [
        (
            TOPIC_TEMPLATE_CATEGORY,
            {"message_template_id": "T-1", "new_category": "UTILITY"},
        ),
        (
            TOPIC_TEMPLATE_QUALITY,
            {"message_template_id": "T-1", "new_quality_score": "RED"},
        ),
    ],
)
async def test_only_a_status_letter_may_resume(
    monkeypatch: pytest.MonkeyPatch, topic: str, payload: dict
) -> None:
    """The resume records a status as well as an id, and these letters carry
    none. The status letter for the same template repairs the row, and the
    provider re-sends category on every change."""
    accessor = _patch(
        monkeypatch,
        _FakeAccessor(
            None, claimed=_template(provider_template_id=None, status="submitting")
        ),
    )
    await consume_template_event(_event(topic=topic, payload=payload), None, None)
    assert accessor.names == ["by_provider_id"]


# ---------------------------------------------------------------------------
# The statements themselves
# ---------------------------------------------------------------------------

_GUARDED = {
    "status_updated_at": apply_status_event_query("m", "t", "w", "approved", NOW, None)[
        0
    ],
    "category_updated_at": apply_category_event_query("m", "t", "w", "UTILITY", NOW)[0],
    "quality_updated_at": apply_quality_event_query("m", "t", "w", "RED", NOW)[0],
}


@pytest.mark.parametrize("column", sorted(_GUARDED))
def test_every_apply_guards_on_its_own_stamped_column(column: str) -> None:
    """A status ladder would be the wrong test — approved -> pending is a
    legitimate move backwards when a merchant edits an approved template —
    so time is the only honest ordering key, per column (061 choice 6)."""
    sql = _GUARDED[column]
    assert f"date_trunc('second', {column}) <= $5" in sql


@pytest.mark.parametrize("column", sorted(_GUARDED))
def test_a_letter_with_no_time_is_still_applied(column: str) -> None:
    """`occurred_at` is nullable: the bay's timestamp read is total, so a
    provider sending a broken entry.time still files a letter worth
    applying. `column <= NULL` is NULL, which is zero rows — an approval
    lost to a malformed clock."""
    assert "$5::timestamptz IS NULL" in _GUARDED[column]


@pytest.mark.parametrize("column", sorted(_GUARDED))
def test_a_column_with_no_time_yet_is_still_applied(column: str) -> None:
    """`quality_updated_at` has NO writer anywhere before this consumer, so
    the FIRST quality letter on every row in the table meets a NULL. Without
    this branch quality webhooks would never apply, ever, and nothing would
    fail loudly."""
    assert f"OR {column} IS NULL" in _GUARDED[column]


@pytest.mark.parametrize("column", sorted(_GUARDED))
def test_the_guard_compares_at_the_providers_resolution(column: str) -> None:
    """Meta stamps whole unix seconds; our own transitions stamp now(). A
    submit at 10:00:00.7 followed by an approval Meta timestamps 10:00:00
    would compare as older and be refused."""
    assert "date_trunc('second'" in _GUARDED[column]


@pytest.mark.parametrize("column", sorted(_GUARDED))
def test_replaying_a_letter_is_a_no_op_not_a_refusal(column: str) -> None:
    """The consumer's write commits independently of the event row's stamp,
    so a batch that fails after it replays the letter. `<=`, not `<`."""
    assert f"date_trunc('second', {column}) <= $5" in _GUARDED[column]
    assert f"date_trunc('second', {column}) < $5" not in _GUARDED[column]


def test_the_resume_is_guarded_by_the_claim_not_by_time() -> None:
    """The row has never been touched by a provider letter, so there is no
    later state to regress — its status_updated_at is our own claim's
    clock."""
    sql = resume_submitted_template_query("m", "t", "w", "T-1", "approved", NOW, None)[
        0
    ]
    assert "provider_template_id IS NULL" in sql
    assert "AND status = $8" in sql
    assert "date_trunc" not in sql


@pytest.mark.parametrize(
    "sql",
    [
        *_GUARDED.values(),
        resume_submitted_template_query("m", "t", "w", "T-1", "approved", NOW, None)[0],
    ],
)
def test_every_webhook_write_is_merchant_first_and_account_scoped(sql: str) -> None:
    """Tenancy plus account on every write, including the ones that could
    match on the provider's globally unique id alone."""
    assert "WHERE merchant_id = $1" in sql
    assert "AND provider_account_ref = $3" in sql


def test_the_provider_id_read_leads_with_the_merchant() -> None:
    """The letter's merchant was decided by the ingress root's owner lookup;
    re-stating it means a payload naming another tenant's template finds
    nothing rather than something."""
    sql, values = template_by_provider_id_query("shop", "T-1")
    assert "WHERE merchant_id = $1" in sql
    assert values == ["shop", "T-1"]


def test_the_resume_lookup_uses_the_full_natural_key() -> None:
    """All five columns of crm_channel_template_natural_uq, so the unique
    index answers it and there is no "which one" left to guess."""
    sql, values = submitting_template_by_natural_key_query(
        "shop", "whatsapp", "waba-1", "n", "en_US"
    )
    for column in (
        "merchant_id",
        "channel",
        "provider_account_ref",
        "name",
        "language",
    ):
        assert f"{column} = $" in sql
    assert "provider_template_id IS NULL" in sql
    assert values[:5] == ["shop", "whatsapp", "waba-1", "n", "en_US"]
    assert values[-1] == "submitting"


# ---------------------------------------------------------------------------
# The registry word the consumer dispatches on
# ---------------------------------------------------------------------------


def test_a_filed_letters_source_finds_its_connector() -> None:
    """A filed letter keeps only its source — EventIn has no room for the
    channel or the connector key — so the consumer has nothing else to
    dispatch on."""
    spec = connector_for_source("whatsapp")
    assert spec is not None and spec.key == "whatsapp"
    assert connector_for_source("msg91") is None
    assert connector_for_source("") is None


def test_every_connector_names_its_spine_word() -> None:
    """The three words coincide for Meta and diverge for the next provider.
    A spec that forgot one would silently never hear its own letters."""
    for key, spec in CONNECTORS.items():
        assert spec.source, f"connector '{key}' names no source"
