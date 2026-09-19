"""Regression tests for the Soniox endpoint watchdog.

History (incident 2026-09-16, flipkart qwen templates): pipecat's Soniox
service pushes a final ``TranscriptionFrame`` only when Soniox emits an
``<end>``/``<fin>`` token. The websocket session intermittently went quiet
right after an utterance's tokens were recognized — no ``<end>`` arrived, the
user's repeat utterances produced nothing, and the buffered finals flushed
only when the connection died ~35s later. Users heard 35-50s of dead air and
the LLM saw only the first utterance.

Fix (live-validated against the Soniox API): when finalized tokens sit
un-endpointed for ``finalize_after_secs``, send ``{"type": "finalize"}`` once
per idle window — Soniox answers with ``<fin>``, which flushes the buffer and
completes the turn. These tests lock in the tick logic, the websocket liveness
settings, and the template-field wiring (``stt.soniox.finalize_after_secs``).
"""

from __future__ import annotations

import asyncio
import time

from pipecat.frames.frames import TranscriptionFrame
from pipecat.services.soniox.stt import FINALIZE_MESSAGE
from websockets.protocol import State

from app.ai.voice.agents.breeze_buddy.stt import (
    BREEZE_BUDDY_SONIOX_FINALIZE_AFTER_SECS,
    create_stt_from_config,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    SonioxSTTConfig,
    STTConfiguration,
    STTProvider,
)
from app.ai.voice.stt.soniox import service as soniox_service_module
from app.ai.voice.stt.soniox.service import SonioxSTTServiceWithEndpointDelay


class FakeWebSocket:
    def __init__(self):
        self.state = State.OPEN
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


class ExplodingWebSocket(FakeWebSocket):
    async def send(self, message):
        raise ConnectionError("send on dead socket")


def _make_service(**kwargs):
    svc = SonioxSTTServiceWithEndpointDelay(api_key="test-key", **kwargs)
    ws = FakeWebSocket()
    svc._websocket = ws
    return svc, ws


def _final_tokens(text="haan, bolo"):
    return [{"text": token, "is_final": True} for token in text.split(" ")]


# ----------------------------------------------------------------- tick logic


async def test_no_finalize_when_buffer_empty():
    svc, ws = _make_service()
    svc._final_transcription_buffer = []
    svc._last_tokens_received = time.time() - 10
    await svc._finalize_watchdog_tick()
    assert ws.sent == []


async def test_no_finalize_while_tokens_are_fresh():
    svc, ws = _make_service()
    svc._final_transcription_buffer = _final_tokens()
    svc._last_tokens_received = time.time()
    await svc._finalize_watchdog_tick()
    await svc._finalize_watchdog_tick()
    assert ws.sent == []
    # Fresh tokens also re-arm after a previous finalize.
    svc._finalize_sent_this_idle = True
    await svc._finalize_watchdog_tick()
    assert not svc._finalize_sent_this_idle


async def test_finalizes_exactly_once_per_idle_window():
    svc, ws = _make_service(finalize_after_secs=2.0)
    svc._final_transcription_buffer = _final_tokens()
    svc._last_tokens_received = time.time() - 5
    for _ in range(3):
        await svc._finalize_watchdog_tick()
    assert ws.sent == [FINALIZE_MESSAGE]
    assert svc._watchdog_finalizes_sent == 1
    assert svc._finalize_sent_this_idle
    # buffered_since was recorded for final-age logging.
    assert svc._buffered_since is not None


async def test_rearms_when_new_tokens_arrive():
    svc, ws = _make_service(finalize_after_secs=2.0)
    svc._final_transcription_buffer = _final_tokens()
    svc._last_tokens_received = time.time() - 5
    await svc._finalize_watchdog_tick()
    # User speaks again: fresh tokens re-arm the window...
    svc._last_tokens_received = time.time()
    await svc._finalize_watchdog_tick()
    # ...and stall again: a second finalize is allowed.
    svc._last_tokens_received = time.time() - 5
    await svc._finalize_watchdog_tick()
    assert len(ws.sent) == 2
    assert svc._watchdog_finalizes_sent == 2


