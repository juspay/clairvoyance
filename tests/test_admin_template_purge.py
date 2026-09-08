"""``DELETE /admin/templates/{id}/purge``: query builders and the route."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers.breeze_buddy.admin.templates import handlers, router
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.database.queries.breeze_buddy.admin import template_purge as q
from app.schemas import UserInfo, UserRole

ADMIN = UserInfo(id="admin-1", username="admin", role=UserRole.ADMIN)
RESELLER = UserInfo(
    id="r-1", username="partner", role=UserRole.RESELLER, reseller_ids=["BB_SHOPIFY"]
)
TID = "9c6e7f0c-db4a-469c-a693-d8c5fc98f499"


def _counts(**over: int) -> Dict[str, int]:
    base = {
        "widget_configs": 0,
        "call_configs": 0,
        "inflight_leads": 0,
        "chat_sessions": 268,
        "active_sessions": 3,
        "chat_messages": 1402,
    }
    base.update(over)
    return base


# --- query builders ---------------------------------------------------------


def test_blockers_query_counts_every_reference_and_the_history():
    sql, values = q.template_purge_blockers_query(TID)
    assert values == [TID]
    for table in (
        "widget_config",
        "call_execution_config",
        "lead_call_tracker",
        "chat_session",
        "chat_message",
    ):
        assert table in sql
    assert "'BACKLOG', 'RETRY', 'PROCESSING'" in sql
    assert "status = 'ACTIVE'" in sql


def test_delete_queries_target_sessions_then_the_template_row():
    s_sql, s_values = q.delete_template_sessions_query(TID)
    t_sql, t_values = q.delete_template_row_query(TID)
    assert s_values == t_values == [TID]
    assert "DELETE FROM chat_session" in s_sql and "RETURNING id" in s_sql
    assert "DELETE FROM template" in t_sql and "RETURNING id, reseller_id" in t_sql


# --- route ------------------------------------------------------------------


@pytest.fixture()
def purge_mocks(monkeypatch) -> Dict[str, AsyncMock]:
    """Patch every accessor *as imported into the handler module* (repo idiom)."""
    existing = SimpleNamespace(
        id=TID,
        name="thebeyondbound-buddy-assist",
        reseller_id="BB_SHOPIFY",
        merchant_id="beyond-bound.myshopify.com",
        is_active=False,
    )
    mocks: Dict[str, AsyncMock] = {
        "get_template_by_id": AsyncMock(return_value=existing),
        "fetch_template_purge_blockers": AsyncMock(return_value=_counts()),
        "purge_template": AsyncMock(
            return_value={
                "template": {"id": TID, "name": existing.name},
                "chat_sessions_deleted": 268,
            }
        ),
        "invalidate_template": AsyncMock(return_value=None),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(handlers, name, mock)
    return mocks


@pytest.fixture()
def purge_app(purge_mocks):
    def make(user: UserInfo) -> TestClient:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_with_rbac] = lambda: user
        return TestClient(app)

    return make


def test_admin_purges_sessions_and_template(purge_app, purge_mocks):
    res = purge_app(ADMIN).delete(f"/admin/templates/{TID}/purge")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["purged"] is True
    assert body["template"]["name"] == "thebeyondbound-buddy-assist"
    assert body["chat_sessions"] == 268 and body["chat_messages"] == 1402
    assert body["active_sessions"] == 3
    assert body["blockers"] == {
        "widget_configs": 0,
        "call_configs": 0,
        "inflight_leads": 0,
    }
    purge_mocks["purge_template"].assert_awaited_once_with(TID)
    purge_mocks["invalidate_template"].assert_awaited_once_with(TID)


def test_dry_run_reports_without_deleting(purge_app, purge_mocks):
    res = purge_app(ADMIN).delete(f"/admin/templates/{TID}/purge?dry_run=true")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["purged"] is False and body["chat_sessions"] == 268
    purge_mocks["purge_template"].assert_not_awaited()
    purge_mocks["invalidate_template"].assert_not_awaited()


@pytest.mark.parametrize(
    "field, phrase",
    [
        ("widget_configs", "widget config(s) still bind"),
        ("call_configs", "call execution config(s)"),
        ("inflight_leads", "in-flight lead(s)"),
    ],
)
def test_live_references_block_the_purge(purge_app, purge_mocks, field, phrase):
    purge_mocks["fetch_template_purge_blockers"].return_value = _counts(**{field: 2})
    res = purge_app(ADMIN).delete(f"/admin/templates/{TID}/purge")
    assert res.status_code == 409, res.text
    assert phrase in res.json()["detail"]
    purge_mocks["purge_template"].assert_not_awaited()


def test_chat_history_alone_never_blocks(purge_app, purge_mocks):
    purge_mocks["fetch_template_purge_blockers"].return_value = _counts(
        chat_sessions=766, active_sessions=766, chat_messages=9000
    )
    res = purge_app(ADMIN).delete(f"/admin/templates/{TID}/purge")
    assert res.status_code == 200, res.text


def test_missing_template_is_404(purge_app, purge_mocks):
    purge_mocks["get_template_by_id"].return_value = None
    res = purge_app(ADMIN).delete(f"/admin/templates/{TID}/purge")
    assert res.status_code == 404
    purge_mocks["fetch_template_purge_blockers"].assert_not_awaited()


def test_template_vanishing_mid_purge_is_404_and_rolls_back(purge_app, purge_mocks):
    purge_mocks["purge_template"].return_value = None
    res = purge_app(ADMIN).delete(f"/admin/templates/{TID}/purge")
    assert res.status_code == 404
    purge_mocks["invalidate_template"].assert_not_awaited()


def test_purge_failure_is_500_with_rollback_note(purge_app, purge_mocks):
    purge_mocks["purge_template"].side_effect = RuntimeError("fk violation")
    res = purge_app(ADMIN).delete(f"/admin/templates/{TID}/purge")
    assert res.status_code == 500
    assert "rolled back" in res.json()["detail"]


def test_reseller_is_forbidden(purge_app, purge_mocks):
    res = purge_app(RESELLER).delete(f"/admin/templates/{TID}/purge")
    assert res.status_code == 403
    purge_mocks["get_template_by_id"].assert_not_awaited()
