"""The merchant's bindings (inbox R1, D13–D15, D24–D28): templates go out
from the primary, which moves only within one provider account; Buddy
answers on ONE binding, which holds Buddy's settings — moved whole,
read total and fail-closed (handoff OFF by default), changed only by the
merchant's admins (D14), and the agent must be a chat agent this merchant
may use."""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.connectivity import api as connectivity_api
from app.crm.connectivity.accounts import channel_settings
from app.crm.connectivity.db.queries.binding import (
    buddy_binding_query,
    lock_channel_bindings_query,
    update_conversation_channel_query,
    update_template_message_channel_query,
)
from app.crm.connectivity.schemas.connector import ChannelBinding
from app.crm.connectivity.schemas.conversation import (
    DEFAULT_CLOSING_MESSAGE,
    ConversationSettings,
    ConversationSettingsPatch,
)
from app.crm.connectivity.vocabulary.status import BINDING_RETIRED
from app.crm.record import catalog


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
    read = channel_settings.settings_of(_binding(conversation={}))
    assert read.human_handoff is False
    assert read.default_agent_id is None
    assert read.closing_message == DEFAULT_CLOSING_MESSAGE
    assert (read.closing_lead_minutes, read.claim_sla_minutes) == (15, 10)


def test_a_blob_that_does_not_parse_reads_as_the_defaults() -> None:
    """A stored value that no longer validates must never turn handoff ON."""
    broken = _binding(conversation={"human_handoff": True, "closing_lead_minutes": 0})
    assert channel_settings.settings_of(broken).human_handoff is False
    assert (
        channel_settings.settings_of(_binding(conversation="yes")).human_handoff
        is False
    )


def test_saved_settings_are_read_back() -> None:
    read = channel_settings.settings_of(
        _binding(conversation={"human_handoff": True, "default_agent_id": "t-9"})
    )
    assert read.human_handoff is True and read.default_agent_id == "t-9"


def test_only_buddys_binding_shows_buddys_settings() -> None:
    buddy = channel_settings._read(_binding(is_primary=False, conversation={}))
    other = channel_settings._read(_binding(id="b-2"))
    assert buddy.is_buddy_binding and buddy.conversation == ConversationSettings()
    assert not other.is_buddy_binding and other.conversation is None
    assert other.is_primary and other.installation_id == "i-1"


async def test_no_buddy_binding_reads_the_defaults(monkeypatch) -> None:
    async def none(*args: Any) -> None:
        return None

    monkeypatch.setattr(channel_settings.binding_accessor, "buddy_binding", none)
    assert await channel_settings.conversation_settings("shop", "whatsapp") == (
        ConversationSettings()
    )


async def test_a_binding_that_isnt_buddys_reads_the_defaults(monkeypatch) -> None:
    """Every other binding does nothing in the inbox: no agent, no handoff."""

    async def primary(*args: Any) -> ChannelBinding:
        return _binding()

    monkeypatch.setattr(channel_settings.binding_accessor, "get_binding", primary)
    read = await channel_settings.conversation_settings("shop", "whatsapp", "b-1")
    assert read == ConversationSettings()


# --- writing: one atom over the merchant's bindings ----------------------------


class _Bindings:
    """The merchant's bindings, faked: the atom runs with a sentinel txn and
    every write lands on these rows."""

    TXN = object()

    def __init__(self, *rows: ChannelBinding) -> None:
        self.rows = {row.id: row for row in rows}
        #: (binding id, settings written — None when taken off), in order.
        self.writes: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        #: (binding id, marked as the template channel or not), in order.
        self.marks: List[Tuple[str, bool]] = []
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

        async def mark(txn, merchant_id, binding_id, is_primary):
            assert txn is self.TXN
            self.marks.append((binding_id, is_primary))
            if is_primary:
                # crm_channel_binding_primary_uq, checked per statement: the
                # current primary must already be unmarked.
                assert not any(
                    other.is_primary
                    for other in rows.values()
                    if other.id != binding_id
                )
            rows[binding_id].is_primary = is_primary
            return rows[binding_id]

        async def update(txn, merchant_id, binding_id, conversation):
            assert txn is self.TXN
            self.writes.append((binding_id, conversation))
            row = rows[binding_id]
            if conversation is None:
                row.capabilities.pop("conversation", None)
                return row
            # crm_channel_binding_buddy_uq, checked per statement: another
            # holder must already be gone.
            assert not any(
                "conversation" in other.capabilities
                for other in rows.values()
                if other.id != binding_id
            )
            row.capabilities["conversation"] = dict(conversation)
            return row

        async def get_template(template_id: str):
            return template

        async def merchants(ids: List[str]):
            # "shop" sits under reseller r-1.
            return [SimpleNamespace(id="shop", reseller_id="r-1")], 1

        async def letter(**fields: Any) -> None:
            self.letters.append(fields)

        accessor = channel_settings.binding_accessor
        monkeypatch.setattr(channel_settings, "file_buddy_bot_moved_letter", letter)
        monkeypatch.setattr(channel_settings, "atomically", atomically)
        monkeypatch.setattr(accessor, "lock_channel_bindings", lock)
        monkeypatch.setattr(accessor, "update_template_message_channel", mark)
        monkeypatch.setattr(accessor, "update_conversation_channel", update)
        monkeypatch.setattr(channel_settings, "get_template_by_id", get_template)
        monkeypatch.setattr(channel_settings, "get_merchants_by_ids", merchants)


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
    read = await channel_settings.update_channel_settings(
        "shop", "b-1", _patch(human_handoff=True)
    )
    # The stored field stays, the sent one lands, nothing else is written.
    assert bindings.writes == [
        ("b-1", {"closing_lead_minutes": 20, "human_handoff": True})
    ]
    assert read is not None and read.conversation is not None
    assert (
        read.conversation.human_handoff and read.conversation.closing_lead_minutes == 20
    )