async def test_rearms_after_buffer_flush():
    svc, ws = _make_service(finalize_after_secs=2.0)
    svc._final_transcription_buffer = _final_tokens()
    svc._last_tokens_received = time.time() - 5
    await svc._finalize_watchdog_tick()
    # <fin> arrived and flushed the buffer.
    svc._final_transcription_buffer = []
    await svc._finalize_watchdog_tick()
    assert svc._buffered_since is None
    # A new utterance stalls: finalize fires again.
    svc._final_transcription_buffer = _final_tokens("hello")
    svc._last_tokens_received = time.time() - 5
    await svc._finalize_watchdog_tick()
    assert len(ws.sent) == 2


async def test_dead_socket_send_failure_is_tolerated():
    svc = SonioxSTTServiceWithEndpointDelay(api_key="test-key")
    ws = ExplodingWebSocket()
    svc._websocket = ws
    svc._final_transcription_buffer = _final_tokens()
    svc._last_tokens_received = time.time() - 5
    # Must not raise — the ping timeout + auto-reconnect own dead sockets.
    await svc._finalize_watchdog_tick()
    assert svc._watchdog_finalizes_sent == 0


async def test_watchdog_disabled_by_zero_threshold():
    svc, ws = _make_service(finalize_after_secs=0)
    svc._final_transcription_buffer = _final_tokens()
    svc._last_tokens_received = time.time() - 60
    await svc._finalize_watchdog_tick()
    assert ws.sent == []


# ------------------------------------------------------- connect/liveness


async def test_connect_uses_tight_pings_and_spawns_watchdog(monkeypatch):
    captured = {}

    async def fake_connect(url, **kwargs):
        captured.update(kwargs)
        return FakeWebSocket()

    monkeypatch.setattr(soniox_service_module, "websocket_connect", fake_connect)
    svc = SonioxSTTServiceWithEndpointDelay(api_key="test-key")
    await svc._connect_websocket()

    # Library defaults are 20s/20s/10s — the incident's 20-40s detection
    # window (interval + timeout + close_timeout).
    assert captured == {
        "ping_interval": 3.0,
        "ping_timeout": 5.0,
        "close_timeout": 3.0,
    }
    assert svc._watchdog_alive()

    await svc._disconnect_websocket()
    await asyncio.sleep(0.1)  # bare-cancel fallback needs a loop tick
    assert not svc._watchdog_alive()
    assert svc._finalize_watchdog_task is None


async def test_connect_passes_customize_pings(monkeypatch):
    captured = {}

    async def fake_connect(url, **kwargs):
        captured.update(kwargs)
        return FakeWebSocket()

    monkeypatch.setattr(soniox_service_module, "websocket_connect", fake_connect)
    svc = SonioxSTTServiceWithEndpointDelay(
        api_key="test-key", ws_ping_interval=3.0, ws_ping_timeout=4.0
    )
    await svc._connect_websocket()
    assert captured == {"ping_interval": 3.0, "ping_timeout": 4.0, "close_timeout": 3.0}
    await svc._disconnect_websocket()


# --------------------------------------------------- template field wiring


async def test_template_finalize_after_secs_flows_to_service(monkeypatch):
    """stt.soniox.finalize_after_secs overrides the env default; 0 (disabled)
    flows through instead of being replaced by the default. A value below the
    endpoint-delay floor is raised, not honored (env default delay = 500ms)."""
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    monkeypatch.setattr(bb_stt, "SONIOX_API_KEY", "test-key")

    svc = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.SONIOX,
            soniox=SonioxSTTConfig(finalize_after_secs=1.5),
        )
    )
    assert isinstance(svc, SonioxSTTServiceWithEndpointDelay)
    assert svc._finalize_after_secs == 1.5

    # Below the floor (2 x 500ms, min 1.0s): clamped so it cannot preempt
    # healthy semantic endpoints.
    svc_fast = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.SONIOX,
            soniox=SonioxSTTConfig(finalize_after_secs=0.5),
        )
    )
    assert isinstance(svc_fast, SonioxSTTServiceWithEndpointDelay)
    assert svc_fast._finalize_after_secs == 1.0

    svc_disabled = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.SONIOX,
            soniox=SonioxSTTConfig(finalize_after_secs=0),
        )
    )
    assert isinstance(svc_disabled, SonioxSTTServiceWithEndpointDelay)
    assert svc_disabled._finalize_after_secs == 0

    svc_default = await create_stt_from_config(
        STTConfiguration(provider=STTProvider.SONIOX)
    )
    assert isinstance(svc_default, SonioxSTTServiceWithEndpointDelay)
    assert svc_default._finalize_after_secs == BREEZE_BUDDY_SONIOX_FINALIZE_AFTER_SECS
    assert svc_default._ws_ping_interval == 3.0
    assert svc_default._ws_ping_timeout == 5.0
    assert svc_default._ws_close_timeout == 3.0


