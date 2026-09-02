"""The template registry's lifecycle, and the provider face under it.

Every test here pins a transition, because transitions are where this
registry has been wrong: a rejected template that could neither be
resubmitted nor edited, a claim that was never released, a provider's
UPPERCASE status stored beside our lowercase rules, and a delete that took
every language variant with it.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

from app.crm.connectivity import (
    accounts as accounts_module,
    templates as templates_module,
)
from app.crm.connectivity.providers.meta import graph as graph_module
from app.crm.connectivity.providers.whatsapp.templates import (
    WhatsappTemplateError,
    WhatsappTemplates,
    _from_meta,
)
from app.crm.connectivity.schemas import (
    ConnectorInstallation,
    CredentialBundle,
    TemplateDraft,
    TemplateRead,
)
from app.crm.connectivity.templates import (
    TemplateError,
    TemplateNotFoundError,
    create_draft,
    edit,
    retire,
    submit,
)
from scripts.check_crm_boundaries import TABLE_OWNERS

# --- the provider face ------------------------------------------------------


def test_meta_shouting_becomes_the_canon_dictionary() -> None:
    """Meta answers APPROVED; every rule in templates.py compares 'approved'.

    Storing their casing verbatim once meant a registry holding 'APPROVED',
    'PENDING', 'submitting' and 'deleted' side by side, ?status=approved
    returning nothing for an approved template, and edits refused with "is
    'APPROVED' — edit not supported from this status".
    """
    assert _from_meta("APPROVED") == "approved"
    assert _from_meta("PENDING_DELETION") == "pending_deletion"
    assert _from_meta(None) is None
    assert _from_meta("  ") is None


def _graph(monkeypatch, handler) -> List[httpx.Request]:
    """Point the shared Graph transport at a canned responder."""
    seen: List[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        """Record the outgoing request, then delegate to the handler."""
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        graph_module,
        "create_http_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(_capture)),
    )
    return seen


def _bundle() -> CredentialBundle:
    return CredentialBundle(values={"system_user_token": "tok"})


async def test_a_submitted_template_keeps_metas_own_verdict(monkeypatch) -> None:
    """Their id, their category, their status — normalised, not reinterpreted.

    The category especially: a provider may assign a different one from the
    one requested, and theirs is what the merchant is billed at.
    """
    _graph(
        monkeypatch,
        lambda _r: httpx.Response(
            200, json={"id": "T-1", "status": "PENDING", "category": "UTILITY"}
        ),
    )
    state = await WhatsappTemplates().submit(
        _bundle(),
        "waba-1",
        TemplateDraft(
            name="order_update", language="en_US", category="MARKETING", components=[]
        ),
    )
    assert state.provider_template_id == "T-1"
    assert state.status == "pending"
    assert state.category == "UTILITY"


async def test_a_submission_without_an_id_is_a_failure(monkeypatch) -> None:
    """Without the provider's id no webhook can ever be matched to this row,
    so a 200 that omits it is not a success."""
    _graph(monkeypatch, lambda _r: httpx.Response(200, json={"status": "PENDING"}))
    with pytest.raises(Exception):
        await WhatsappTemplates().submit(
            _bundle(),
            "waba-1",
            TemplateDraft(
                name="n", language="en_US", category="UTILITY", components=[]
            ),
        )


async def test_retiring_one_language_carries_the_provider_id(monkeypatch) -> None:
    """Meta's delete takes a NAME, and a name alone deletes every language
    variant of it. Retiring order_update/en_US would silently delete
    order_update/hi_IN on Meta while our hi_IN row still read 'approved' —
    and the first send on it would fail at Meta with 132001."""
    seen = _graph(monkeypatch, lambda _r: httpx.Response(200, json={"success": True}))
    await WhatsappTemplates().retire(
        _bundle(), "waba-1", "T-1", "order_update", "en_US"
    )
    url = str(seen[0].url)
    assert "hsm_id=T-1" in url
    assert "name=order_update" in url


def test_a_status_webhook_normalises_into_the_registry() -> None:
    face = WhatsappTemplates()
    state = face.normalize_event(
        "template.status",
        {
            "message_template_id": "T-1",
            "message_template_name": "order_update",
            "message_template_language": "en_US",
            "event": "APPROVED",
            "reason": "NONE",
        },
    )
    assert state is not None
    assert state.status == "approved"
    # Meta sends the literal string "NONE" when there is no reason; storing
    # it would show a merchant the word NONE as their rejection reason.
    assert state.rejection_reason is None


def test_a_rejection_keeps_the_providers_own_words() -> None:
    state = WhatsappTemplates().normalize_event(
        "template.status",
        {
            "message_template_id": "T-1",
            "event": "REJECTED",
            "reason": "INVALID_FORMAT",
        },
    )
    assert state is not None and state.rejection_reason == "INVALID_FORMAT"


def test_a_category_webhook_carries_the_new_category() -> None:
    state = WhatsappTemplates().normalize_event(
        "template.category",
        {
            "message_template_id": "T-1",
            "previous_category": "MARKETING",
            "new_category": "UTILITY",
        },
    )
    assert state is not None and state.category == "UTILITY"


def test_a_letter_with_nothing_to_apply_is_none() -> None:
    assert WhatsappTemplates().normalize_event("template.status", {}) is None
    assert WhatsappTemplates().normalize_event("something.else", {}) is None


# --- the lifecycle ----------------------------------------------------------


def _template(**overrides) -> TemplateRead:
    fields = dict(
        id="t-1",
        merchant_id="shop",
        channel="whatsapp",
        provider_account_ref="waba-1",
        name="order_update",
        language="en_US",
        components=[{"type": "BODY", "text": "hi"}],
        status="draft",
        status_updated_at=datetime.now(timezone.utc),
        quality="UNKNOWN",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return TemplateRead(**fields)


class _FakeTemplateAccessor:
    """Stands in for db/accessors/template."""

    def __init__(self, template: Optional[TemplateRead] = None, claim=True):
        """Test double."""
        self.template = template
        self.claim = claim
        self.calls: List[str] = []
        self.released = False

    async def get_template(self, merchant_id, template_id):
        """Test double: the seeded row."""
        return self.template

    async def get_template_by_natural_key(self, txn, *args):
        """Test double: no existing row unless one was seeded."""
        return self.template

    async def insert_template_draft(self, txn, *args):
        """Test double: record the insert."""
        self.calls.append("insert")
        return _template()

    async def update_draft_components(self, txn, merchant_id, template_id, components):
        """Test double: record the draft edit."""
        self.calls.append("update_draft")
        return _template(components=components)

    async def claim_for_submit(self, txn, merchant_id, template_id):
        """Test double: the exclusive claim, or nothing."""
        self.calls.append("claim")
        return _template(status="submitting") if self.claim else None

    async def release_submit_claim(self, merchant_id, template_id):
        """Test double: record that the claim was handed back."""
        self.released = True
        return _template(status="draft")

    async def record_submission(self, txn, *args):
        """Test double: record the provider's verdict."""
        self.calls.append("record_submission")
        return _template(status="pending", provider_template_id="T-1")

    async def record_in_place_edit(
        self, conn, merchant_id, template_id, comps, status, expected_status
    ):
        """Test double: the conditional write — records what it was
        authorised against as well as what it sets."""
        self.calls.append("record_in_place_edit")
        self.edited_from = expected_status
        return _template(status=status, components=comps)

    async def retire_template(self, txn, merchant_id, template_id):
        """Test double: record the retirement."""
        self.calls.append("retire")
        return _template(status="deleted")


