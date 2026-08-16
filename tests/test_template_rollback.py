"""Rollback: pure arg-builder semantics + handler wiring.

The arg-builder is where every restore-vs-keep decision lives; each test
pins one line of the design-doc table (docs/TEMPLATE_VERSIONING.md §4).

USER is a SimpleNamespace duck-typing UserInfo (username/role only, per
the brief) rather than a real UserInfo instance; call sites `cast` it to
UserInfo so pyrefly checks the rest of the file normally. Runtime
correctness (not the cast) is what's under test.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple, cast

import pytest
from fastapi import HTTPException

import app.api.routers.breeze_buddy.templates.handlers as handlers
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template import TemplateVersionDetail

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def _existing_template(**overrides: Any) -> TemplateModel:
    fields: Dict[str, Any] = dict(
        id="6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e",
        reseller_id="r1",
        merchant_id="m1",
        name="live-name",
        flow={"initial_node": "new", "nodes": {"new": {}}},
        configurations=ConfigurationModel(
            mcp={
                "servers": [
                    {
                        "name": "crm",
                        "url": "https://mcp.example.com",
                        "auth": {"type": "bearer", "token": "real-live-token"},
                    }
                ]
            }
        ),
        secrets={"api_key": "real-secret"},
        outbound_number_id="22222222-2222-2222-2222-222222222222",
        is_active=True,
        supported_channels=["voice", "chat"],
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return TemplateModel(**fields)


def _snapshot(**overrides: Any) -> TemplateVersionDetail:
    fields: Dict[str, Any] = dict(
        id="00000000-0000-0000-0000-000000000005",
        template_id="6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e",
        version_number=5,
        name="old-name",
        flow={"initial_node": "old", "nodes": {"old": {}}},
        configurations={
            "mcp": {
                "servers": [
                    {
                        "name": "crm",
                        "url": "https://mcp.example.com",
                        "auth": {"type": "bearer", "token": "**********"},
                    }
                ]
            }
        },
        expected_payload_schema={"type": "object"},
        expected_callback_response_schema=None,
        updated_by="ops@breeze.in",
        change_source="update",
        restored_from=None,
        created_at=NOW,
    )
    fields.update(overrides)
    return TemplateVersionDetail(**fields)


def test_rollback_restores_flow_and_schemas_from_snapshot():
    args = handlers.build_rollback_template_args(_existing_template(), _snapshot())
    assert args["flow"] == {"initial_node": "old", "nodes": {"old": {}}}
    assert args["expected_payload_schema"] == {"type": "object"}
    assert args["expected_callback_response_schema"] is None


def test_rollback_keeps_live_identity_and_secrets():
    existing = _existing_template()
    args = handlers.build_rollback_template_args(existing, _snapshot())
    assert args["name"] == "live-name"  # never renamed by rollback
    assert args["secrets"] == {"api_key": "real-secret"}
    assert args["outbound_number_id"] == existing.outbound_number_id
    assert args["is_active"] is True
    assert args["merchant_id"] == "m1"
    assert args["reseller_id"] == "r1"
    assert args["supported_channels"] == ["voice", "chat"]


def test_rollback_unmasks_mcp_auth_from_live_row():
    args = handlers.build_rollback_template_args(_existing_template(), _snapshot())
    token = args["configurations"]["mcp"]["servers"][0]["auth"]["token"]
    assert token == "real-live-token"


def test_rollback_masked_auth_without_live_counterpart_is_cleared():
    existing = _existing_template(configurations=None)
    args = handlers.build_rollback_template_args(existing, _snapshot())
    auth = args["configurations"]["mcp"]["servers"][0]["auth"]
    # Cleared token round-trips through ConfigurationModel validation with
    # exclude_none=True, so the key is dropped entirely rather than kept
    # as an explicit null -- never persist the mask literal either way.
    assert auth.get("token") is None


async def test_rollback_handler_appends_new_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _existing_template()
    captured: Dict[str, Any] = {}

    async def fake_get_template(template_id: str) -> TemplateModel:
        return existing

    async def fake_get_version(
        template_id: str, version_number: int
    ) -> TemplateVersionDetail:
        return _snapshot()

    async def fake_replace(**kwargs: Any) -> Tuple[TemplateModel, int]:
        captured.update(kwargs)
        return existing, 11

    async def fake_invalidate(template_id: str) -> None:
        captured["invalidated"] = template_id

    monkeypatch.setattr(handlers, "get_template_by_id", fake_get_template)
    monkeypatch.setattr(handlers, "get_template_version_by_number", fake_get_version)
    monkeypatch.setattr(handlers, "replace_template", fake_replace)
    monkeypatch.setattr(handlers, "invalidate_template", fake_invalidate)
    monkeypatch.setattr(handlers, "validate_template_access", lambda *a, **k: None)

    user = cast(UserInfo, SimpleNamespace(username="ops@breeze.in", role="admin"))
    result = await handlers.rollback_template_handler(str(existing.id), 5, user)

    assert result.new_version == 11
    assert result.restored_from == 5
    assert captured["change_source"] == "rollback"
    assert captured["restored_from"] == 5
    assert captured["updated_by"] == "ops@breeze.in"
    # Snapshot stored for v11 is the masked snapshot config, unchanged.
    assert (
        captured["snapshot_configurations"]["mcp"]["servers"][0]["auth"]["token"]
        == "**********"
    )
    assert captured["invalidated"] == str(existing.id)


async def test_rollback_handler_rejects_invalid_snapshot_configurations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot that predates today's ConfigurationModel shape (or was
    valid under a since-tightened rule) must not be written back to the
    live row -- see docs/TEMPLATE_VERSIONING.md §6. Confirms the handler
    surfaces a 400 and never calls replace_template with the bad config.
    """
    existing = _existing_template()
    invalid_snapshot = _snapshot(
        configurations={"vad_config": {"confidence": "not-a-number"}}
    )
    replace_called = False

    async def fake_get_template(template_id: str) -> TemplateModel:
        return existing

    async def fake_get_version(
        template_id: str, version_number: int
    ) -> TemplateVersionDetail:
        return invalid_snapshot

    async def fake_replace(**kwargs: Any) -> Tuple[TemplateModel, int]:
        nonlocal replace_called
        replace_called = True
        return existing, 11

    monkeypatch.setattr(handlers, "get_template_by_id", fake_get_template)
    monkeypatch.setattr(handlers, "get_template_version_by_number", fake_get_version)
    monkeypatch.setattr(handlers, "replace_template", fake_replace)
    monkeypatch.setattr(handlers, "validate_template_access", lambda *a, **k: None)

    user = cast(UserInfo, SimpleNamespace(username="ops@breeze.in", role="admin"))
    with pytest.raises(HTTPException) as exc:
        await handlers.rollback_template_handler(str(existing.id), 5, user)

    assert exc.value.status_code == 400
    assert not replace_called


async def test_rollback_unknown_version_404(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _existing_template()

    async def fake_get_template(template_id: str) -> TemplateModel:
        return existing

    async def fake_get_version(
        template_id: str, version_number: int
    ) -> Optional[TemplateVersionDetail]:
        return None

    monkeypatch.setattr(handlers, "get_template_by_id", fake_get_template)
    monkeypatch.setattr(handlers, "get_template_version_by_number", fake_get_version)
    monkeypatch.setattr(handlers, "validate_template_access", lambda *a, **k: None)

    user = cast(UserInfo, SimpleNamespace(username="ops@breeze.in", role="admin"))
    with pytest.raises(HTTPException) as exc:
        await handlers.rollback_template_handler(str(existing.id), 99, user)
    assert exc.value.status_code == 404