def test_soniox_stt_config_accepts_finalize_after_secs():
    parsed = SonioxSTTConfig.model_validate({"finalize_after_secs": 1.5})
    assert parsed.finalize_after_secs == 1.5
    assert SonioxSTTConfig().finalize_after_secs is None


def test_soniox_stt_config_rejects_negative_finalize_after_secs():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SonioxSTTConfig.model_validate({"finalize_after_secs": -1})


# ----------------------------------------------- disconnect flush + cycles


async def test_disconnect_emits_observability_tokens():
    """The per-call metrics line and the flush token must be greppable in
    logs (OpenObserve: str_match on 'soniox_call_stats' /
    'soniox_disconnect_flush'), and the summary must reflect the rescues."""
    import time as _time

    from loguru import logger as _logger

    lines: list[str] = []
    sink = _logger.add(lambda msg: lines.append(str(msg)), level="INFO")
    try:
        svc, ws = _make_service()
        svc._connected_at = _time.time() - 60
        svc._watchdog_finalizes_sent = 2
        svc._finals_pushed = 5
        svc._final_transcription_buffer = _final_tokens()
        await svc._disconnect_websocket()
    finally:
        _logger.remove(sink)

    stats = [l for l in lines if "soniox_call_stats" in l]
    assert stats, "no per-call stats line emitted"
    assert "watchdog_rescues=2" in stats[0], stats[0]
    assert "finals=6" in stats[0], stats[0]  # 5 pushed + the flushed rescue
    assert "disconnect_flushes=1" in stats[0], stats[0]
    assert "duration=60s" in stats[0], stats[0]
    assert any("soniox_disconnect_flush" in l for l in lines)


async def test_disconnect_flushes_buffered_finals():
    """A dead connection carries no Soniox error/finished message, so the
    only place the caller's stuck words can still be emitted is the
    disconnect path that the reconnect runs through."""
    svc, ws = _make_service()
    svc._final_transcription_buffer = _final_tokens()
    pushed = []

    async def record_frame(frame, direction=None):
        pushed.append(frame)

    svc.push_frame = record_frame
    await svc._disconnect_websocket()
    finals = [f for f in pushed if isinstance(f, TranscriptionFrame)]
    assert len(finals) == 1 and finals[0].finalized
    assert finals[0].text == "haan,bolo"  # tokens join with no separator
    assert svc._final_transcription_buffer == []


async def test_disconnect_flush_noops_on_empty_buffer():
    svc, ws = _make_service()
    pushed = []

    async def record_frame(frame, direction=None):
        pushed.append(frame)

    svc.push_frame = record_frame
    await svc._disconnect_websocket()
    assert pushed == []


async def test_watchdog_lifecycle_across_reconnect_cycles(monkeypatch):
    """The prod auto-reconnect path cycles _disconnect/_connect_websocket —
    each cycle must spawn exactly one live watchdog and leave no orphans."""

    async def fake_connect(url, **kwargs):
        return FakeWebSocket()

    monkeypatch.setattr(soniox_service_module, "websocket_connect", fake_connect)
    svc = SonioxSTTServiceWithEndpointDelay(api_key="test-key")
    for _ in range(3):
        await svc._connect_websocket()
        assert svc._watchdog_alive()
        task = svc._finalize_watchdog_task
        await svc._disconnect_websocket()
        await asyncio.sleep(0.1)  # bare-cancel fallback needs a loop tick
        assert not svc._watchdog_alive()
        assert task is not None and task.done(), "orphan watchdog after disconnect"


