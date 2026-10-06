"""A cached greeting is found with EXISTS: its ~100 KB of audio is not read on every
dial (spec 2026-10-05 §4.10). Both dialers prewarm through these checks."""

from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.managers import utils as U

pytestmark = pytest.mark.asyncio

GREETING = "Hello, this is Breeze"
TEMPLATE: Any = NS(
    id="T1",
    configurations=NS(initial_greeting=GREETING, tts_configuration=None),
)


class _Redis:
    """A Redis that holds the greeting's audio and refuses to hand it out."""

    async def get(self, key):
        raise AssertionError(f"read the audio at {key}")

    async def get_client(self):
        return self

    async def exists(self, key):
        assert key == "greeting:template:T1"
        return 1


async def test_a_cached_static_greeting_is_found_without_reading_its_audio(monkeypatch):
    monkeypatch.setattr(U, "get_redis_service", AsyncMock(return_value=_Redis()))
    monkeypatch.setattr(U, "_gemini_realtime_config", lambda t: None)
    synth = AsyncMock()
    monkeypatch.setattr(U, "generate_audio", synth)
    assert await U.prepare_and_store_initial_greeting("L1", {}, TEMPLATE) == GREETING
    synth.assert_not_awaited()


async def test_a_cached_realtime_opening_line_is_found_without_reading_its_audio(
    monkeypatch,
):
    monkeypatch.setattr(U, "get_redis_service", AsyncMock(return_value=_Redis()))
    monkeypatch.setattr(U, "_gemini_realtime_config", lambda t: object())
    monkeypatch.setattr(U, "greeting_has_variables", lambda text: False)
    generate = AsyncMock()
    monkeypatch.setattr(U, "generate_opening_line_mulaw", generate)
    assert await U.ensure_realtime_opening_line_cached(TEMPLATE) == GREETING
    generate.assert_not_awaited()
