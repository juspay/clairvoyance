"""5xx responses carry a fixed message; the exception text stays in the logs.

Messages from our own specific errors (``except ValueError as e: ...
detail=str(e)``, OnboardingFailure, NotImplementedError) are text written by us
for the caller, and are not covered here.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and the leads handlers pull in managers.calls.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.api.routers.breeze_buddy import dragontts
from app.api.routers.breeze_buddy.leads import handlers as lead_handlers
from app.api.routers.feature_flags import handlers as flag_handlers
from app.schemas import UserInfo, UserRole

ADMIN = UserInfo(id="admin-1", username="admin", role=UserRole.ADMIN)
# Stands in for what a real driver error carries: internal hosts, SQL details.
SECRET_TEXT = "invalid input for query argument $1 at 10.0.0.5:5432"
APP_DIR = Path(__file__).resolve().parents[1] / "app"
_BROAD = {"Exception", "BaseException"}


async def test_feature_flag_error_returns_fixed_message(monkeypatch):
    monkeypatch.setattr(
        flag_handlers, "get_all_flags", AsyncMock(side_effect=RuntimeError(SECRET_TEXT))
    )

    with pytest.raises(HTTPException) as caught:
        await flag_handlers.get_feature_flags_handler()

    assert caught.value.status_code == 500
    assert caught.value.detail == "Failed to fetch feature flags"


async def test_lead_error_returns_fixed_message(monkeypatch):
    monkeypatch.setattr(
        lead_handlers,
        "get_lead_by_id",
        AsyncMock(side_effect=RuntimeError(SECRET_TEXT)),
    )

    with pytest.raises(HTTPException) as caught:
        await lead_handlers.get_lead_handler("lead-1", ADMIN)

    assert caught.value.status_code == 500
    assert caught.value.detail == "Unexpected error"


async def test_dragontts_unreachable_hides_the_internal_address(monkeypatch):
    def unreachable(**kwargs):
        raise httpx.ConnectError("All connection attempts failed: 10.0.0.7:8000")

    monkeypatch.setattr(
        dragontts, "DRAGONTTS_URL", AsyncMock(return_value="http://10.0.0.7:8000")
    )
    monkeypatch.setattr(
        dragontts,
        "httpx",
        SimpleNamespace(AsyncClient=unreachable, RequestError=httpx.RequestError),
    )

    with pytest.raises(HTTPException) as caught:
        await dragontts._forward("GET", "/health")

    assert caught.value.status_code == 502
    assert caught.value.detail == "DragonTTS unreachable"


def _status_code(call: ast.Call) -> Optional[int]:
    node = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg == "status_code":
            node = keyword.value
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute) and node.attr.startswith("HTTP_"):
        return int(node.attr.split("_")[1])
    return None


def _offenders(app_dir: Path) -> List[str]:
    """``except Exception as e`` → HTTPException(5xx) whose detail uses ``e``."""
    found: List[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        for handler in ast.walk(ast.parse(path.read_text())):
            if not isinstance(handler, ast.ExceptHandler) or not handler.name:
                continue
            # Only catch-alls: their text is whatever the failure said.
            if not (isinstance(handler.type, ast.Name) and handler.type.id in _BROAD):
                continue
            for call in ast.walk(handler):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "HTTPException"
                ):
                    continue
                code = _status_code(call)
                detail = next(
                    (k.value for k in call.keywords if k.arg == "detail"), None
                )
                if code is None or code < 500 or detail is None:
                    continue
                if any(
                    isinstance(n, ast.Name) and n.id == handler.name
                    for n in ast.walk(detail)
                ):
                    found.append(f"{path.relative_to(app_dir.parent)}:{call.lineno}")
    return found


def test_no_5xx_detail_is_built_from_the_caught_exception():
    assert _offenders(APP_DIR) == []
