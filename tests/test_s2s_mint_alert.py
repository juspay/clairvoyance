"""Every S2S token mint posts a Slack audit notice — who, from where, what scope,
how long — and never the token itself; a Slack failure never fails the mint."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import alerts
from app.api.routers.breeze_buddy.auth import handlers as auth_handlers
from app.schemas import UserRole
from app.schemas.breeze_buddy.auth import S2STokenRequest


@pytest.fixture
def sent(monkeypatch):
    posts: list = []

    async def _send(*, title, fields, **kw):
        posts.append((title, {f["name"]: f["value"] for f in fields}))
        return True

    monkeypatch.setattr(alerts.slack_alert, "send", _send)
    return posts


@pytest.fixture
def run_inline(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(
        auth_handlers, "spawn_background_task", lambda coro, **kw: spawned.append(coro)
    )
    return spawned


def _admin():
    return SimpleNamespace(
        id="u_admin",
        username="ops-admin",
        role=UserRole.ADMIN,
        is_active=True,
        password_hash="h",
        reseller_ids=["*"],
        merchant_ids=["*"],
        email=None,
        owner_id=None,
    )


async def _mint(monkeypatch, **body):
    async def _user(_u):
        return _admin()

    async def _ok(_p, _h):
        return True

    monkeypatch.setattr(auth_handlers, "get_user_by_username", _user)
    monkeypatch.setattr(auth_handlers, "verify_password_async", _ok)
    return await auth_handlers.generate_s2s_token_handler(
        S2STokenRequest(username="ops-admin", password="p", **body), ip="49.37.12.5"
    )


async def test_admin_mint_alerts_with_who_where_scope_and_ttl(
    monkeypatch, sent, run_inline
):
    resp = await _mint(monkeypatch, token_lifetime_days=30)
    assert len(run_inline) == 1
    await run_inline[0]
    ((title, f),) = sent
    assert title == "New S2S Token Minted"
    assert f["Minted by"] == "ops-admin"
    assert f["From IP"] == "49.37.12.5"
    assert f["Token role"] == "admin"
    assert f["Resellers"] == "*" and f["Merchants"] == "*"
    assert f["TTL"] == "30 days"
    assert f["Expires at"] == resp.expires_at
    assert resp.access_token not in str(sent)


async def test_scoped_mint_reports_the_narrowed_scope(monkeypatch, sent, run_inline):
    await _mint(monkeypatch, reseller_ids=["r1"], merchant_ids=["m1", "m2"])
    await run_inline[0]
    ((_, f),) = sent
    assert f["Token role"] == "reseller"
    assert f["Resellers"] == "r1" and f["Merchants"] == "m1, m2"


async def test_merchant_name_is_escaped_for_slack(sent):
    await alerts.emit_s2s_token_minted_alert(
        minted_by="ops-admin",
        ip="",
        token_role="merchant",
        reseller_ids=["r1"],
        merchant_ids=["shop-1"],
        lifetime_days=3650,
        expires_at="2036-10-27T00:00:00+00:00",
        merchant_name="<!channel> Shop",
    )
    ((_, f),) = sent
    assert f["Merchant name"] == "&lt;!channel&gt; Shop"
    assert f["From IP"] == "-"


async def test_a_slack_failure_never_raises(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("slack down")

    monkeypatch.setattr(alerts.slack_alert, "send", _boom)
    await alerts.emit_s2s_token_minted_alert(
        minted_by="a",
        ip="1.2.3.4",
        token_role="admin",
        reseller_ids=["*"],
        merchant_ids=["*"],
        lifetime_days=1,
        expires_at="x",
    )
