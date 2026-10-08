"""The merchant's bindings (inbox R1, D13–D15, D24–D28): templates go out
from the primary, which moves only within one provider account; Buddy
answers on ONE binding, which holds Buddy's settings — moved whole,
read total and fail-closed (handoff OFF by default), changed only by the
merchant's admins (D14), and the agent must be a chat agent this merchant
may use."""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.connectivity import api as connectivity_api, settings
from app.crm.connectivity.db.queries.binding import (
    buddy_binding_query,
    clear_primary_query,
    lock_channel_bindings_query,
    put_conversation_query,
    set_primary_query,
    take_conversation_query,
    update_conversation_settings_query,
)
from app.crm.connectivity.letters import queued_letter_payload
from app.crm.connectivity.schemas.connector import (
    DEFAULT_CLOSING_MESSAGE,
    ChannelBinding,
    ConversationSettings,
    ConversationSettingsPatch,
)
from app.crm.connectivity.schemas.message import (
    ButtonsBody,
    ImageBody,
    ListBody,
    TextBody,
)
from app.crm.connectivity.status import BINDING_ACTIVE, BINDING_RETIRED
from app.crm.record import catalog
from app.crm.record.extractors.whatsapp.queued import QUEUED_KINDS


def _binding(
    id: str = "b-1",
    installation_id: str = "i-1",
    is_primary: bool = True,
    status: str = "active",
    **capabilities: Any,
) -> ChannelBinding:
    return ChannelBinding(
        id=id,
        merchant_id="shop",
        channel="whatsapp",
        installation_id=installation_id,
        address=f"PHONE_{id}",
        capabilities=capabilities,
        is_primary=is_primary,
        status=status,
    )


# --- reading: total, and fail closed ----------------------------------------


def test_settings_never_saved_have_handoff_off() -> None:
    read = settings.settings_of(_binding(conversation={}))
    assert read.human_handoff is False
    assert read.default_agent_id is None
    assert read.closing_message == DEFAULT_CLOSING_MESSAGE
    assert (read.closing_lead_minutes, read.claim_sla_minutes) == (15, 10)


def test_a_blob_that_does_not_parse_reads_as_the_defaults() -> None:
    """A stored value that no longer validates must never turn handoff ON."""
    broken = _binding(conversation={"human_handoff": True, "closing_lead_minutes": 0})
    assert settings.settings_of(broken).human_handoff is False
    assert settings.settings_of(_binding(conversation="yes")).human_handoff is False


def test_saved_settings_are_read_back() -> None:
    read = settings.settings_of(
        _binding(conversation={"human_handoff": True, "default_agent_id": "t-9"})
    )
    assert read.human_handoff is True and read.default_agent_id == "t-9"


def test_only_buddys_binding_shows_buddys_settings() -> None:
    buddy = settings._read(_binding(is_primary=False, conversation={}))
    other = settings._read(_binding(id="b-2"))
    assert buddy.is_buddy_binding and buddy.conversation == ConversationSettings()
    assert not other.is_buddy_binding and other.conversation is None
    assert other.is_primary and other.installation_id == "i-1"


async def test_no_buddy_binding_reads_the_defaults(monkeypatch) -> None:
    async def none(*args: Any) -> None:
        return None

    monkeypatch.setattr(settings.binding_accessor, "buddy_binding", none)
    assert await settings.conversation_settings("shop", "whatsapp") == (
        ConversationSettings()
    )


async def test_a_binding_that_isnt_buddys_reads_the_defaults(monkeypatch) -> None:
    """Every other binding does nothing in the inbox: no agent, no handoff."""

    async def primary(*args: Any) -> ChannelBinding:
        return _binding()

    monkeypatch.setattr(settings.binding_accessor, "get_binding", primary)
    read = await settings.conversation_settings("shop", "whatsapp", "b-1")
    assert read == ConversationSettings()


# --- writing: one atom over the merchant's bindings ----------------------------