class _FakeInstallationAccessor:
    """Stands in for db/accessors/installation."""

    def __init__(self, installation=None):
        """Test double."""
        self.installation = installation

    async def get_installation_by_account(self, merchant_id, key, account_ref):
        """Test double: the seeded door for that provider account."""
        return self.installation


class _StubTemplates:
    """A TemplateProvider with scripted behaviour."""

    def __init__(self, edits_in_place=True, submit_error=None):
        """Test double."""
        self.edits_in_place = edits_in_place
        self.submit_error = submit_error
        self.retired: List[tuple] = []

    async def submit(self, bundle, account_ref, draft):
        """Test double: the provider's answer, or its refusal."""
        if self.submit_error:
            raise self.submit_error
        from app.crm.connectivity.schemas import ProviderTemplateState

        return ProviderTemplateState(
            provider_template_id="T-1", status="pending", category=draft.category
        )

    async def edit(self, bundle, account_ref, provider_template_id, components):
        """Test double: an in-place edit sends it back for review."""
        from app.crm.connectivity.schemas import ProviderTemplateState

        return ProviderTemplateState(
            provider_template_id=provider_template_id, status="pending"
        )

    async def retire(self, bundle, account_ref, provider_template_id, name, language):
        """Test double: record the withdrawal."""
        self.retired.append((provider_template_id, name, language))