async def test_null_restores_a_default_without_checking_an_agent(monkeypatch) -> None:
    bindings = _Bindings(_binding(conversation={"default_agent_id": "t-9"}))
    bindings.install(monkeypatch, template=None)
    await channel_settings.update_channel_settings(
        "shop", "b-1", _patch(default_agent_id=None)
    )
    assert bindings.writes == [("b-1", {})]
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
    with pytest.raises(channel_settings.SettingsError, match=match):
        await channel_settings.update_channel_settings(
            "shop", "b-1", _patch(default_agent_id="t-9")
        )
    assert bindings.writes == []


async def test_a_resellers_shared_chat_agent_may_be_the_agent(monkeypatch) -> None:
    bindings = _Bindings(_binding(conversation={}))
    bindings.install(monkeypatch, template=_template(merchant_id=None))
    await channel_settings.update_channel_settings(
        "shop", "b-1", _patch(default_agent_id="t-9")
    )
    assert bindings.writes == [("b-1", {"default_agent_id": "t-9"})]


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
    read = await channel_settings.update_channel_settings(
        "shop", "b-2", _patch(is_buddy_binding=True)
    )
    assert "conversation" not in bindings["b-1"].capabilities
    assert bindings["b-2"].capabilities["conversation"] == config
    assert read is not None and read.is_buddy_binding
    # Off the old binding first, then onto the new one — whole, in one write.
    assert bindings.writes == [("b-1", None), ("b-2", config)]


async def test_a_field_sent_to_another_binding_moves_buddy_there_first(
    monkeypatch,
) -> None:
    bindings = _Bindings(
        _binding(conversation={"human_handoff": True}),
        _binding(id="b-2", is_primary=False),
    )
    bindings.install(monkeypatch)
    await channel_settings.update_channel_settings(
        "shop", "b-2", _patch(claim_sla_minutes=30)
    )
    assert "conversation" not in bindings["b-1"].capabilities
    moved = {"human_handoff": True, "claim_sla_minutes": 30}
    assert bindings["b-2"].capabilities["conversation"] == moved
    assert bindings.writes == [("b-1", None), ("b-2", moved)]


async def test_the_first_buddy_binding_starts_from_the_defaults(monkeypatch) -> None:
    bindings = _Bindings(_binding(), _binding(id="b-2", is_primary=False))
    bindings.install(monkeypatch)
    read = await channel_settings.update_channel_settings(
        "shop", "b-2", _patch(is_buddy_binding=True)
    )
    assert read is not None and read.conversation == ConversationSettings()
    assert bindings.writes == [("b-2", {})]
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
    await channel_settings.update_channel_settings(
        "shop", "b-2", _patch(is_buddy_binding=True)
    )
    assert bindings.letters == [
        {
            "merchant_id": "shop",
            "channel": "whatsapp",
            "from_binding_id": "b-1",
            "to_binding_id": "b-2",
        }
    ]
    await channel_settings.update_channel_settings(
        "shop", "b-2", _patch(human_handoff=True)
    )
    assert len(bindings.letters) == 1


async def test_buddy_cannot_move_to_a_paused_binding(monkeypatch) -> None:
    bindings = _Bindings(
        _binding(conversation={}),
        _binding(id="b-2", is_primary=False, status="paused"),
    )
    bindings.install(monkeypatch)
    with pytest.raises(channel_settings.SettingsError, match="paused"):
        await channel_settings.update_channel_settings(
            "shop", "b-2", _patch(is_buddy_binding=True)
        )
    assert bindings.writes == []  # refused before anything is written


async def test_the_primary_moves_within_one_account(monkeypatch) -> None:
    bindings = _Bindings(_binding(), _binding(id="b-2", is_primary=False))
    bindings.install(monkeypatch)
    read = await channel_settings.update_channel_settings(
        "shop", "b-2", _patch(is_primary=True)
    )
    assert read is not None and read.is_primary
    assert not bindings["b-1"].is_primary
    # Off the current primary first, then onto the new one.
    assert bindings.marks == [("b-1", False), ("b-2", True)]


