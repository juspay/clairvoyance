"""The template webhook consumer, and the declaration that lets its letters
reach it.

Meta decides a template's fate and pushes it; the bay files the letter to
the spine; record's pass has to recognise the letter as merchant-level
rather than quarantining it for naming no customer; and this consumer is the
only thing that ever writes provider-decided template state. There is no
timer anywhere behind it — the periodic sync was removed before it ever ran.

The scars these pin, in the order they would bite:

* a source whose spec says its letters are about a PERSON finds no phone in
  a template review and quarantines EVERY one as ``no_handle`` — with the
  consumer wired, green, and never once called;
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

import app.crm.connectivity.providers.meta.inbound as meta_inbound
import app.crm.record.consumers as record_consumers
import app.crm.record.workers as workers
from app.core.config.static import (
    CRM_TEMPLATE_CLAIM_CRASHED_AFTER_SECONDS,
    CRM_TEMPLATE_EVENT_SKEW_SECONDS,
)
from app.crm.connectivity.connectors import CONNECTORS, connector_for_source
from app.crm.connectivity.db.queries.template import (
    apply_category_event_query,
    apply_quality_event_query,
    apply_status_event_query,
    record_in_place_edit_query,
    resume_submitted_template_query,
    stale_submit_claims_query,
    template_by_natural_key_query,
    template_by_provider_id_query,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.connectivity.schemas.template import TemplateRead
from app.crm.connectivity.templates import events as events_module
from app.crm.connectivity.templates.events import consume_template_event
from app.crm.connectivity.topics import (
    TEMPLATE_TOPICS,
    TOPIC_ACCOUNT,
    TOPIC_INBOUND,
    TOPIC_STATUS,
    TOPIC_TEMPLATE_CATEGORY,
    TOPIC_TEMPLATE_QUALITY,
    TOPIC_TEMPLATE_STATUS,
)
from app.crm.record.db import DbTxn
from app.crm.record.extractors.whatsapp import ENTRIES as WHATSAPP_ENTRIES
from app.crm.record.schemas import ABOUT_CUSTOMER, ABOUT_MERCHANT, RawEvent

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

#: dynamic.py's default for META_GRAPH_TIMEOUT_SECONDS — the fact both
#: template clock dials are bounded by.
_GRAPH_TIMEOUT_DEFAULT = 15.0

#: which parameter carries the skew in each apply (status takes one more
#: value than the other two).
_SKEW_PARAM = {
    "status_updated_at": 8,
    "category_updated_at": 7,
    "quality_updated_at": 7,
}


# ---------------------------------------------------------------------------
# The precondition: these letters must survive the pass to be consumed
# ---------------------------------------------------------------------------
#
# This consumer only ever runs on letters the pass did NOT quarantine, and a
# template review names no person: no phone, no email, nothing resolve()
# could probe. The catalog spec is what says so — ``about="merchant"`` makes
# the pass skip resolve(), stamp a NULL customer (canon T13 col 14) and hand
# the letter to every consumer anyway. Declared "customer" instead, every one
# of them would be quarantined ``no_handle`` BEFORE reaching this module, and
# the feature would be registered, imported, type-checked and green while
# never once being called. So the declaration is pinned here, next to the
# code that depends on it, even though the spec itself is record's.


@pytest.mark.parametrize("topic", [*TEMPLATE_TOPICS, TOPIC_ACCOUNT])
def test_the_topics_this_consumer_reads_are_declared_merchant_level(
    topic: str,
) -> None:
    entry = next((e for e in WHATSAPP_ENTRIES if e.topic == topic), None)
    assert entry is not None, f"{topic} is not declared in the whatsapp catalog"
    assert entry.about == ABOUT_MERCHANT


def test_a_reply_is_the_one_whatsapp_topic_about_a_person() -> None:
    """The boundary this consumer sits beside: an inbound message IS about a
    customer, a receipt is about the MESSAGE we sent (the manifest row
    already carries the person). Neither is ours — the topic is what makes a
    letter ours, and that is the only filter."""
    about = {e.topic: e.about for e in WHATSAPP_ENTRIES}
    assert about[TOPIC_INBOUND] == ABOUT_CUSTOMER
    assert about[TOPIC_STATUS] == ABOUT_MERCHANT


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

    A letter this source declares about a PERSON produces no handle here —
    a template review names nobody — and is quarantined ``no_handle`` BEFORE
    any consumer runs. The consumer would be registered, imported,
    type-checked and green, and Meta's approvals would simply never arrive.
    """
    heard: List[Tuple[str, Optional[str]]] = []

    async def _spy(event, customer_id, handles, variables) -> None:
        heard.append((event.topic, customer_id))

    # The REAL consumer is registered beside the spy, so its arity is pinned
    # by something that RUNS. Registered alone it is checked only by pyrefly
    # on worker_main's registration line, and a signature that drifts from
    # the pass would take down every event of every source at runtime.
    _patch(monkeypatch, _FakeAccessor(_template()))
    monkeypatch.setattr(record_consumers, "_CONSUMERS", [consume_template_event, _spy])
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

    async def stale_submit_claims(self, *args):
        """Test double: crashed claims under this natural key, any account."""
        self.calls.append(("claim", args))
        return [self.claimed] if self.claimed is not None else []

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

    async def connector_accounts(self, merchant_id, connector_key):
        """Test double: every account this merchant has ever held."""
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
        _FakeAccessor(
            None,
            claimed=_template(provider_template_id=None, provider_account_ref="waba-7"),
        ),
        accounts=("waba-7",),
    )
    await consume_template_event(_event(), None, None)
    # The probe is account-FREE (m2: the ordinary "id names no local row"
    # case must not pay an installations lookup); the account decides which
    # returned claim is the letter's.
    assert accessor.named("claim") == ("shop", "whatsapp", "order_update", "en_US")
    assert accessor.named("resume")[2] == "waba-7"


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
    # The claim probe runs first and finds something; the account is what
    # cannot be decided, so nothing is written.
    assert accessor.names == ["by_provider_id", "claim"]