class _Credential:
    is_active = True
    value = {"system_user_token": "tok"}


def _healthy(**overrides) -> ConnectorInstallation:
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


def _patch(monkeypatch, *, templates=None, installation=None, provider=None):
    """Wire templates.py to doubles."""
    accessor = templates or _FakeTemplateAccessor()
    installations = _FakeInstallationAccessor(
        _healthy() if installation is None else installation
    )
    face = provider or _StubTemplates()

    class _Spec:
        key = "whatsapp"
        channel = "whatsapp"

    spec = _Spec()
    spec.templates = face  # type: ignore[attr-defined]

    async def _atomically(fn, *args):
        """Test double: run the atom body against a None handle."""
        return await fn(None, *args)

    async def _credential(credential_id, mask=True, raise_errors=False):
        """Test double: the vault read."""
        return _Credential()

    monkeypatch.setattr(templates_module, "template_accessor", accessor)
    monkeypatch.setattr(accounts_module, "installation_accessor", installations)
    monkeypatch.setattr(templates_module, "atomically", _atomically)
    monkeypatch.setattr(accounts_module, "get_credential_by_id", _credential)
    monkeypatch.setattr(templates_module, "connector_for_channel", lambda c: spec)
    return accessor, face


async def test_a_draft_on_an_unregistered_channel_is_refused(monkeypatch) -> None:
    """Fail closed at create. A draft nothing can ever submit is worse than a
    refusal — it looks like progress."""
    _patch(monkeypatch)
    monkeypatch.setattr(templates_module, "connector_for_channel", lambda c: None)
    with pytest.raises(TemplateError, match="no connector"):
        await create_draft("shop", "telegram", "acct", "n", "en_US", [])


async def test_a_draft_on_an_unconnected_account_is_refused(monkeypatch) -> None:
    """A provider_account_ref naming no healthy connection has no credential
    to submit with, so it is refused here rather than at submit."""
    _patch(monkeypatch, installation=None)
    monkeypatch.setattr(
        accounts_module, "installation_accessor", _FakeInstallationAccessor(None)
    )
    with pytest.raises(TemplateError, match="no connected account"):
        await create_draft("shop", "whatsapp", "waba-9", "n", "en_US", [])


async def test_a_draft_on_a_degraded_connection_is_refused(monkeypatch) -> None:
    _patch(monkeypatch, installation=_healthy(status="degraded"))
    with pytest.raises(TemplateError, match="degraded"):
        await create_draft("shop", "whatsapp", "waba-1", "n", "en_US", [])


async def test_recreating_a_submitted_draft_does_not_overwrite_it(
    monkeypatch,
) -> None:
    """Idempotency does not extend to replacing the components a provider is
    currently reviewing."""
    _patch(monkeypatch, templates=_FakeTemplateAccessor(_template(status="pending")))
    with pytest.raises(TemplateError, match="already exists"):
        await create_draft("shop", "whatsapp", "waba-1", "order_update", "en_US", [])


async def test_a_rejected_template_cannot_be_resubmitted(monkeypatch) -> None:
    """The provider still holds that name, so a re-create answers "name
    already exists" — the refusal names the way out instead."""
    _patch(monkeypatch, templates=_FakeTemplateAccessor(_template(status="rejected")))
    with pytest.raises(TemplateError, match="edit it"):
        await submit("shop", "t-1", "UTILITY")


async def test_a_rejected_template_is_corrected_by_editing_it(monkeypatch) -> None:
    """The edit IS the resubmission: same row, back to pending."""
    accessor, _ = _patch(
        monkeypatch,
        templates=_FakeTemplateAccessor(
            _template(status="rejected", provider_template_id="T-1")
        ),
    )
    updated = await edit("shop", "t-1", [{"type": "BODY", "text": "fixed"}])
    assert updated.status == "pending"
    assert "record_in_place_edit" in accessor.calls


async def test_an_approved_template_edited_goes_back_to_pending(monkeypatch) -> None:
    """canon T23's one explicit transition rule."""
    accessor, _ = _patch(
        monkeypatch,
        templates=_FakeTemplateAccessor(
            _template(status="approved", provider_template_id="T-1")
        ),
    )
    updated = await edit("shop", "t-1", [{"type": "BODY", "text": "new"}])
    assert updated.status == "pending"