class _Bindings:
    """The merchant's bindings, faked: the atom runs with a sentinel txn and
    every write lands on these rows."""

    TXN = object()

    def __init__(self, *rows: ChannelBinding) -> None:
        self.rows = {row.id: row for row in rows}
        self.changes: List[Dict[str, Any]] = []
        self.letters: List[Dict[str, Any]] = []

    def __getitem__(self, binding_id: str) -> ChannelBinding:
        return self.rows[binding_id]

    def install(self, monkeypatch: pytest.MonkeyPatch, template: Any = None) -> None:
        rows = self.rows

        async def atomically(fn, *args):
            return await fn(self.TXN, *args)

        async def lock(txn, merchant_id, binding_id):
            assert txn is self.TXN
            return list(rows.values()) if binding_id in rows else []

        async def clear_primary(txn, merchant_id, channel):
            for row in rows.values():
                row.is_primary = False

        async def set_primary(txn, merchant_id, binding_id):
            row = rows[binding_id]
            if row.status != BINDING_ACTIVE:
                return None
            row.is_primary = True
            return row

        async def take(txn, merchant_id, channel):
            for row in rows.values():
                if "conversation" in row.capabilities:
                    return row.id, row.capabilities.pop("conversation")
            return None

        async def put(txn, merchant_id, binding_id, conversation):
            assert not any("conversation" in r.capabilities for r in rows.values())
            rows[binding_id].capabilities["conversation"] = dict(conversation)
            return rows[binding_id]

        async def update(txn, merchant_id, binding_id, changes):
            self.changes.append(changes)
            stored = rows[binding_id].capabilities["conversation"]
            stored.update(changes)
            for key in [k for k, v in stored.items() if v is None]:
                del stored[key]
            return rows[binding_id]

        async def get_template(template_id: str):
            return template

        async def merchants(ids: List[str]):
            # "shop" sits under reseller r-1.
            return [SimpleNamespace(id="shop", reseller_id="r-1")], 1

        async def letter(**fields: Any) -> None:
            self.letters.append(fields)

        accessor = settings.binding_accessor
        monkeypatch.setattr(settings, "file_buddy_moved_letter", letter)
        monkeypatch.setattr(settings, "atomically", atomically)
        monkeypatch.setattr(accessor, "lock_channel_bindings", lock)
        monkeypatch.setattr(accessor, "clear_primary", clear_primary)
        monkeypatch.setattr(accessor, "set_primary", set_primary)
        monkeypatch.setattr(accessor, "take_conversation", take)
        monkeypatch.setattr(accessor, "put_conversation", put)
        monkeypatch.setattr(accessor, "update_conversation_settings", update)
        monkeypatch.setattr(settings, "get_template_by_id", get_template)
        monkeypatch.setattr(settings, "get_merchants_by_ids", merchants)