# --------------------------------------------- review hardening (PR #1149)


async def test_not_open_tick_keeps_buffered_since():
    """During websockets' close wait the tick must not erase the age anchor —
    the disconnect flush that follows needs it to flag a genuinely late
    rescue (the dead-socket path the LATE FINAL warning exists for)."""
    svc, ws = _make_service(finalize_after_secs=2.0)
    svc._final_transcription_buffer = _final_tokens()
    svc._last_tokens_received = time.time() - 5
    await svc._finalize_watchdog_tick()
    assert svc._buffered_since is not None

    ws.state = State.CLOSED  # dead path: ticks continue during close wait
    await svc._finalize_watchdog_tick()
    assert svc._buffered_since is not None
    assert not svc._finalize_sent_this_idle


async def test_long_utterance_final_is_not_flagged_late():
    """Final tokens buffer progressively during speech, so a long healthy
    utterance has an old first-buffered timestamp. Age must be measured from
    Soniox's last output, not from the first buffered token, or every long
    utterance would page a false LATE FINAL with caller speech attached."""
    from loguru import logger as _logger

    lines: list[str] = []
    sink = _logger.add(lambda msg: lines.append(str(msg)), level="INFO")
    try:
        svc, _ = _make_service()
        svc._final_transcription_buffer = _final_tokens("ek lambi kahaani")
        svc._buffered_since = time.time() - 7  # first token buffered 7s ago
        svc._last_tokens_received = time.time() - 0.4  # output stayed fresh
        await svc._handle_transcription("ek lambi kahaani", is_final=True)
    finally:
        _logger.remove(sink)

    assert not any("soniox_late_final" in l for l in lines)
    final_line = next(l for l in lines if "soniox final:" in l)
    assert "age=0.4s" in final_line, final_line


async def test_dead_path_flush_flags_late_final_without_text():
    """A transcript rescued on a dead connection sat for the whole detection
    window: that flush must page (WARNING) — but without the caller's words,
    which stay at INFO level only."""
    import time as _time

    from loguru import logger as _logger

    lines: list[str] = []
    sink = _logger.add(lambda msg: lines.append(str(msg)), level="INFO")
    try:
        svc, _ = _make_service()
        svc._connected_at = _time.time() - 60
        svc._final_transcription_buffer = [
            {"text": "yeh mera number hai", "is_final": True}
        ]
        svc._last_tokens_received = _time.time() - 12  # dead for 12s
        await svc._disconnect_websocket()
    finally:
        _logger.remove(sink)

    late = [l for l in lines if "soniox_late_final" in l]
    assert late, "dead-path flush was not flagged late"
    assert "yeh" not in late[0] and "number" not in late[0], late[0]
    # The transcript itself is still visible — at INFO only.
    info_final = [l for l in lines if "soniox final:" in l]
    assert info_final and "yeh mera number hai" in info_final[0]
    stats = [l for l in lines if "soniox_call_stats" in l]
    assert stats and "late_finals=1" in stats[0], stats[0]
    assert "disconnect_flushes=1" in stats[0], stats[0]


def test_finalize_floored_against_endpoint_delay_budget():
    """The watchdog must never fire inside Soniox's max_endpoint_delay_ms
    budget (it would preempt every healthy semantic endpoint)."""

    def _native(**kwargs):
        # Native endpoint detection is the prod mode (vad_force_turn_endpoint
        # defaults to True in pipecat, which has no <end> budget to protect).
        return SonioxSTTServiceWithEndpointDelay(
            api_key="test-key", vad_force_turn_endpoint=False, **kwargs
        )

    svc = _native(max_endpoint_delay_ms=2000, finalize_after_secs=1.0)
    assert svc._finalize_after_secs == 4.0  # 2 x 2000ms

    svc = _native(max_endpoint_delay_ms=500, finalize_after_secs=0.5)
    assert svc._finalize_after_secs == 1.0  # max(2 x 500ms, 1.0s floor)

    svc = _native(max_endpoint_delay_ms=500, finalize_after_secs=2.0)
    assert svc._finalize_after_secs == 2.0  # above the floor: honored

    # VAD-forced endpointing has no native <end> budget to protect.
    svc = SonioxSTTServiceWithEndpointDelay(
        api_key="test-key",
        max_endpoint_delay_ms=2000,
        finalize_after_secs=1.0,
        vad_force_turn_endpoint=True,
    )
    assert svc._finalize_after_secs == 1.0

    # No endpoint delay configured: nothing to floor against.
    svc = SonioxSTTServiceWithEndpointDelay(api_key="test-key", finalize_after_secs=0.5)
    assert svc._finalize_after_secs == 0.5

    # Explicitly disabled stays disabled.
    svc = SonioxSTTServiceWithEndpointDelay(
        api_key="test-key",
        max_endpoint_delay_ms=2000,
        finalize_after_secs=0,
        vad_force_turn_endpoint=False,
    )
    assert svc._finalize_after_secs == 0