async def test_a_revoked_account_still_counts_against_the_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subtle half of the same defect. Filtering the count to
    non-revoked accounts reads as the careful choice and is the dangerous
    one: the letter was owned at ARRIVAL, this runs at CONSUME. A merchant
    who revokes WABA-A and connects WABA-B then has exactly one live
    account, so a delayed letter about A's template resolves to B — and
    stamps A's globally unique provider id onto B's row. Counting every
    account ever held cannot drift with time."""
    accessor = _patch(
        monkeypatch,
        _FakeAccessor(None, claimed=_template(provider_template_id=None)),
        accounts=("waba-revoked", "waba-live"),
    )
    await consume_template_event(_event(), None, None)
    assert "resume" not in accessor.names


async def test_a_merchant_with_no_account_at_all_cannot_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing could have delivered this letter, so there is no account to
    attribute a claim to."""
    accessor = _patch(
        monkeypatch,
        _FakeAccessor(None, claimed=_template(provider_template_id=None)),
        accounts=(),
    )
    await consume_template_event(_event(), None, None)
    assert "resume" not in accessor.names


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
    assert f"date_trunc('second', {column})" in sql
    assert "<= $5 + make_interval(secs =>" in sql


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
def test_a_clockless_letter_does_not_move_the_ordering_clock(column: str) -> None:
    """The guard is only safe while a letter with no clock leaves the clock
    alone. Stamping now() instead would write a moment in OUR present into
    the column that provider letters are compared against — so the next
    genuine event, whose provider timestamp is necessarily older, fails the
    predicate and is dropped. The worker marks it processed either way, so
    the registry would stay at whatever a broken entry.time left it at,
    permanently and with nothing to replay."""
    assert f"{column} = COALESCE($5::timestamptz, {column})" in _GUARDED[column]
    assert "now()" not in _GUARDED[column]


@pytest.mark.parametrize("column", sorted(_GUARDED))
def test_replaying_a_letter_is_a_no_op_not_a_refusal(column: str) -> None:
    """The consumer's write commits independently of the event row's stamp,
    so a batch that fails after it replays the letter. `<=`, not `<`."""
    assert "<= $5 + make_interval(secs =>" in _GUARDED[column]
    assert "< $5 + make_interval(secs =>" not in _GUARDED[column].replace("<=", "<>")


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


def test_the_resume_probe_only_sees_claims_old_enough_to_be_crashed() -> None:
    """'submitting' with a NULL provider id is ALSO every healthy submit for
    the width of one Graph round-trip — between the claim's commit (which
    must precede the provider call) and record_submission. Resuming inside
    that window stamps the id first, leaves record_submission's CAS matching
    nothing, and shows the merchant a failure for a submission the provider
    accepted."""
    sql, values = stale_submit_claims_query("shop", "whatsapp", "n", "en_US")
    assert "status_updated_at < now() - make_interval(secs => $6)" in sql
    assert "provider_template_id IS NULL" in sql
    assert values == [
        "shop",
        "whatsapp",
        "n",
        "en_US",
        "submitting",
        CRM_TEMPLATE_CLAIM_CRASHED_AFTER_SECONDS,
    ]
    # The relation that matters, not a bare floor: the window must outlast a
    # Graph round trip by an order of magnitude, or it calls a healthy
    # submit crashed.
    assert CRM_TEMPLATE_CLAIM_CRASHED_AFTER_SECONDS >= 10 * _GRAPH_TIMEOUT_DEFAULT


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


def test_the_bay_and_the_registry_agree_on_every_word() -> None:
    """The spine word is declared TWICE — once by the face that files the
    letters, once by the spec that consumes them — and nothing joined them
    up. A rename on either side would not fail: the consumer would look up a
    source no spec claims, return at DEBUG as "ordinary", and every template
    webhook would be dropped with the whole suite green. So the two are
    compared here, along with the other two words the letter carries."""
    for words in meta_inbound._WORDS_FOR_OBJECT.values():
        spec = connector_for_source(words.source)
        assert spec is not None, f"the bay files '{words.source}', no spec claims it"
        assert spec.key == words.connector_key
        assert spec.channel == words.channel


