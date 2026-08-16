# pyrefly: ignore-errors
# USER is a SimpleNamespace duck-typing UserInfo (username/role only, per
# the brief) rather than a real UserInfo instance; pyrefly flags every call
# site as a bad-argument-type. Runtime correctness is what's under test.
"""Version read handlers: RBAC gate, 404s, active_version computation.

Accessors and RBAC are monkeypatched at the handlers-module namespace —
same seam the handlers import through.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.routers.breeze_buddy.templates.handlers as handlers
from app.schemas.breeze_buddy.template import (
    TemplateVersionDetail,
    TemplateVersionMetadata,
)

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)
USER = SimpleNamespace(username="ops@breeze.in", role="admin")
TEMPLATE = SimpleNamespace(
    id="6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e",
    reseller_id="r1",
    merchant_id=None,
)


def _meta(n, source="update", restored=None):
    return TemplateVersionMetadata(
        id=f"00000000-0000-0000-0000-00000000000{n}",
        template_id=TEMPLATE.id,
        version_number=n,
        name="order-confirmation",
        updated_by="ops@breeze.in",
        change_source=source,
        restored_from=restored,
        created_at=NOW,
    )


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(handlers, "get_template_by_id", _async(TEMPLATE))
    monkeypatch.setattr(handlers, "validate_template_access", lambda *a, **k: None)


def _async(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


async def test_list_versions_reports_active_version(patched, monkeypatch):
    monkeypatch.setattr(
        handlers,
        "list_template_versions",
        _async(([_meta(3, "rollback", 1), _meta(2), _meta(1, "create")], 3)),
    )
    response = await handlers.list_template_versions_handler(
        TEMPLATE.id, page=1, limit=20, current_user=USER
    )
    assert response.total == 3
    assert response.active_version == 3
    assert response.versions[0].change_source == "rollback"
    assert response.versions[0].restored_from == 1


async def test_list_versions_unknown_template_404(monkeypatch):
    monkeypatch.setattr(handlers, "get_template_by_id", _async(None))
    with pytest.raises(HTTPException) as exc:
        await handlers.list_template_versions_handler(
            TEMPLATE.id, page=1, limit=20, current_user=USER
        )
    assert exc.value.status_code == 404


async def test_get_version_unknown_number_404(patched, monkeypatch):
    monkeypatch.setattr(handlers, "get_template_version_by_number", _async(None))
    with pytest.raises(HTTPException) as exc:
        await handlers.get_template_version_handler(
            TEMPLATE.id, version_number=99, current_user=USER
        )
    assert exc.value.status_code == 404


async def test_get_version_returns_detail(patched, monkeypatch):
    detail = TemplateVersionDetail(
        **_meta(2).model_dump(),
        flow={"initial_node": "a", "nodes": {"a": {}}},
    )
    monkeypatch.setattr(handlers, "get_template_version_by_number", _async(detail))
    result = await handlers.get_template_version_handler(
        TEMPLATE.id, version_number=2, current_user=USER
    )
    assert result.version_number == 2
    assert result.flow["initial_node"] == "a"