def test_ws_liveness_env_validation(monkeypatch):
    """0 is not "disabled" for websockets keepalive (only None is): a 0 ping
    interval is a sleep(0) ping storm and a 0 pong timeout kills every
    connection on its first ping. Bad values must fail at boot."""
    import importlib

    import pytest

    import app.core.config.static as static_module

    def _reload(interval="3.0", timeout="5.0", close="3.0"):
        # Set all three every time: a failed reload leaves both the module
        # namespace and the monkeypatched env partially updated.
        monkeypatch.setenv("BREEZE_BUDDY_SONIOX_WS_PING_INTERVAL", interval)
        monkeypatch.setenv("BREEZE_BUDDY_SONIOX_WS_PING_TIMEOUT", timeout)
        monkeypatch.setenv("BREEZE_BUDDY_SONIOX_WS_CLOSE_TIMEOUT", close)
        importlib.reload(static_module)

    try:
        with pytest.raises(ValueError, match="WS_PING_INTERVAL"):
            _reload(interval="0")
        with pytest.raises(ValueError, match="WS_PING_TIMEOUT"):
            _reload(timeout="nan")
        with pytest.raises(ValueError, match="WS_CLOSE_TIMEOUT"):
            _reload(close="-1")
    finally:
        # Restore clean values so later tests are not poisoned by the
        # partially-updated module a failed reload leaves behind.
        _reload()


async def test_template_vad_force_turn_endpoint_flows_to_service(monkeypatch):
    """stt.soniox.vad_force_turn_endpoint overrides the env default; unset
    falls back to BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT. False must win
    over a true env (opt-out), True must win over a false env (opt-in)."""
    import app.ai.voice.agents.breeze_buddy.stt as bb_stt

    monkeypatch.setattr(bb_stt, "SONIOX_API_KEY", "test-key")
    monkeypatch.setattr(bb_stt, "BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT", False)

    svc_on = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.SONIOX,
            soniox=SonioxSTTConfig(vad_force_turn_endpoint=True),
        )
    )
    assert isinstance(svc_on, SonioxSTTServiceWithEndpointDelay)
    assert svc_on._vad_force_turn_endpoint is True
    assert svc_on.requires_vad_analyzer is True

    monkeypatch.setattr(bb_stt, "BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT", True)
    svc_off = await create_stt_from_config(
        STTConfiguration(
            provider=STTProvider.SONIOX,
            soniox=SonioxSTTConfig(vad_force_turn_endpoint=False),
        )
    )
    assert isinstance(svc_off, SonioxSTTServiceWithEndpointDelay)
    assert svc_off._vad_force_turn_endpoint is False
    assert svc_off.requires_vad_analyzer is False

    # Unset template field inherits the env value in both directions.
    svc_env_on = await create_stt_from_config(
        STTConfiguration(provider=STTProvider.SONIOX)
    )
    assert isinstance(svc_env_on, SonioxSTTServiceWithEndpointDelay)
    assert svc_env_on._vad_force_turn_endpoint is True

    monkeypatch.setattr(bb_stt, "BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT", False)
    svc_env_off = await create_stt_from_config(
        STTConfiguration(provider=STTProvider.SONIOX)
    )
    assert isinstance(svc_env_off, SonioxSTTServiceWithEndpointDelay)
    assert svc_env_off._vad_force_turn_endpoint is False
