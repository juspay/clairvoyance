"""A paused agent previews for the console, and stays paused for shoppers.

Pausing is how a merchant stops serving shoppers, and the moment they most
want to look at their agent is before they have ever unpaused it. Session
CREATE understood that. The session-BOUND routes did not: each re-read
the widget config and applied the active flag itself, so the console opened
a session, sent one word, and got 401 — rendered to the merchant as "Your
chat session expired", on a session one second old.

The rule now lives in one place. These tests hold both halves of it: the
console gets through a paused widget, and a storefront — even one on the
allow-list — does not.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Optional

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routers.breeze_buddy import widget_common
from app.api.routers.breeze_buddy.widget import handlers

CONSOLE = "https://breezebuddy.ai"
STOREFRONT = "https://beyond-bound.myshopify.com"
WIDGET_CONFIG_ID = "00000000-0000-0000-0000-0000000000aa"


class _Config:
    """Only what the gate reads."""

    def __init__(self, active: bool) -> None:
        self.id = WIDGET_CONFIG_ID
        self.active = active


def _request(origin: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/agent/voice/breeze-buddy/widget/session/s1/message",
            "headers": [(b"origin", origin.encode())],
            "client": ("203.0.113.9", 51234),
            "query_string": b"",
            "scheme": "http",
            "server": ("localhost", 8000),
        }
    )


@pytest.fixture()
def row(monkeypatch):
    """Install the widget_config the gate will read back."""

    def install(config: Optional[_Config]) -> None:
        async def fake(_widget_config_id: str):
            return config

        monkeypatch.setattr(widget_common, "get_widget_config_by_id", fake)

    return install


async def _resolve(origin: str, *, preview_paused: bool = True):
    return await widget_common.resolve_session_widget_config(
        request=_request(origin),
        widget_config_id=WIDGET_CONFIG_ID,
        preview_paused=preview_paused,
    )


@pytest.mark.asyncio
async def test_the_console_can_talk_to_a_paused_agent(row) -> None:
    row(_Config(active=False))
    assert (await _resolve(CONSOLE)).id == WIDGET_CONFIG_ID


@pytest.mark.asyncio
async def test_a_storefront_cannot(row) -> None:
    # Even its own storefront, and even holding a valid session token: a
    # paused widget serves nobody. Otherwise "pause" would mean "pause,
    # unless a shopper already had the panel open".
    row(_Config(active=False))
    with pytest.raises(HTTPException) as raised:
        await _resolve(STOREFRONT)
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_voice_and_try_on_stay_off_for_a_paused_agent_even_in_the_console(
    row,
) -> None:
    # Origin can be claimed by any script; only chat previews a paused agent.
    row(_Config(active=False))
    with pytest.raises(HTTPException) as raised:
        await _resolve(CONSOLE, preview_paused=False)
    assert raised.value.status_code == 401
    source = Path(handlers.__file__).read_text()
    for route in (
        "async def voice_connect_handler(",
        "async def try_on_widget_handler(",
    ):
        body = source[source.index(route) :]
        call = body[: body.index("preview_paused=") + len("preview_paused=False")]
        assert call.endswith("preview_paused=False"), route


@pytest.mark.asyncio
async def test_a_live_agent_serves_everyone(row) -> None:
    row(_Config(active=True))
    assert (await _resolve(STOREFRONT)).id == WIDGET_CONFIG_ID


@pytest.mark.asyncio
async def test_a_token_naming_a_deleted_config_is_refused_either_way(row) -> None:
    # 401, not 404: the useful instruction to a caller holding a token for a
    # row that no longer exists is "abandon this token".
    row(None)
    for origin in (CONSOLE, STOREFRONT):
        with pytest.raises(HTTPException) as raised:
            await _resolve(origin)
        assert raised.value.status_code == 401


def test_no_session_bound_route_reads_the_row_itself() -> None:
    # The rule only holds if every session-bound route goes through
    # resolve_session_widget_config. Try-on landed after the rule did and
    # re-read the row on its own, active flag and all, so the console got
    # its "session expired" 401 back on exactly one route. Only a check on
    # the module itself catches the next route written that way.
    source = Path(handlers.__file__).read_text()
    assert "get_widget_config_by_id(" not in source


def test_production_counts_only_https_console_origins(monkeypatch) -> None:
    from app.core.config import static

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "WIDGET_CONSOLE_ORIGINS", "https://console.test,http://localhost:5173"
    )
    try:
        assert importlib.reload(static).WIDGET_CONSOLE_ORIGINS == [
            "https://console.test"
        ]
    finally:
        monkeypatch.undo()
        importlib.reload(static)


def test_no_local_console_is_trusted_by_default(monkeypatch) -> None:
    from app.core.config import static

    monkeypatch.delenv("WIDGET_CONSOLE_ORIGINS", raising=False)
    try:
        origins = importlib.reload(static).WIDGET_CONSOLE_ORIGINS
        assert all(origin.startswith("https://") for origin in origins)
    finally:
        monkeypatch.undo()
        importlib.reload(static)