def _template(**overrides: Any) -> SimpleNamespace:
    fields = dict(
        id="t-9",
        merchant_id="shop",
        reseller_id="r-1",
        is_active=True,
        supported_channels=["chat"],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _patch(**fields: Any) -> ConversationSettingsPatch:
    return ConversationSettingsPatch(merchant_id="shop", **fields)


async def test_only_the_fields_sent_are_saved(monkeypatch) -> None:
    bindings = _Bindings(_binding(conversation={"closing_lead_minutes": 20}))
    bindings.install(monkeypatch)
    read = await settings.update_channel_settings(
        "shop", "b-1", _patch(human_handoff=True)
    )
    assert bindings.changes == [{"human_handoff": True}]
    assert read is not None and read.conversation is not None
    assert (
        read.conversation.human_handoff and read.conversation.closing_lead_minutes == 20
    )


async def test_null_restores_a_default_without_checking_an_agent(monkeypatch) -> None:
    bindings = _Bindings(_binding(conversation={"default_agent_id": "t-9"}))
    bindings.install(monkeypatch, template=None)
    await settings.update_channel_settings("shop", "b-1", _patch(default_agent_id=None))
    assert bindings.changes == [{"default_agent_id": None}]
    assert bindings["b-1"].capabilities["conversation"] == {}


@pytest.mark.parametrize(
    ("template", "match"),
    [
        (None, "does not exist"),
        (_template(merchant_id="other"), "another merchant"),
        # Shared (no merchant), but by a reseller this merchant is not under.
        (_template(merchant_id=None, reseller_id="r-2"), "another reseller"),
        (_template(is_active=False), "not active"),
        (_template(supported_channels=["voice"]), "cannot chat"),
    ],
)
async def test_the_agent_must_be_a_chat_agent_this_merchant_may_use(
    monkeypatch, template, match
) -> None:
    bindings = _Bindings(_binding(conversation={}))
    bindings.install(monkeypatch, template=template)
    with pytest.raises(settings.SettingsError, match=match):
        await settings.update_channel_settings(
            "shop", "b-1", _patch(default_agent_id="t-9")
        )
    assert bindings.changes == []


async def test_a_resellers_shared_chat_agent_may_be_the_agent(monkeypatch) -> None:
    bindings = _Bindings(_binding(conversation={}))
    bindings.install(monkeypatch, template=_template(merchant_id=None))
    await settings.update_channel_settings(
        "shop", "b-1", _patch(default_agent_id="t-9")
    )
    assert bindings.changes == [{"default_agent_id": "t-9"}]


async def test_picking_buddys_binding_moves_the_whole_config(monkeypatch) -> None:
    """The settings are one config: they leave the old binding entirely."""
    config = {
        "human_handoff": True,
        "default_agent_id": "t-9",
        "closing_message": "Bye",
    }
    bindings = _Bindings(
        _binding(conversation=dict(config)),
        _binding(id="b-2", is_primary=False),
    )
    bindings.install(monkeypatch)
    read = await settings.update_channel_settings(
        "shop", "b-2", _patch(is_buddy_binding=True)
    )
    assert "conversation" not in bindings["b-1"].capabilities
    assert bindings["b-2"].capabilities["conversation"] == config
    assert read is not None and read.is_buddy_binding and bindings.changes == []


async def test_a_field_sent_to_another_binding_moves_buddy_there_first(
    monkeypatch,
) -> None:
    bindings = _Bindings(
        _binding(conversation={"human_handoff": True}),
        _binding(id="b-2", is_primary=False),
    )
    bindings.install(monkeypatch)
    await settings.update_channel_settings("shop", "b-2", _patch(claim_sla_minutes=30))
    assert "conversation" not in bindings["b-1"].capabilities
    assert bindings["b-2"].capabilities["conversation"] == {
        "human_handoff": True,
        "claim_sla_minutes": 30,
    }


async def test_the_first_buddy_binding_starts_from_the_defaults(monkeypatch) -> None:
    bindings = _Bindings(_binding(), _binding(id="b-2", is_primary=False))
    bindings.install(monkeypatch)
    read = await settings.update_channel_settings(
        "shop", "b-2", _patch(is_buddy_binding=True)
    )
    assert read is not None and read.conversation == ConversationSettings()
    assert bindings.letters == []  # Buddy left no binding: nothing to resolve


async def test_a_move_files_the_letter_and_a_save_in_place_does_not(
    monkeypatch,
) -> None:
    """Conversations resolves the old binding's threads from this letter —
    filed only when Buddy really LEFT a binding."""
    bindings = _Bindings(
        _binding(conversation={}), _binding(id="b-2", is_primary=False)
    )
    bindings.install(monkeypatch)
    await settings.update_channel_settings("shop", "b-2", _patch(is_buddy_binding=True))
    assert bindings.letters == [
        {
            "merchant_id": "shop",
            "channel": "whatsapp",
            "from_binding_id": "b-1",
            "to_binding_id": "b-2",
        }
    ]
    await settings.update_channel_settings("shop", "b-2", _patch(human_handoff=True))
    assert len(bindings.letters) == 1


async def test_buddy_cannot_move_to_a_paused_binding(monkeypatch) -> None:
    bindings = _Bindings(
        _binding(conversation={}),
        _binding(id="b-2", is_primary=False, status="paused"),
    )
    bindings.install(monkeypatch)
    with pytest.raises(settings.SettingsError, match="paused"):
        await settings.update_channel_settings(
            "shop", "b-2", _patch(is_buddy_binding=True)
        )


async def test_the_primary_moves_within_one_account(monkeypatch) -> None:
    bindings = _Bindings(_binding(), _binding(id="b-2", is_primary=False))
    bindings.install(monkeypatch)
    read = await settings.update_channel_settings(
        "shop", "b-2", _patch(is_primary=True)
    )
    assert read is not None and read.is_primary
    assert not bindings["b-1"].is_primary


async def test_the_primary_never_moves_to_another_account(monkeypatch) -> None:
    """Templates are approved per provider account (D27)."""
    bindings = _Bindings(
        _binding(), _binding(id="b-2", installation_id="i-2", is_primary=False)
    )
    bindings.install(monkeypatch)
    with pytest.raises(settings.SettingsError, match="same account"):
        await settings.update_channel_settings("shop", "b-2", _patch(is_primary=True))
    assert bindings["b-1"].is_primary and not bindings["b-2"].is_primary


async def test_with_no_primary_any_active_binding_may_take_over(monkeypatch) -> None:
    bindings = _Bindings(
        _binding(is_primary=False, status="paused"),
        _binding(id="b-2", installation_id="i-2", is_primary=False),
    )
    bindings.install(monkeypatch)
    read = await settings.update_channel_settings(
        "shop", "b-2", _patch(is_primary=True)
    )
    assert read is not None and read.is_primary


async def test_a_binding_that_is_not_the_merchants_is_none(monkeypatch) -> None:
    bindings = _Bindings(_binding(), _binding(id="b-9", status=BINDING_RETIRED))
    bindings.install(monkeypatch)
    assert (
        await settings.update_channel_settings("shop", "b-2", _patch(is_primary=True))
        is None
    )
    assert (
        await settings.update_channel_settings("shop", "b-9", _patch(is_primary=True))
        is None
    )


@pytest.mark.parametrize("flag", ["is_primary", "is_buddy_binding"])
def test_neither_flag_can_be_turned_off(flag) -> None:
    """Pick another binding instead: there is always one of each, or none."""
    with pytest.raises(ValidationError):
        ConversationSettingsPatch.model_validate({"merchant_id": "shop", flag: False})


def test_the_merge_only_touches_buddys_binding() -> None:
    query, values = update_conversation_settings_query(
        "shop", "b-1", {"human_handoff": True}
    )
    assert "jsonb_strip_nulls" in query and "'{conversation}'" in query
    # A stored value that is not an object merges as {} — `||` would
    # otherwise append to an array or fail on a scalar.
    assert "jsonb_typeof(capabilities -> 'conversation') = 'object'" in query
    assert "WHERE merchant_id = $1" in query and "status <> $4" in query
    assert "capabilities ? 'conversation'" in query
    assert values == ["shop", "b-1", '{"human_handoff": true}', BINDING_RETIRED]


def test_the_bindings_atom_locks_in_one_order_and_moves_in_two_steps() -> None:
    query, values = lock_channel_bindings_query("shop", "b-1")
    assert (
        "ORDER BY id" in query and "FOR UPDATE" in query and values == ["shop", "b-1"]
    )
    # Both unique indexes are checked per statement: lower, then raise.
    assert "SET is_primary = false" in clear_primary_query("shop", "whatsapp")[0]
    query, values = set_primary_query("shop", "b-2")
    assert "SET is_primary = true" in query and values[-1] == BINDING_ACTIVE
    assert (
        "capabilities - 'conversation'"
        in take_conversation_query("shop", "whatsapp")[0]
    )
    query, values = put_conversation_query("shop", "b-2", {"human_handoff": True})
    assert "jsonb_set" in query and values[-1] == BINDING_ACTIVE


def test_buddys_binding_is_read_by_the_unique_indexs_predicate() -> None:
    from pathlib import Path

    ddl = (
        Path(__file__).parents[2]
        / "app/database/migrations/083_create_inbox_schema.sql"
    ).read_text()
    assert "WHERE capabilities ? 'conversation'" in ddl
    assert "capabilities ? 'conversation'" in buddy_binding_query("shop", "whatsapp")[0]


# --- the routes: any user reads, only admins write (D14) ----------------------

#: A real binding id (the route takes a UUID), and one no binding has.
BINDING = "5f0c2a8e-3b1d-4c7e-9a6f-2d8e4b1c0a93"
UNKNOWN = "0b7e9c1d-2a3f-4e5d-8c6b-7a9f0e1d2c3b"


def _client(monkeypatch, role: str, merchants=("shop",)) -> TestClient:
    app = FastAPI()
    app.include_router(connectivity_api.router, prefix="/connectors")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role=role, username="u", merchant_ids=list(merchants), reseller_ids=[]
    )
    return TestClient(app)


