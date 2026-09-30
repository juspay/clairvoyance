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

from pathlib import Path
from typing import Optional

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routers.breeze_buddy import widget_common
from app.api.routers.breeze_buddy.widget import handlers

CONSOLE = "http://localhost:5173"
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


async def _resolve(origin: str):
    return await widget_common.resolve_session_widget_config(
        request=_request(origin), widget_config_id=WIDGET_CONFIG_ID
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
