"""The consent laws (B1), as pure functions — no database needed.

plan_consent is the whole of the business logic: five event types, and what
each one is allowed to do to a stored answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.crm.permission import consent
from app.crm.permission.consent import ConsentPolicy
from app.crm.permission.schemas import (
    ConsentEventIn,
    ConsentEventType,
    ConsentStateRecord,
    ConsentStatus,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
MERCHANT = "m_123"
CUSTOMER = UUID("00000000-0000-0000-0000-000000000777")
MIGRATIONS = Path(__file__).resolve().parents[3] / "app/database/migrations"
CONSENT_DDL = (MIGRATIONS / "055_create_crm_consent.sql").read_text()

# Not the shipped defaults, so a window hardcoded back into the planner fails
# here instead of quietly agreeing with it.
POLICY = ConsentPolicy(
    marketing_grant_days=5, reask_embargo_days=45, pending_confirm_hours=6
)


def _event(
    event_type: ConsentEventType, purpose: str, **overrides: Any
) -> ConsentEventIn:
    body: Dict[str, Any] = {
        "merchant_id": MERCHANT,
        "customer_id": CUSTOMER,
        "address": "+919812340000",
        "event_type": event_type,
        "channel": "whatsapp",
        "purpose_key": purpose,
    }
    body.update(overrides)
    return ConsentEventIn(**body)


def _state(
    purpose: str, status: ConsentStatus, expires_at: Optional[datetime] = None
) -> ConsentStateRecord:
    return ConsentStateRecord(
        merchant_id=MERCHANT,
        customer_id=CUSTOMER,
        channel="whatsapp",
        purpose_key=purpose,
        status=status,
        expires_at=expires_at,
    )


def _plan(
    event_type: ConsentEventType,
    purpose: str,
    existing: Sequence[ConsentStateRecord] = (),
    acted_at: datetime = NOW,
) -> List[consent.StateWrite]:
    """acted_at defaults to the write clock — the live-capture case."""
    return consent.plan_consent(
        _event(event_type, purpose), acted_at, NOW, list(existing), POLICY
    )


# ── the purpose tree ─────────────────────────────────────────────────────────


def test_a_rule_governs_itself_and_everything_beneath_it() -> None:
    assert consent.covers("marketing", "marketing")
    assert consent.covers("marketing", "marketing.promotional.winback")


def test_a_rule_never_governs_upward() -> None:
    assert not consent.covers("marketing.promotional", "marketing")


def test_prefix_collisions_are_not_ancestry() -> None:
    """`marketing` must not swallow a hypothetical `marketingx`."""
    assert not consent.covers("marketing", "marketingx.promotional")


def test_ancestors_run_from_the_root_down_to_the_key() -> None:
    assert consent.ancestors_of("marketing.promotional.winback") == [
        "marketing",
        "marketing.promotional",
        "marketing.promotional.winback",
    ]
    assert consent.ancestors_of("marketing") == ["marketing"]


# ── WITHDRAW ─────────────────────────────────────────────────────────────────


def test_a_withdrawal_cascades_down_the_tree() -> None:
    existing = [
        _state("marketing", ConsentStatus.GRANTED),
        _state("marketing.promotional", ConsentStatus.GRANTED),
        _state("marketing.promotional.winback", ConsentStatus.GRANTED),
    ]
    writes = _plan(ConsentEventType.WITHDRAW, "marketing", existing)
    assert [w.purpose_key for w in writes] == [
        "marketing",
        "marketing.promotional",
        "marketing.promotional.winback",
    ]
    assert {w.status for w in writes} == {ConsentStatus.WITHDRAWN}


def test_a_withdrawal_writes_its_row_even_when_none_existed() -> None:
    """'No row' and 'row saying no' are different answers, and only one of
    them is fixable by asking."""
    writes = _plan(ConsentEventType.WITHDRAW, "marketing.promotional")
    assert [w.purpose_key for w in writes] == ["marketing.promotional"]
    assert writes[0].status is ConsentStatus.WITHDRAWN
    assert writes[0].expires_at == NOW + timedelta(days=POLICY.reask_embargo_days)


def test_a_withdrawal_is_never_refused() -> None:
    """It only ever reduces permission, so nothing blocks recording it."""
    existing = [_state("marketing", ConsentStatus.PROHIBITED)]
    writes = _plan(ConsentEventType.WITHDRAW, "marketing.promotional", existing)
    assert [w.status for w in writes] == [ConsentStatus.WITHDRAWN]


def test_a_cascade_does_not_downgrade_a_prohibition_beneath_it() -> None:
    """A prohibition is permanent; rewriting it would trade a standing bar for
    one that expires in 45 days."""
    existing = [
        _state("marketing", ConsentStatus.GRANTED),
        _state("marketing.promotional", ConsentStatus.PROHIBITED),
    ]
    writes = _plan(ConsentEventType.WITHDRAW, "marketing", existing)
    assert [w.purpose_key for w in writes] == ["marketing"]


# ── GRANT ────────────────────────────────────────────────────────────────────


def test_a_grant_answers_exactly_the_question_asked() -> None:
    """A grant NEVER cascades — anything else manufactures permission."""
    existing = [_state("marketing.promotional", ConsentStatus.WITHDRAWN)]
    writes = _plan(ConsentEventType.GRANT, "marketing", existing)
    assert [w.purpose_key for w in writes] == ["marketing"]


def test_marketing_permission_is_a_parking_ticket() -> None:
    writes = _plan(ConsentEventType.GRANT, "marketing.promotional")
    assert writes[0].expires_at == NOW + timedelta(days=POLICY.marketing_grant_days)


def test_transactional_permission_lives_with_the_account() -> None:
    """A suppressed OTP is a locked-out customer — the opposite of protecting
    her."""
    writes = _plan(ConsentEventType.GRANT, "transactional.auth")
    assert writes[0].expires_at is None


def test_her_own_yes_outranks_her_own_earlier_no() -> None:
    """The embargo protects her from us, not from herself: refusing a START
    would lock her out of a list she is asking to rejoin."""
    existing = [_state("marketing", ConsentStatus.WITHDRAWN, NOW + timedelta(days=44))]
    writes = _plan(ConsentEventType.GRANT, "marketing", existing)
    assert [w.status for w in writes] == [ConsentStatus.GRANTED]


# ── IMPORT ───────────────────────────────────────────────────────────────────


def test_import_grants_when_nothing_refused_it() -> None:
    """The migrating list works on day one."""
    writes = _plan(ConsentEventType.IMPORT, "marketing.promotional")
    assert writes[0].status is ConsentStatus.GRANTED


def test_import_never_resurrects_a_withdrawal_at_the_same_purpose() -> None:
    existing = [_state("marketing.promotional", ConsentStatus.WITHDRAWN)]
    assert _plan(ConsentEventType.IMPORT, "marketing.promotional", existing) == []


def test_import_never_resurrects_a_withdrawal_on_an_ancestor() -> None:
    """The common shape: she withdraws `marketing` owning no leaf rows, then a
    bulk list imports `marketing.promotional`."""
    existing = [_state("marketing", ConsentStatus.WITHDRAWN, NOW + timedelta(days=44))]
    assert _plan(ConsentEventType.IMPORT, "marketing.promotional", existing) == []


def test_import_is_refused_long_after_the_embargo_has_lifted() -> None:
    """The embargo governs asking, not importing: bulk data never overwrites
    an act she performed, however old."""
    stale = [_state("marketing", ConsentStatus.WITHDRAWN, NOW - timedelta(days=400))]
    assert _plan(ConsentEventType.IMPORT, "marketing", stale) == []


# ── REQUEST / CONFIRM ────────────────────────────────────────────────────────


def test_request_opens_a_confirm_window() -> None:
    writes = _plan(ConsentEventType.REQUEST, "marketing.promotional")
    assert writes[0].status is ConsentStatus.PENDING_CONFIRM
    assert writes[0].expires_at == NOW + timedelta(hours=POLICY.pending_confirm_hours)


def test_we_may_not_re_ask_inside_the_embargo() -> None:
    """The stored 'no' survives, which is the point — a pending row written
    over it would erase the refusal the gate reads."""
    existing = [_state("marketing", ConsentStatus.WITHDRAWN, NOW + timedelta(days=44))]
    assert _plan(ConsentEventType.REQUEST, "marketing", existing) == []


def test_a_withdrawal_with_no_clock_is_one_we_never_agreed_to_lift() -> None:
    forever = [_state("marketing", ConsentStatus.WITHDRAWN, None)]
    assert _plan(ConsentEventType.REQUEST, "marketing", forever) == []


def test_confirm_completes_a_live_request() -> None:
    existing = [
        _state(
            "marketing.promotional",
            ConsentStatus.PENDING_CONFIRM,
            NOW + timedelta(hours=1),
        )
    ]
    writes = _plan(ConsentEventType.CONFIRM, "marketing.promotional", existing)
    assert writes[0].status is ConsentStatus.GRANTED


def test_a_confirm_with_nothing_pending_confirms_nothing() -> None:
    """Without a prior REQUEST there was no double opt-in to complete, and the
    ledger could not show one."""
    assert _plan(ConsentEventType.CONFIRM, "marketing.promotional") == []


def test_a_confirm_link_that_died_does_not_still_open_the_door() -> None:
    """A click months later — or a mail scanner replaying the URL — is not a
    fresh yes."""
    dead = [
        _state(
            "marketing.promotional",
            ConsentStatus.PENDING_CONFIRM,
            NOW - timedelta(days=29),
        )
    ]
    assert _plan(ConsentEventType.CONFIRM, "marketing.promotional", dead) == []


def test_a_confirm_needs_the_pending_row_at_its_own_purpose() -> None:
    """A pending question about offers is not one about winbacks."""
    elsewhere = [
        _state("marketing", ConsentStatus.PENDING_CONFIRM, NOW + timedelta(hours=1))
    ]
    assert _plan(ConsentEventType.CONFIRM, "marketing.promotional", elsewhere) == []


# ── the prohibition floor ────────────────────────────────────────────────────


def test_a_prohibition_on_an_ancestor_stops_everything_below_it() -> None:
    existing = [_state("marketing", ConsentStatus.PROHIBITED)]
    for event_type in (
        ConsentEventType.GRANT,
        ConsentEventType.IMPORT,
        ConsentEventType.REQUEST,
        ConsentEventType.CONFIRM,
    ):
        assert _plan(event_type, "marketing.promotional", existing) == [], event_type


# ── what a caller may hand us ────────────────────────────────────────────────


def test_an_unknown_channel_or_purpose_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError, match="channel"):
        _event(ConsentEventType.GRANT, "marketing", channel="telepathy")
    with pytest.raises(ValidationError, match="purpose_key"):
        _event(ConsentEventType.GRANT, "marketing.made_up")


def test_a_naive_timestamp_is_refused() -> None:
    """Bound into a timestamptz it would resolve against the POD's timezone,
    so the same payload would store a different instant on an IST pod."""
    with pytest.raises(ValidationError, match="occurred_at"):
        _event(ConsentEventType.GRANT, "marketing", occurred_at="2026-08-23T12:00:00")


def test_a_timestamp_in_the_future_is_refused() -> None:
    """Every expiry is measured from this field, so an unbounded value defeats
    the expiry policy — a 2099 import grants marketing until 2099."""
    with pytest.raises(ValidationError, match="future"):
        _event(ConsentEventType.IMPORT, "marketing", occurred_at="2099-01-01T00:00:00Z")


def test_a_backdated_import_fails_safe() -> None:
    """A CSV collected two years ago is normal, and the grant it produces has
    already expired."""
    long_ago = NOW - timedelta(days=730)
    writes = _plan(ConsentEventType.IMPORT, "marketing", acted_at=long_ago)
    expires_at = writes[0].expires_at
    assert expires_at is not None and expires_at < NOW


def test_an_address_is_stored_in_one_spelling() -> None:
    """Its job is to be compared against the customer's handle later; two
    spellings of one number are two strangers."""
    spellings = ["+919812340000", "+91 98123 40000", "9812340000", "09812340000"]
    stored = {
        _event(ConsentEventType.GRANT, "marketing", address=s).address
        for s in spellings
    }
    assert stored == {"+919812340000"}

    email = _event(
        ConsentEventType.GRANT,
        "marketing",
        channel="email",
        address=" User@Example.COM ",
    )
    assert email.address == "user@example.com"


def test_an_unusable_address_is_refused_rather_than_stored_wrong() -> None:
    with pytest.raises(ValidationError, match="not a valid whatsapp handle"):
        _event(ConsentEventType.GRANT, "marketing", address="not-a-number")


def test_a_whitespace_only_field_is_not_a_value() -> None:
    """min_length counts characters, so ' ' passes it — and ' ' as a
    merchant_id creates a tenant nothing will ever read from."""
    with pytest.raises(ValidationError):
        _event(ConsentEventType.GRANT, "marketing", merchant_id="   ")


# ── code and schema agree ────────────────────────────────────────────────────


def test_the_closed_status_enums_match_the_migration_checks() -> None:
    """Drift means the code writes a value the table refuses. Only the status
    enums are constrained — law 11 requires a CHECK on exactly that shape."""
    for member in list(ConsentStatus) + list(ConsentEventType):
        assert f"'{member.value}'" in CONSENT_DDL, member


def test_the_vocabularies_have_no_database_check() -> None:
    """Law 11: channel and purpose both grow with the product, so a new value
    is a deploy, not a migration (the 027 scar)."""
    for column in ("channel", "purpose_key"):
        assert f"CHECK ({column}" not in CONSENT_DDL, column


def test_an_unknown_vocabulary_value_is_refused_at_the_front_door() -> None:
    """The table is permissive; the request model is not. Unknown values are
    stopped where a human can be told why."""
    with pytest.raises(ValidationError, match="purpose_key"):
        _event(ConsentEventType.GRANT, "marketing.made_up")
    with pytest.raises(ValidationError, match="channel"):
        _event(ConsentEventType.GRANT, "marketing", channel="rcs")


def test_a_stored_value_outside_the_vocabulary_still_decodes() -> None:
    """The reason the reads are str and not enums. A row written around the
    module — a backfill, or a newer pod after a rollback — must not make every
    later read raise, because one of those reads is how a STOP gets recorded."""
    stranger = ConsentStateRecord(
        merchant_id=MERCHANT,
        customer_id=CUSTOMER,
        channel="rcs",
        purpose_key="marketing.future_thing",
        status=ConsentStatus.GRANTED,
    )
    assert stranger.purpose_key == "marketing.future_thing"


def test_a_withdrawal_still_cascades_over_a_purpose_the_code_does_not_know() -> None:
    """And it must be able to WITHDRAW it, not just read past it."""
    existing = [
        _state("marketing", ConsentStatus.GRANTED),
        _state("marketing.future_thing", ConsentStatus.GRANTED),
    ]
    writes = _plan(ConsentEventType.WITHDRAW, "marketing", existing)
    assert [w.purpose_key for w in writes] == ["marketing", "marketing.future_thing"]
    assert {w.status for w in writes} == {ConsentStatus.WITHDRAWN}


def test_expire_is_not_an_event_type() -> None:
    """Expiry is arithmetic; no human performed it, so no ledger row claims
    one did."""
    assert "EXPIRE" not in {e.value for e in ConsentEventType}


def test_asking_never_takes_away_an_answer_she_already_gave() -> None:
    """A pending row over a live grant reads as 'not granted' at the gate, so
    a re-confirmation campaign would cut off sending to a consented customer.
    Worse one level down: asking about a leaf shadows a grant on its parent."""
    granted = [_state("marketing", ConsentStatus.GRANTED, NOW + timedelta(days=4))]
    assert _plan(ConsentEventType.REQUEST, "marketing", granted) == []
    assert _plan(ConsentEventType.REQUEST, "marketing.promotional", granted) == []


def test_a_replayed_confirm_does_not_renew_the_window() -> None:
    """The first CONFIRM consumed the pending row; a redelivered webhook finds
    a granted row and changes nothing."""
    already = [_state("marketing", ConsentStatus.GRANTED, NOW + timedelta(days=5))]
    assert _plan(ConsentEventType.CONFIRM, "marketing", already) == []


def test_asking_is_allowed_once_a_grant_has_lapsed() -> None:
    """A lapsed grant is a gap, not an answer — re-permission is the point."""
    lapsed = [_state("marketing", ConsentStatus.GRANTED, NOW - timedelta(days=1))]
    writes = _plan(ConsentEventType.REQUEST, "marketing", lapsed)
    assert [w.status for w in writes] == [ConsentStatus.PENDING_CONFIRM]


def test_asking_never_overwrites_a_no_even_after_the_embargo_lifts() -> None:
    """A pending row over a withdrawal deletes the only thing IMPORT reads, so
    the next bulk sync would re-grant someone who opted out."""
    old = [_state("marketing", ConsentStatus.WITHDRAWN, NOW - timedelta(days=400))]
    assert _plan(ConsentEventType.REQUEST, "marketing", old) == []


def test_an_import_cannot_complete_an_unclicked_double_opt_in() -> None:
    """IMPORT carries no act of hers, so it must not finish what a REQUEST
    started — that is the CONFIRM branch's whole job."""
    pending = [
        _state("marketing", ConsentStatus.PENDING_CONFIRM, NOW + timedelta(hours=1))
    ]
    assert _plan(ConsentEventType.IMPORT, "marketing", pending) == []