def _patch_contracts(monkeypatch, updated: Optional[Any] = "ok") -> List[tuple]:
    seen: List[tuple] = []

    async def list_settings(merchant_id: str):
        seen.append(("list", merchant_id))
        return []

    async def update(merchant_id: str, binding_id: str, patch: Any):
        seen.append(("update", merchant_id, binding_id))
        if updated is None:
            return None
        if isinstance(updated, Exception):
            raise updated
        return settings._read(_binding(conversation={}))

    monkeypatch.setattr(
        connectivity_api.contracts, "list_channel_settings", list_settings
    )
    monkeypatch.setattr(connectivity_api.contracts, "update_channel_settings", update)
    return seen


def test_any_user_of_the_merchant_may_read_its_bindings(monkeypatch) -> None:
    seen = _patch_contracts(monkeypatch)
    response = _client(monkeypatch, "user").get(
        "/connectors/bindings", params={"merchant_id": "shop"}
    )
    assert response.status_code == 200 and seen == [("list", "shop")]


@pytest.mark.parametrize(
    ("role", "code"),
    [("user", 403), ("merchant", 200), ("reseller", 200), ("admin", 200)],
)
def test_only_the_merchants_admins_may_change_settings(monkeypatch, role, code) -> None:
    seen = _patch_contracts(monkeypatch)
    response = _client(monkeypatch, role).patch(
        f"/connectors/bindings/{BINDING}",
        json={"merchant_id": "shop", "human_handoff": True},
    )
    assert response.status_code == code
    assert bool(seen) is (code == 200)
    if seen:
        assert seen == [("update", "shop", BINDING)]