async def test_the_primary_never_moves_to_another_account(monkeypatch) -> None:
    """Templates are approved per provider account (D27)."""
    bindings = _Bindings(
        _binding(), _binding(id="b-2", installation_id="i-2", is_primary=False)
    )
    bindings.install(monkeypatch)
    with pytest.raises(channel_settings.SettingsError, match="same account"):
        await channel_settings.update_channel_settings(
            "shop", "b-2", _patch(is_primary=True)
        )
    assert bindings["b-1"].is_primary and not bindings["b-2"].is_primary
    assert bindings.marks == []  # refused before anything is written


async def test_with_no_primary_any_active_binding_may_take_over(monkeypatch) -> None:
    bindings = _Bindings(
        _binding(is_primary=False, status="paused"),
        _binding(id="b-2", installation_id="i-2", is_primary=False),
    )
    bindings.install(monkeypatch)
    read = await channel_settings.update_channel_settings(
        "shop", "b-2", _patch(is_primary=True)
    )
    assert read is not None and read.is_primary
    assert bindings.marks == [("b-2", True)]  # no primary to unmark


async def test_a_binding_that_is_not_the_merchants_is_none(monkeypatch) -> None:
    bindings = _Bindings(_binding(), _binding(id="b-9", status=BINDING_RETIRED))
    bindings.install(monkeypatch)
    assert (
        await channel_settings.update_channel_settings(
            "shop", "b-2", _patch(is_primary=True)
        )
        is None
    )
    assert (
        await channel_settings.update_channel_settings(
            "shop", "b-9", _patch(is_primary=True)
        )
        is None
    )


@pytest.mark.parametrize("flag", ["is_primary", "is_buddy_binding"])
def test_neither_flag_can_be_turned_off(flag) -> None:
    """Pick another binding instead: there is always one of each, or none."""
    with pytest.raises(ValidationError):
        ConversationSettingsPatch.model_validate({"merchant_id": "shop", flag: False})


def test_buddys_settings_are_built_from_what_is_stored_and_what_was_sent() -> None:
    build = channel_settings.build_conversation_settings
    stored = {"human_handoff": True, "default_agent_id": "t-9"}
    assert build(stored, {"claim_sla_minutes": 30}) == {
        "human_handoff": True,
        "default_agent_id": "t-9",
        "claim_sla_minutes": 30,
    }
    # null takes a field back to its default; the stored object is not touched.
    assert build(stored, {"default_agent_id": None}) == {"human_handoff": True}
    assert stored == {"human_handoff": True, "default_agent_id": "t-9"}
    # Nothing stored, or a stored value that is not an object: start empty.
    assert build(None, {}) == {} and build("yes", {"human_handoff": True}) == {
        "human_handoff": True
    }


def test_the_writer_puts_or_takes_off_only_buddys_key() -> None:
    query, values = update_conversation_channel_query(
        "shop", "b-1", {"human_handoff": True}
    )
    assert "jsonb_set(capabilities, '{conversation}', $3::jsonb)" in query
    assert "WHEN $3::jsonb IS NULL THEN capabilities - 'conversation'" in query
    assert "WHERE merchant_id = $1" in query and "AND id = $2::uuid" in query
    assert values == ["shop", "b-1", '{"human_handoff": true}']
    # Taking it off binds SQL NULL — never the JSON text 'null', which would
    # store a null under the key and keep the binding Buddy's.
    assert update_conversation_channel_query("shop", "b-1", None)[1][2] is None


def test_the_bindings_atom_locks_in_one_order_and_marks_with_one_writer() -> None:
    query, values = lock_channel_bindings_query("shop", "b-1")
    assert (
        "ORDER BY id" in query and "FOR UPDATE" in query and values == ["shop", "b-1"]
    )
    # One writer for the template channel: the atom unmarks, then marks.
    query, values = update_template_message_channel_query("shop", "b-2", True)
    assert "SET is_primary = $3" in query and "WHERE merchant_id = $1" in query
    assert values == ["shop", "b-2", True]


def test_buddys_binding_is_read_by_the_unique_indexs_predicate() -> None:
    from pathlib import Path

    ddl = (
        Path(__file__).parents[2]
        / "app/database/migrations/085_add_message_receipts_and_buddy_binding.sql"
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
        return channel_settings._read(_binding(conversation={}))

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
        monkeypatch, updated=channel_settings.SettingsError("that agent cannot chat")
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
    """channel_settings.py writes them, record declares them (rule 12 forbids the
    import) — renamed on one side only, the consumer would read nothing."""
    from pathlib import Path

    declared = {
        f.path.removeprefix("payload.")
        for f in catalog.CATALOG[("whatsapp", "binding.buddy_bot_moved")].fields
    }
    source = (
        Path(__file__).parents[2] / "app/crm/connectivity/accounts/channel_settings.py"
    )
    assert all(f'"{key}"' in source.read_text() for key in declared)
