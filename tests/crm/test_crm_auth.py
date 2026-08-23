"""Token extraction for the s2s door (A5/A9 seam)."""

import asyncio
from types import SimpleNamespace
from typing import Dict, cast

import pytest
from fastapi import HTTPException, Request

import app.crm.auth as crm_auth
from app.crm.auth import _extract_token, verify_s2s_merchant


def _request(headers: Dict[str, str]) -> Request:
    # Only .headers is touched; a stub is honest and avoids ASGI setup.
    return cast(Request, SimpleNamespace(headers=headers))


def test_bearer_token_extracted() -> None:
    req = _request({"authorization": "Bearer abc.def"})
    assert _extract_token(req) == "abc.def"


def test_bearer_is_case_insensitive() -> None:
    req = _request({"authorization": "bearer abc"})
    assert _extract_token(req) == "abc"


def test_s2s_header_fallback() -> None:
    req = _request({"x-s2s-token": "tok1"})
    assert _extract_token(req) == "tok1"


def test_raw_authorization_fallback() -> None:
    req = _request({"authorization": "rawtoken"})
    assert _extract_token(req) == "rawtoken"


def test_missing_token_is_none() -> None:
    assert _extract_token(_request({})) is None


def _s2s(monkeypatch: pytest.MonkeyPatch, stored: str | None) -> None:
    async def fake_get(merchant_id: str) -> str | None:
        return stored

    monkeypatch.setattr(crm_auth, "get_merchant_s2s_token", fake_get)
    monkeypatch.setattr(
        crm_auth.rbac_token_manager, "verify_rbac_token", lambda token: None
    )


def test_s2s_unknown_merchant_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _s2s(monkeypatch, stored=None)  # 404, not 403 — callers can't probe ids
    with pytest.raises(HTTPException) as e:
        asyncio.run(verify_s2s_merchant("m1", _request({"x-s2s-token": "t"})))
    assert e.value.status_code == 404


def test_s2s_token_mismatch_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _s2s(monkeypatch, stored="right-token")
    with pytest.raises(HTTPException) as e:
        asyncio.run(verify_s2s_merchant("m1", _request({"x-s2s-token": "wrong"})))
    assert e.value.status_code == 401


def test_s2s_valid_token_returns_merchant(monkeypatch: pytest.MonkeyPatch) -> None:
    _s2s(monkeypatch, stored="tok")
    result = asyncio.run(verify_s2s_merchant("m1", _request({"x-s2s-token": "tok"})))
    assert result == "m1"