async def test_a_provider_without_in_place_edits_says_so(monkeypatch) -> None:
    """Meta's behaviour is not universal: SMS-DLT re-registers under a new
    id. The refusal is honest rather than repeating Meta's rule."""
    _patch(
        monkeypatch,
        templates=_FakeTemplateAccessor(
            _template(status="approved", provider_template_id="T-1")
        ),
        provider=_StubTemplates(edits_in_place=False),
    )
    with pytest.raises(TemplateError, match="retire"):
        await edit("shop", "t-1", [])


async def test_a_refused_submission_gives_the_draft_back(monkeypatch) -> None:
    """The claim is exclusive and 'submitting' is not re-claimable, so a
    claim left standing after a failure is permanent: that template could
    never be submitted or edited again."""
    accessor, _ = _patch(
        monkeypatch,
        templates=_FakeTemplateAccessor(_template(status="draft")),
        provider=_StubTemplates(submit_error=WhatsappTemplateError("bad components")),
    )
    with pytest.raises(TemplateError, match="bad components"):
        await submit("shop", "t-1", "UTILITY")
    assert accessor.released is True


async def test_a_concurrent_submit_loses_the_claim(monkeypatch) -> None:
    """Two requests that both read 'draft' must not both reach the provider:
    it refuses the second by name, but only after we have fired it."""
    _patch(
        monkeypatch,
        templates=_FakeTemplateAccessor(_template(status="draft"), claim=False),
    )
    with pytest.raises(TemplateError, match="already being submitted"):
        await submit("shop", "t-1", "UTILITY")


async def test_a_successful_submit_records_the_providers_verdict(
    monkeypatch,
) -> None:
    accessor, _ = _patch(
        monkeypatch, templates=_FakeTemplateAccessor(_template(status="draft"))
    )
    updated = await submit("shop", "t-1", "UTILITY")
    assert updated.status == "pending"
    assert updated.provider_template_id == "T-1"
    assert accessor.calls == ["claim", "record_submission"]


async def test_retiring_still_happens_when_the_provider_is_down(
    monkeypatch,
) -> None:
    """Local retirement is what the send path reads, so it is the one that
    has to happen — a provider outage must not leave a merchant unable to
    stop using a template."""

    class _Broken(_StubTemplates):
        async def retire(
            self, bundle, account_ref, provider_template_id, name, language
        ):
            """Test double: the provider is unreachable."""
            raise RuntimeError("meta down")

    accessor, _ = _patch(
        monkeypatch,
        templates=_FakeTemplateAccessor(
            _template(status="approved", provider_template_id="T-1")
        ),
        provider=_Broken(),
    )
    updated = await retire("shop", "t-1")
    assert updated.status == "deleted"


async def test_an_unknown_template_is_a_not_found(monkeypatch) -> None:
    """Its own exception type, so routes answer 404 rather than 400."""
    _patch(monkeypatch, templates=_FakeTemplateAccessor(None))
    with pytest.raises(TemplateNotFoundError):
        await submit("shop", "t-999", "UTILITY")
    with pytest.raises(TemplateNotFoundError):
        await edit("shop", "t-999", [])
    with pytest.raises(TemplateNotFoundError):
        await retire("shop", "t-999")


# --- the table itself -------------------------------------------------------

TEMPLATE_MIGRATION = Path("app/database/migrations/061_create_crm_channel_template.sql")


