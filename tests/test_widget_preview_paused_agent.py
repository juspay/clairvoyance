"""A paused agent previews for the console, and stays paused for shoppers.

Pausing is how a merchant stops serving shoppers, and the moment they most
want to look at their agent is before they have ever unpaused it. Session
CREATE understood that. The session-BOUND routes did not: each re-read
the widget config and applied the active flag itself, so the console opened
a session, sent one word, and got 401 — rendered to the merchant as "Your
chat session expired", on a session one second old.

The rule now lives in one place. These tests hold both halves of it: a
console preview session gets through a paused widget, and a storefront
session does not. "Preview" is the server-signed claim on the widget token
(minted from a console preview grant), never the Origin header (N-WID-3).
"""

from __future__ import annotations

from typing import Optional

import pytest
from fastapi import HTTPException

from app.api.routers.breeze_buddy import widget_common

WIDGET_CONFIG_ID = "00000000-0000-0000-0000-0000000000aa"


class _Config:
    """Only what the gate reads."""

    def __init__(self, active: bool) -> None:
        self.id = WIDGET_CONFIG_ID
        self.active = active


@pytest.fixture()
def row(monkeypatch):
    """Install the widget_config the gate will read back."""

    def install(config: Optional[_Config]) -> None:
        async def fake(_widget_config_id: str):
            return config

        monkeypatch.setattr(widget_common, "get_widget_config_by_id", fake)

    return install


async def _resolve(*, preview: bool, preview_paused: bool = True):
    return await widget_common.resolve_session_widget_config(
        widget_config_id=WIDGET_CONFIG_ID,
        preview=preview,
        preview_paused=preview_paused,
    )


@pytest.mark.asyncio
async def test_only_a_preview_session_can_talk_to_a_paused_agent(row) -> None:
    row(_Config(active=False))
    assert (await _resolve(preview=True)).id == WIDGET_CONFIG_ID
    # A storefront session cannot: a paused widget serves no shopper.
    with pytest.raises(HTTPException) as raised:
        await _resolve(preview=False)
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_voice_and_try_on_stay_paused_even_for_a_preview(row) -> None:
    row(_Config(active=False))
    with pytest.raises(HTTPException) as raised:
        await _resolve(preview=True, preview_paused=False)
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_an_active_widget_serves_every_session(row) -> None:
    row(_Config(active=True))
    assert (await _resolve(preview=False)).id == WIDGET_CONFIG_ID
