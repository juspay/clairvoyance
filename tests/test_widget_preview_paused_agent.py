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

from typing import Optional

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routers.breeze_buddy import widget_common

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
async def test_only_the_console_can_talk_to_a_paused_agent(row) -> None:
    row(_Config(active=False))
    assert (await _resolve(CONSOLE)).id == WIDGET_CONFIG_ID
    # A storefront, even its own, cannot: a paused widget serves no shopper.
    with pytest.raises(HTTPException) as raised:
        await _resolve(STOREFRONT)
    assert raised.value.status_code == 401
