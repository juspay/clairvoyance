"""Where a late Soniox final's delay sat: in the pod, or on Soniox's side.

Incident 2026-09-20 (call 615b9ecd): the caller's words reached Soniox 4-28s
after they were spoken and no log line could say where the audio waited. Every
``soniox final`` line now carries ``pod_lag`` (audio reaching the service
behind real time) and ``soniox_lag`` (time from sending the utterance's end
to its final), and ``soniox_call_stats`` carries their maxima.
"""

from __future__ import annotations

from websockets.protocol import State

from app.ai.voice.stt.soniox import service as mod
from app.ai.voice.stt.soniox.service import SonioxSTTServiceWithEndpointDelay

RATE = 8000
CHUNK_SECS = 0.02
CHUNK = b"\x01\x00" * int(RATE * CHUNK_SECS)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeWebSocket:
    def __init__(self, clock: FakeClock, block_secs: float = 0.0) -> None:
        self.state = State.OPEN
        self.sent: list[bytes | str] = []
        self._clock = clock
        self._block_secs = block_secs

    async def send(self, message: bytes | str) -> None:
        self._clock.t += self._block_secs  # a stalled socket send
        self.sent.append(message)

    async def close(self) -> None:
        self.state = State.CLOSED


class RecordingLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def __getattr__(self, level: str):
        if level not in ("debug", "info", "warning", "error", "exception"):
            raise AttributeError(level)
        return lambda msg, *a, **k: self.lines.append((level, msg))

    def of(self, level: str) -> list[str]:
        return [m for lvl, m in self.lines if lvl == level]


def _service(
    block_secs: float = 0.0,
) -> tuple[SonioxSTTServiceWithEndpointDelay, FakeWebSocket, FakeClock]:
    clock = FakeClock()
    ws = FakeWebSocket(clock, block_secs)
    svc = SonioxSTTServiceWithEndpointDelay(api_key="test-key")
    svc._websocket = ws  # type: ignore[assignment]
    svc._sample_rate = RATE
    svc._lag_clock = clock
    return svc, ws, clock


async def _stream(svc, clock: FakeClock, chunks: int, wall_per_chunk: float) -> None:
    """Push ``chunks`` 20ms chunks, the wall clock advancing per chunk."""
    for _ in range(chunks):
        async for _frame in svc.run_stt(CHUNK):
            pass
        clock.t += wall_per_chunk


async def test_pod_lag_grows_when_audio_arrives_slower_than_real_time():
    svc, ws, clock = _service()
    await _stream(svc, clock, 50, CHUNK_SECS)  # on time
    assert svc._pod_lag < 0.05
    await _stream(svc, clock, 20, 0.1)  # 20ms of audio every 100ms
    # The 20th chunk lands 1.9s after the first slow one carrying 0.4s of audio.
    assert abs(svc._pod_lag - (19 * 0.1 - 20 * CHUNK_SECS)) < 0.01
    assert svc._pod_lag_max > 1.4
    assert len(ws.sent) == 70


async def test_pod_lag_reanchors_after_a_source_pause():
    svc, _ws, clock = _service()
    await _stream(svc, clock, 20, 0.1)
    assert svc._pod_lag > 1.0
    clock.t += 5.0  # the source stopped, nothing was queued
    await _stream(svc, clock, 5, CHUNK_SECS)
    assert svc._pod_lag < 0.05


async def test_soniox_lag_is_measured_from_the_send_of_the_utterance_end():
    svc, _ws, clock = _service()
    await _stream(svc, clock, 100, CHUNK_SECS)  # 2.0s of audio, sent on time
    # The utterance ended 1.5s into the stream: that chunk went out at t=1001.48.
    svc._final_transcription_buffer = [
        {"text": "haan", "is_final": True, "start_ms": 1100, "end_ms": 1500}
    ]
    clock.t = 1002.3
    lag = svc._soniox_lag()
    assert lag is not None and abs(lag - 0.82) < 1e-6
    svc._final_transcription_buffer = []
    assert svc._soniox_lag() is None  # nothing to attribute


async def test_final_line_carries_both_lags_and_stats_keep_the_maxima(monkeypatch):
    log = RecordingLogger()
    monkeypatch.setattr(mod, "logger", log)
    svc, _ws, clock = _service()
    await _stream(svc, clock, 100, CHUNK_SECS)
    svc._final_transcription_buffer = [
        {"text": "haan", "is_final": True, "end_ms": 1500}
    ]
    svc._last_tokens_received = 0.0
    clock.t += 2.0  # Soniox took its time
    await svc._handle_transcription("haan", is_final=True)
    (final_line,) = [m for m in log.of("info") if m.startswith("soniox final:")]
    assert "pod_lag=0.0s" in final_line
    assert "soniox_lag=2.5s" in final_line

    svc._log_call_stats()
    (stats,) = [m for m in log.of("warning") if m.startswith("soniox_call_stats:")]
    assert "pod_lag_max=0.0s soniox_lag_max=2.5s send_block_max=0.00s" in stats


async def test_a_blocked_send_counts_against_soniox_not_the_pod(monkeypatch):
    log = RecordingLogger()
    monkeypatch.setattr(mod, "logger", log)
    svc, _ws, clock = _service(block_secs=3.0)
    await _stream(svc, clock, 3, CHUNK_SECS)
    assert svc._pod_lag < 0.05
    assert svc._send_block_max == 3.0
    svc._final_transcription_buffer = [{"text": "haan", "is_final": True, "end_ms": 40}]
    soniox_lag = svc._soniox_lag()
    assert soniox_lag is not None and soniox_lag >= 3.0

    svc._log_call_stats()
    (stats,) = [m for m in log.of("info") if m.startswith("soniox_call_stats:")]
    assert "send_block_max=3.00s" in stats


async def test_a_new_session_restarts_the_send_clock(monkeypatch):
    svc, ws, clock = _service()
    await _stream(svc, clock, 10, CHUNK_SECS)
    assert svc._audio_secs_sent > 0 and svc._sent_at

    async def fake_connect(url, **kwargs):
        ws.state = State.OPEN
        return ws

    monkeypatch.setattr(mod, "websocket_connect", fake_connect)
    await svc._disconnect_websocket()  # a reconnect runs disconnect + connect
    await svc._connect_websocket()  # Soniox's end_ms clock restarts per session
    assert svc._audio_secs_sent == 0.0 and not svc._sent_at
    await svc._disconnect_websocket()