def _template_ddl() -> str:
    """The migration with comment prose stripped: a structural assertion that
    passes on the paragraph explaining a choice proves nothing."""
    return "\n".join(
        line
        for line in TEMPLATE_MIGRATION.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def test_the_table_is_owned_by_connectivity() -> None:
    assert TABLE_OWNERS["crm_channel_template"] == "connectivity"


def _index_columns(index_name: str) -> str:
    """The parenthesised column list of one index, and nothing else.

    Sliced rather than matched on the whole statement, because the index NAME
    contains the word 'channel' and would satisfy an assertion meant for the
    columns.
    """
    ddl = _template_ddl()
    body = ddl[ddl.index(index_name) :]
    return body[body.index("(") + 1 : body.index(")")]


def test_the_natural_key_includes_the_provider_account() -> None:
    """canon T23 seals four columns; this index carries a fifth.

    A merchant may hold two accounts on one channel, and the same
    name+language registered in both are two different templates with two
    different provider ids. Without the account in the key the second one
    collides with the first and simply cannot be created.
    """
    columns = [
        c.strip() for c in _index_columns("crm_channel_template_natural_uq").split(",")
    ]
    assert columns == [
        "merchant_id",
        "channel",
        "provider_account_ref",
        "name",
        "language",
    ]
    # merchant_id leads, as every unique index on a crm_ table must.
    assert columns[0] == "merchant_id"


def test_the_provider_id_is_unique_alone_and_partial() -> None:
    """The same exception crm_message_provider_id_uq earns: the tenancy law
    protects OUR identifiers, and this one is the PROVIDER's — globally
    unique, and the only thing a webhook names."""
    assert _index_columns("crm_channel_template_provider_id_uq").strip() == (
        "provider_template_id"
    )
    ddl = _template_ddl()
    index = ddl[ddl.index("crm_channel_template_provider_id_uq") :]
    assert "WHERE provider_template_id IS NOT NULL" in index[: index.index(";")]


def test_no_check_constrains_provider_vocabulary() -> None:
    """The 027 scar: a CHECK turns "support a new status" into a migration.
    Providers rename categories and add statuses we have not seen."""
    ddl = _template_ddl()
    assert "CHECK" not in ddl.upper()


def test_the_touch_trigger_is_present() -> None:
    assert "crm_channel_template_touch" in _template_ddl()


def test_a_malformed_components_blob_decodes_to_empty_rather_than_raising() -> None:
    """The totality promise has to hold at the layer that needs objects.

    jsonb_list makes the COLUMN total — a scalar or an object becomes []. But
    TemplateRead types components as objects, so a stored `[1, 2]` would sail
    through the helper and raise in pydantic one line later, stranding every
    row decoded beside it in the same batch. The decoder filters, so the
    guarantee survives the layer above it.
    """
    from app.crm.connectivity.db.decoders.template import decode_template

    row = {
        "id": "t-1",
        "merchant_id": "shop",
        "channel": "whatsapp",
        "provider_account_ref": "waba-1",
        "name": "n",
        "language": "en_US",
        "provider_template_id": None,
        "category": None,
        "submitted_category": None,
        "category_updated_at": None,
        "components": '[1, 2, {"type": "BODY"}]',
        "status": "draft",
        "status_updated_at": datetime.now(timezone.utc),
        "rejection_reason": None,
        "quality": "UNKNOWN",
        "quality_updated_at": None,
        "last_synced_at": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    decoded = decode_template(row)
    assert decoded.components == [{"type": "BODY"}], "non-objects must be dropped"


async def test_a_bug_in_the_face_does_not_reach_the_caller(monkeypatch) -> None:
    """A declared refusal is the merchant's own template being described, so
    it passes through. Anything else is a bug, and a bug's text is an
    internal detail — a KeyError('provider_row_id') must not become a 400
    whose body reads 'provider_row_id'. Same split onboarding makes with
    ConnectorHandshakeError."""
    accessor, _ = _patch(
        monkeypatch,
        templates=_FakeTemplateAccessor(_template(status="draft")),
        provider=_StubTemplates(submit_error=KeyError("provider_row_id")),
    )
    with pytest.raises(TemplateError) as caught:
        await submit("shop", "t-1", "UTILITY")
    assert "provider_row_id" not in str(caught.value)
    assert str(caught.value) == "could not complete the template operation"
    # and the claim is still released, so the draft is retryable
    assert accessor.released is True


async def test_an_edit_refuses_when_the_row_moved_underneath_it(
    monkeypatch,
) -> None:
    """The provider call happens outside any transaction, so a concurrent
    retire() can land between the pre-read and the write. Without the CAS
    this update would put fresh components and 'pending' over a row the
    merchant just deleted — resurrecting a withdrawn template."""

    accessor = _FakeTemplateAccessor(
        _template(status="approved", provider_template_id="T-1")
    )

    async def _row_moved(*args, **kwargs):
        """Test double: the CAS matched nothing — something else moved the
        row while the provider call was in flight."""
        return None

    accessor.record_in_place_edit = _row_moved  # type: ignore[method-assign]
    _patch(monkeypatch, templates=accessor)
    with pytest.raises(TemplateError, match="changed while it was being edited"):
        await edit("shop", "t-1", [{"type": "BODY", "text": "new"}])


def test_the_in_place_edit_is_conditional_on_the_status_it_read() -> None:
    """The guard lives in SQL, where two callers cannot interleave around it."""
    from app.crm.connectivity.db.queries.template import record_in_place_edit_query

    sql, values = record_in_place_edit_query("shop", "t-1", "[]", "pending", "approved")
    assert "AND status = $5" in sql
    assert values[-1] == "approved"