def test_a_nightly_re_import_cannot_keep_a_grant_alive_forever() -> None:
    """Re-stamping a live grant on every sync means the marketing window never
    expires for any list synced more often than the window."""
    live = [_state("marketing", ConsentStatus.GRANTED, NOW + timedelta(days=2))]
    assert _plan(ConsentEventType.IMPORT, "marketing", live) == []


def test_an_unrecognised_event_type_grants_nothing() -> None:
    """Fail closed: the default arm of a permission decision is never 'grant'.
    A sixth event type refuses until someone decides what it means."""
    event = _event(ConsentEventType.GRANT, "marketing")
    object.__setattr__(event, "event_type", "SHOUT")
    assert consent.plan_consent(event, NOW, NOW, [], POLICY) == []


def test_a_backdated_withdrawal_still_promises_a_full_embargo() -> None:
    """The embargo is a promise WE make now. Measured from a date two years
    ago it would arrive already lifted — and overwrite a live one."""
    writes = _plan(
        ConsentEventType.WITHDRAW, "marketing", acted_at=NOW - timedelta(days=730)
    )
    assert writes[0].expires_at == NOW + timedelta(days=POLICY.reask_embargo_days)


def test_a_backdated_confirm_cannot_revive_a_dead_link() -> None:
    """Liveness is judged at the write moment, not at a caller-supplied one."""
    dead = [
        _state("marketing", ConsentStatus.PENDING_CONFIRM, NOW - timedelta(days=10))
    ]
    assert (
        _plan(
            ConsentEventType.CONFIRM,
            "marketing",
            dead,
            acted_at=NOW - timedelta(days=20),
        )
        == []
    )


def test_a_customer_with_consent_history_cannot_be_hard_deleted() -> None:
    """Both FKs say RESTRICT, and they say it explicitly.

    The ledger answers "prove she agreed" years later, so a DELETE that took it
    with the customer would destroy the evidence. A CASCADE on the state table
    would have been unreachable regardless — the ledger's FK refuses the delete
    first — so it only advertised an erasure path that does not exist. Erasure
    is the soft path: crm_customer.status = 'erased'.
    """
    assert CONSENT_DDL.count("ON DELETE RESTRICT") == 2
    assert "ON DELETE CASCADE" not in CONSENT_DDL


def test_both_tables_key_tenancy_on_the_composite_pair() -> None:
    """A plain id FK accepts any existing uuid, so a wrong merchant_id would
    file a real customer's withdrawal under a tenant that never reads it."""
    # comment lines out: the prose explains why the plain form is wrong, and
    # would otherwise match the assertion it is explaining
    ddl = "\n".join(
        line for line in CONSENT_DDL.splitlines() if not line.lstrip().startswith("--")
    )
    assert ddl.count("REFERENCES crm_customer (merchant_id, id)") == 2
    assert "REFERENCES crm_customer (id)" not in ddl