def test_the_registry_natural_key_is_the_unique_index() -> None:
    """The five columns of crm_channel_template_natural_uq, spelled in more
    than one builder. Pinned here so a migration that changes the key cannot
    leave a lookup quietly reading a different one."""
    sql, values = template_by_natural_key_query("m", "whatsapp", "w", "n", "en_US")
    for column in (
        "merchant_id",
        "channel",
        "provider_account_ref",
        "name",
        "language",
    ):
        assert f"{column} = $" in sql
    assert values == ["m", "whatsapp", "w", "n", "en_US"]


def test_every_connector_names_its_spine_word() -> None:
    """The three words coincide for Meta and diverge for the next provider.
    A spec that forgot one would silently never hear its own letters."""
    for key, spec in CONNECTORS.items():
        assert spec.source, f"connector '{key}' names no source"


# ---------------------------------------------------------------------------
# The tombstone, and the writer this consumer races
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", sorted(_GUARDED.values()))
def test_no_webhook_can_resurrect_a_retired_template(sql: str) -> None:
    """retire keeps ``provider_template_id`` — crm_message rows name the
    template and "what did we send in August" must stay answerable — so a
    webhook naming that id still finds the row long after the merchant
    withdrew it. The provider-side delete is best-effort, so Meta may go on
    deciding about a template it never removed. Without a tombstone clause a
    redelivered APPROVED flips 'deleted' back to 'approved' and the send
    door, which filters on nothing else, resumes sending it. The time guard
    does not cover this: a letter with no clock skips that branch entirely.
    """
    assert "AND status <> $" in sql


@pytest.mark.parametrize("sql", sorted(_GUARDED.values()))
def test_the_tombstone_word_is_bound_not_spliced(sql: str) -> None:
    """A status word in the SQL TEXT would be the one f-string'd value in
    this module (CLAUDE.md: any value via f-string into SQL is a blocker)."""
    assert "'deleted'" not in sql


def test_an_edit_survives_the_consumer_winning_the_race() -> None:
    """The consumer is a second writer on this row and often wins: the
    provider accepts an edit, sends its status letter, and the worker
    applies 'pending' before edit()'s own CAS runs. On a bare equality the
    CAS matches nothing, edit() raises "reload and try again" — advice that
    cannot work, since 'pending' is not in TEMPLATE_IN_PLACE_EDIT — and the
    components the provider is ALREADY reviewing are never recorded, leaving
    the row approved over stale components with no sync left to heal it."""
    sql, _ = record_in_place_edit_query("m", "t", "[]", "pending", "approved")
    assert "AND status IN ($4, $5)" in sql


def test_accepting_the_destination_status_keeps_the_tombstone_shut() -> None:
    """The widened CAS must not reopen what it was written to close: a
    concurrent retire sets 'deleted', which is neither the status the edit
    is moving to nor the one it was authorised against."""
    _, values = record_in_place_edit_query("m", "t", "[]", "pending", "approved")
    assert "deleted" not in values


# ---------------------------------------------------------------------------
# The skew: our own clock sits in the column the provider is ordered against
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", sorted(_GUARDED))
def test_a_letter_just_behind_our_own_stamp_still_applies(column: str) -> None:
    """Two of OUR transitions stamp this column with now() at COMMIT, while
    the provider stamps whole seconds at the moment it DECIDED — a round
    trip earlier. A template approved inside that same second therefore
    yields a letter legitimately BEHIND our stamp. A 300ms Graph call
    crosses a second boundary about a third of the time, and the WhatsApp
    edit face reports 'pending' unconditionally — so that refused letter was
    the only source of truth, and the row would sit pending forever."""
    assert f"make_interval(secs => ${_SKEW_PARAM[column]})" in _GUARDED[column]
    assert CRM_TEMPLATE_EVENT_SKEW_SECONDS >= _GRAPH_TIMEOUT_DEFAULT / 2


def test_the_skew_is_narrower_than_a_redelivery() -> None:
    """The tolerance buys the round trip and nothing like a retry window: a
    letter minutes late must still be refused, or the guard stops guarding.
    It is also the exact cost being accepted — two PROVIDER letters less
    than this apart can reorder."""
    assert CRM_TEMPLATE_EVENT_SKEW_SECONDS <= 30


def test_both_template_clock_dials_live_in_config() -> None:
    """Behaviour constants belong beside the other CRM dials, once — and
    these two are mutually constrained with the Graph timeout they are both
    derived from, which a constant in a queries file cannot express."""
    import app.crm.connectivity.db.queries.template as builders

    assert not hasattr(builders, "CLAIM_CRASHED_AFTER_SECONDS")
