"""templates.py: status-transition rules that live in business logic, not
the DB — the ones canon states explicitly. Accessor/provider calls are
monkeypatched; atomically() is bypassed with a stub since these tests
never touch a real connection."""

from types import SimpleNamespace

import pytest

from app.crm.connectivity import meta_graph as whatsapp, templates
from app.crm.connectivity.templates import TemplateError


async def _fake_atomically(fn, *args, **kwargs):
    return await fn(None, *args, **kwargs)


def _template(**overrides) -> SimpleNamespace:
    base = dict(
        id="tmpl-1",
        merchant_id="m1",
        channel="whatsapp",
        provider_account_ref="waba-1",
        name="order_update",
        language="en_US",
        provider_template_id=None,
        category=None,
        components=[{"type": "BODY", "text": "hi"}],
        status="draft",
        status_updated_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_create_draft_natural_key_collision_on_submitted_raises(
    monkeypatch,
) -> None:
    monkeypatch.setattr(templates, "atomically", _fake_atomically)
    existing = _template(status="pending")

    async def fake_get_by_key(
        txn, merchant_id, channel, provider_account_ref, name, language
    ):
        return existing

    monkeypatch.setattr(
        templates.accessor, "get_template_by_natural_key", fake_get_by_key
    )

    with pytest.raises(TemplateError, match="not draft"):
        await templates.create_draft(
            "m1", "whatsapp", "waba-1", "order_update", "en_US", []
        )


def test_from_meta_lowercases_unknown_words_pass_through() -> None:
    assert templates._from_meta("APPROVED") == "approved"
    assert (
        templates._from_meta("SOME_NEW_STATUS_META_ADDS") == "some_new_status_meta_adds"
    )


def test_from_meta_none_stays_none() -> None:
    assert templates._from_meta(None) is None


def test_claimable_for_submit_excludes_submitting() -> None:
    """Exclusive to draft/rejected — two concurrent submit() calls must
    never both pass the claim. Resume-after-crash goes through the sync
    path (matching a 'submitting' row with no provider_template_id), not
    through re-claiming here."""
    assert templates._CLAIMABLE_FOR_SUBMIT == ["draft", "rejected"]
    assert "submitting" not in templates._CLAIMABLE_FOR_SUBMIT


async def test_resolve_access_token_missing_bundle_key_raises(monkeypatch) -> None:
    async def fake_get_installation_credential(merchant_id, connector_key, ref):
        return {"id": "inst-1", "credential_id": "cred-1", "status": "healthy"}

    async def fake_get_credential_by_id(credential_id, mask):
        return SimpleNamespace(value={"some_other_key": "x"})

    monkeypatch.setattr(
        templates.accessor,
        "get_installation_credential",
        fake_get_installation_credential,
    )
    monkeypatch.setattr(templates, "get_credential_by_id", fake_get_credential_by_id)

    with pytest.raises(TemplateError, match="missing or unreadable"):
        await templates._resolve_access_token("m1", "waba-1")


async def test_resolve_access_token_reads_the_bundle_key(monkeypatch) -> None:
    async def fake_get_installation_credential(merchant_id, connector_key, ref):
        return {"id": "inst-1", "credential_id": "cred-1", "status": "healthy"}

    async def fake_get_credential_by_id(credential_id, mask):
        return SimpleNamespace(value={whatsapp.TOKEN_KEY: "the-token"})

    monkeypatch.setattr(
        templates.accessor,
        "get_installation_credential",
        fake_get_installation_credential,
    )
    monkeypatch.setattr(templates, "get_credential_by_id", fake_get_credential_by_id)

    token = await templates._resolve_access_token("m1", "waba-1")
    assert token == "the-token"


async def test_sync_installation_templates_resumes_when_no_local_match(
    monkeypatch,
) -> None:
    """A Meta template with no row matched by provider_template_id (a
    submit that crashed after Meta accepted it) is resumed by natural key
    instead of being logged and skipped."""
    remote = {
        "id": "meta-tpl-1",
        "status": "APPROVED",
        "category": "MARKETING",
        "name": "order_update",
        "language": "en_US",
    }

    async def fake_list_message_templates(waba_id, access_token):
        return [remote]

    async def fake_sync_template_status(*args, **kwargs):
        return None  # no local row matched by provider_template_id

    resumed_calls = []

    async def fake_resume_submitted_template(*args, **kwargs):
        resumed_calls.append(args)
        return _template(status="approved")

    async def fake_get_credential_by_id(credential_id, mask):
        return SimpleNamespace(value={whatsapp.TOKEN_KEY: "tok"})

    monkeypatch.setattr(templates, "get_credential_by_id", fake_get_credential_by_id)
    monkeypatch.setattr(whatsapp, "list_message_templates", fake_list_message_templates)
    monkeypatch.setattr(
        templates.accessor, "sync_template_status", fake_sync_template_status
    )
    monkeypatch.setattr(
        templates.accessor,
        "resume_submitted_template",
        fake_resume_submitted_template,
    )

    installation = {
        "id": "inst-1",
        "merchant_id": "m1",
        "credential_id": "cred-1",
        "external_account_id": "waba-1",
    }
    await templates.sync_installation_templates(installation)

    assert len(resumed_calls) == 1
    call_args = resumed_calls[0]
    # (merchant_id, waba_id, name, language, provider_template_id, category, status, quality, rejection_reason)
    assert call_args[0] == "m1"
    assert call_args[1] == "waba-1"
    assert call_args[2] == "order_update"
    assert call_args[3] == "en_US"
    assert call_args[4] == "meta-tpl-1"
    assert call_args[6] == "approved"  # normalized from Meta's "APPROVED"