def test_an_admin_of_another_merchant_is_still_refused(monkeypatch) -> None:
    seen = _patch_contracts(monkeypatch)
    response = _client(monkeypatch, "merchant", merchants=("other",)).patch(
        f"/connectors/bindings/{BINDING}",
        json={"merchant_id": "shop", "human_handoff": True},
    )
    assert response.status_code == 403 and seen == []


def test_an_unknown_binding_is_404_and_a_bad_agent_is_400(monkeypatch) -> None:
    _patch_contracts(monkeypatch, updated=None)
    client = _client(monkeypatch, "merchant")
    body = {"merchant_id": "shop", "human_handoff": True}
    assert client.patch(f"/connectors/bindings/{UNKNOWN}", json=body).status_code == 404
    _patch_contracts(
        monkeypatch, updated=settings.SettingsError("that agent cannot chat")
    )
    response = client.patch(f"/connectors/bindings/{BINDING}", json=body)
    assert response.status_code == 400 and "cannot chat" in response.text


def test_a_malformed_binding_id_is_refused_before_any_lookup(monkeypatch) -> None:
    seen = _patch_contracts(monkeypatch)
    response = _client(monkeypatch, "merchant").patch(
        "/connectors/bindings/b-1", json={"merchant_id": "shop", "human_handoff": True}
    )
    assert response.status_code == 422 and seen == []


def test_a_timing_out_of_bounds_is_refused_by_the_body_model(monkeypatch) -> None:
    _patch_contracts(monkeypatch)
    response = _client(monkeypatch, "merchant").patch(
        f"/connectors/bindings/{BINDING}",
        json={"merchant_id": "shop", "closing_lead_minutes": 0},
    )
    assert response.status_code == 422


# --- the letters: one set of keys on both sides --------------------------------


def test_the_moved_letter_keys_are_spelled_the_same_on_both_sides() -> None:
    """letters.py writes them, record declares them (rule 12 forbids the
    import) — renamed on one side only, the consumer would read nothing."""
    from pathlib import Path

    declared = {
        f.path.removeprefix("payload.")
        for f in catalog.CATALOG[("whatsapp", "binding.buddy_moved")].fields
    }
    source = Path(__file__).parents[2] / "app/crm/connectivity/letters.py"
    assert all(f'"{key}"' in source.read_text() for key in declared)


def test_the_catalog_declares_the_keys_the_letter_writes() -> None:
    """letters.py writes the payload; record's WhatsApp spec declares it
    (rule 12 forbids the import). A key renamed on one side only would read
    as absent in every timeline."""
    written = set(
        queued_letter_payload(
            message_id="m",
            channel="whatsapp",
            sent_to_address="+91",
            source_kind="agent",
            source_id=None,
            purpose_key="service.x",
            template_id=None,
            variables={},
            body=TextBody(text="hi"),
        )
    )
    declared = {
        field.path.removeprefix("payload.")
        for field in catalog.CATALOG[("whatsapp", "message.queued")].fields
    }
    assert declared <= written
    body_kinds = [
        model.model_fields["kind"].default
        for model in (TextBody, ButtonsBody, ListBody, ImageBody)
    ]
    assert QUEUED_KINDS == ["template"] + body_kinds
