"""Sarvam STT on a live call: a dropped socket.

pipecat 1.1.0's Sarvam service is not a WebsocketService: when Sarvam closes
the socket mid-call (1011 "VAD RESOURCE_EXHAUSTED") it keeps sending every
20 ms frame into the dead socket and the caller goes unheard. These lock in
our wrapper's reconnect, its cap, the open-speech reset, and the call end
after it. No network: the SDK socket is a fake.
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from pipecat.frames.frames import (
    AudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.stt_service import STTService
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import app.ai.voice.stt.sarvam as sarvam_mod
from app.ai.voice.stt import STT_UNAVAILABLE_EVENT, SarvamConfig, build_sarvam_stt
from app.core.config import static

RESOURCE_EXHAUSTED = (
    "Speech processing is temporarily unavailable (VAD RESOURCE_EXHAUSTED)"
)


def _closed() -> ConnectionClosedError:
    return ConnectionClosedError(Close(1011, RESOURCE_EXHAUSTED), None)


class FakeSocket:
    """The SDK's AsyncSpeechToTextStreamingSocketClient, minus the network."""

    def __init__(self, fail_with: Optional[BaseException] = None):
        self.fail_with = fail_with
        self.attempts = 0
        self.sent: list[bytes] = []
        self.callbacks: dict[Any, list] = {}

    def on(self, event, callback):
        self.callbacks.setdefault(event, []).append(callback)

    async def transcribe(self, audio: str, encoding: str, sample_rate: int):
        self.attempts += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(base64.b64decode(audio))

    async def flush(self):
        if self.fail_with is not None:
            raise self.fail_with


class Recorder:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def _add(self, level: str, msg: str, *args: Any, **_: Any) -> None:
        self.lines.append((level, msg % args if args else msg))

    def info(self, msg, *a, **k):
        self._add("info", msg, *a, **k)

    def warning(self, msg, *a, **k):
        self._add("warning", msg, *a, **k)

    def error(self, msg, *a, **k):
        self._add("error", msg, *a, **k)

    def debug(self, msg, *a, **k):
        self._add("debug", msg, *a, **k)

    def having(self, level: str, text: str) -> list[str]:
        return [m for lv, m in self.lines if lv == level and text in m]


class Harness:
    """A built Sarvam service whose sockets, tasks and pipeline are fakes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, sockets: list, **config):
        self.sockets = list(sockets)  # None = that connect attempt fails
        self.connects = 0
        self.disconnects = 0
        self.pushed: list[Any] = []
        self.broadcast: list[type] = []
        self.errors: list[str] = []
        self.log = Recorder()
        monkeypatch.setattr(sarvam_mod, "logger", self.log)
        monkeypatch.setattr(sarvam_mod, "SARVAM_RECONNECT_BACKOFF_SECS", (0.01, 0.02))
        harness = self

        async def parent_connect(svc):
            await harness._parent_connect(svc)

        async def parent_disconnect(svc):
            await harness._parent_disconnect(svc)

        monkeypatch.setattr(SarvamSTTService, "_connect", parent_connect)
        monkeypatch.setattr(SarvamSTTService, "_disconnect", parent_disconnect)

        async def capture_push(svc, frame, direction=FrameDirection.DOWNSTREAM):
            self.pushed.append(frame)

        monkeypatch.setattr(STTService, "push_frame", capture_push)
        self.svc = build_sarvam_stt(
            SarvamConfig(api_key="k", model="saaras:v3", sample_rate=8000, **config)
        )
        svc = self.svc

        def create_task(coroutine, name=None):
            return asyncio.get_running_loop().create_task(coroutine)

        async def cancel_task(task, timeout=None):
            task.cancel()
            try:
                await task
            except BaseException:
                pass

        async def push_error(svc, error_msg, exception=None, fatal=False):
            self.errors.append(error_msg)

        # Below our push_error override, which quiets per-attempt errors.
        monkeypatch.setattr(FrameProcessor, "push_error", push_error)

        async def broadcast_frame(frame_cls, **kwargs):
            self.broadcast.append(frame_cls)

        async def broadcast_interruption():
            pass

        svc.create_task = create_task
        svc.cancel_task = cancel_task
        svc.broadcast_frame = broadcast_frame
        svc.broadcast_interruption = broadcast_interruption
        svc._sample_rate = 8000

    async def _parent_connect(self, svc):
        self.connects += 1
        nxt = self.sockets.pop(0) if self.sockets else None
        if nxt is None:
            svc._socket_client = None
            await svc.push_error(error_msg="Failed to connect to Sarvam")
            return
        svc._socket_client = nxt
        svc._websocket_context = object()

    async def _parent_disconnect(self, svc):
        self.disconnects += 1
        task = svc._receive_task
        svc._receive_task = None
        if task is not None:
            await svc.cancel_task(task)
        svc._socket_client = None
        svc._websocket_context = None

    async def audio(self, payload: bytes) -> None:
        frame = AudioRawFrame(audio=payload, sample_rate=8000, num_channels=1)
        await self.svc.process_audio_frame(frame, FrameDirection.DOWNSTREAM)

    async def settle(self) -> None:
        task = self.svc._recovery_task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        await asyncio.sleep(0)


async def _until(condition, timeout: float = 2.0) -> None:
    """Wait for a condition, failing (not hanging) if it never holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition never held"
        await asyncio.sleep(0.001)


def _signal(signal: str) -> Any:
    return SimpleNamespace(
        type="events",
        data=SimpleNamespace(signal_type=signal, occured_at=0.0),
        dict=lambda: {"type": "events"},
    )


def _data(text: str, request_id: Optional[str] = "req-1") -> Any:
    return SimpleNamespace(
        type="data",
        data=SimpleNamespace(
            transcript=text, language_code="hi-IN", request_id=request_id
        ),
        dict=lambda: {"type": "data", "data": {"request_id": request_id}},
    )


def _transcripts(h: Harness) -> list[TranscriptionFrame]:
    return [f for f in h.pushed if isinstance(f, TranscriptionFrame)]


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(static, "SARVAM_API_KEY", "test-key")


# ---------------------------------------------------------------- reconnect


async def test_a_closed_socket_stops_the_sends_and_reconnects_once(monkeypatch):
    dead, fresh = FakeSocket(fail_with=_closed()), FakeSocket()
    h = Harness(monkeypatch, [dead, fresh])
    await h.svc._connect()

    await h.audio(b"\x01\x00")  # hits the closed socket
    for _ in range(5):  # arrives during the reconnect: buffered
        await h.audio(b"\x02\x00")
    await h.settle()
    await h.audio(b"\x03\x00")

    assert dead.attempts == 1  # no send per frame into the dead socket
    assert h.connects == 2  # the first connect + exactly one reconnect
    # the frame that found the socket closed, the buffered ones, then live
    assert fresh.sent == [b"\x01\x00"] + [b"\x02\x00"] * 5 + [b"\x03\x00"]
    assert h.errors == []  # a recovered drop is not a pipeline error
    [warning] = h.log.having("warning", "connection lost")
    assert "code=1011" in warning and "RESOURCE_EXHAUSTED" in warning
    assert h.log.having("info", "reconnected (attempt 1/2)")


async def test_frames_after_the_drop_are_never_sent_to_the_dead_socket(monkeypatch):
    dead = FakeSocket(fail_with=_closed())
    h = Harness(monkeypatch, [dead, None, None])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    await h.settle()
    for _ in range(50):
        await h.audio(b"\x02\x00")
    assert dead.attempts == 1
    assert not any("Error sending audio" in e for e in h.errors)


async def test_the_listener_ending_on_its_own_reconnects(monkeypatch):
    first, second = FakeSocket(), FakeSocket()
    h = Harness(monkeypatch, [first, second])

    async def listener_ends(svc):
        error = _closed()
        for cb in first.callbacks.get(sarvam_mod.EventType.ERROR, []):
            cb(error)  # the SDK reports the failure as an ERROR event

    monkeypatch.setattr(SarvamSTTService, "_receive_task_handler", listener_ends)
    await h.svc._connect()
    await h.svc._receive_task_handler()
    await h.settle()

    assert h.connects == 2
    assert h.svc._socket_client is second
    [warning] = h.log.having("warning", "listener ended")
    assert "code=1011" in warning


async def test_our_own_disconnect_does_not_reconnect(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket(), FakeSocket()])

    async def listen_forever(svc):
        await asyncio.Event().wait()

    monkeypatch.setattr(SarvamSTTService, "_receive_task_handler", listen_forever)
    await h.svc._connect()
    h.svc._receive_task = asyncio.get_running_loop().create_task(
        h.svc._receive_task_handler()
    )
    await asyncio.sleep(0)
    await h.svc._disconnect()  # cancels the listener, as at call end
    await asyncio.sleep(0.05)

    assert h.connects == 1
    assert h.svc._recovery_task is None
    assert h.log.having("warning", "connection lost") == []


async def test_a_drop_after_the_call_stops_does_not_reconnect(monkeypatch):
    dead = FakeSocket(fail_with=_closed())
    h = Harness(monkeypatch, [dead, FakeSocket()])
    await h.svc._connect()
    await h.svc._stop_recovery()  # what stop()/cancel() do first
    await h.audio(b"\x01\x00")
    await asyncio.sleep(0.05)
    assert h.connects == 1


async def test_a_second_drop_while_reconnecting_does_not_start_another(monkeypatch):
    dead = FakeSocket(fail_with=_closed())
    h = Harness(monkeypatch, [dead, FakeSocket()])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    task = h.svc._recovery_task
    await h.svc._on_connection_lost("listener ended")  # the same drop, seen again
    assert h.svc._recovery_task is task
    await h.settle()
    assert h.connects == 2
    assert len(h.log.having("warning", "connection lost")) == 1


# ------------------------------------------------------- cap and call end


async def test_two_attempts_then_the_stt_unavailable_event_fires_once(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), None, None, None])
    fired: list[str] = []

    async def on_unavailable(_svc, reason):
        fired.append(reason)

    h.svc.add_event_handler(STT_UNAVAILABLE_EVENT, on_unavailable)
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    await h.settle()
    await asyncio.sleep(0.01)  # the event handler runs in its own task
    for _ in range(10):
        await h.audio(b"\x02\x00")

    assert h.connects == 3  # the first connect + 2 attempts, no third
    assert len(fired) == 1 and "reconnect attempts are used up" in fired[0]
    assert sum("attempts are used up" in e for e in h.errors) == 1
    await h.svc._on_connection_lost("send failed")
    await asyncio.sleep(0.01)
    assert len(fired) == 1


async def test_the_cap_counts_across_the_call_not_per_drop(monkeypatch):
    h = Harness(
        monkeypatch,
        [
            FakeSocket(fail_with=_closed()),
            FakeSocket(fail_with=_closed()),
            FakeSocket(fail_with=_closed()),
            FakeSocket(),
        ],
    )
    fired: list[str] = []
    h.svc.add_event_handler(
        STT_UNAVAILABLE_EVENT, lambda _svc, reason: fired.append(reason)
    )
    await h.svc._connect()
    for _ in range(3):
        await h.audio(b"\x01\x00")
        await h.settle()
    await asyncio.sleep(0.01)
    assert h.connects == 3
    assert len(fired) == 1


async def test_the_agent_ends_the_call_when_the_stt_is_gone(monkeypatch):
    import app.ai.voice.agents.breeze_buddy.agent as agent_mod

    ended: list[Any] = []

    async def fake_end_conversation(context, args):
        ended.append(context)

    monkeypatch.setattr(agent_mod, "end_conversation", fake_end_conversation)
    monkeypatch.setattr(agent_mod, "TemplateContext", lambda bot: bot)
    bot: Any = SimpleNamespace(
        conversation_ended=False,
        pending_transfer=None,
        lead=SimpleNamespace(outcome=None, metaData=None),
        _transcript_collector=None,
        _rtvi_processor=object(),
        _post_greeting_task=None,
        approval_manager=None,
    )
    events: list[tuple[str, Any]] = []

    async def emit(name, data=None):
        events.append((name, data))

    bot._emit_rtvi_event = emit
    bot._handle_unexpected_disconnect = lambda reason: (
        agent_mod.Agent._handle_unexpected_disconnect(bot, reason)
    )

    svc = build_sarvam_stt(
        SarvamConfig(api_key="k", model="saaras:v3", sample_rate=8000)
    )
    agent_mod.Agent._end_call_when_stt_is_lost(bot, svc)
    await svc._call_event_handler(STT_UNAVAILABLE_EVENT, "gone")
    await asyncio.sleep(0.01)

    assert ended == [bot]
    assert bot.lead.metaData["call_ended_by"] == "system"
    assert bot.lead.metaData["call_end_reason"] == "stt_unavailable"
    assert bot.lead.metaData["stt_unavailable"] is True
    assert bot.lead.outcome == agent_mod.DEFAULT_OUTCOME
    assert events == [("conversation-end", {"reason": "stt_unavailable"})]


@pytest.mark.parametrize(
    "meta, expected",
    [
        ({"call_ended_by": "agent"}, "success"),
        ({"call_ended_by": "system"}, "success"),  # idle timeout, unchanged
        ({"call_ended_by": "customer"}, "incomplete"),
        ({}, "incomplete"),
        (
            {"call_ended_by": "system", "stt_unavailable": True},
            "incomplete",
        ),
        (
            # labels set by an earlier path do not hide the outage
            {
                "call_ended_by": "agent",
                "call_end_reason": "done",
                "stt_unavailable": True,
            },
            "incomplete",
        ),
    ],
)
def test_a_consult_lost_to_the_stt_is_reported_incomplete(meta, expected):
    from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
        hold_transfer_status,
    )

    assert hold_transfer_status(meta) == expected


def test_a_stt_without_the_event_is_left_alone():
    import app.ai.voice.agents.breeze_buddy.agent as agent_mod

    bot: Any = SimpleNamespace()
    stt = SimpleNamespace()  # e.g. Soniox: no STT_UNAVAILABLE_EVENT
    agent_mod.Agent._end_call_when_stt_is_lost(bot, stt)


async def test_audio_from_a_failed_attempt_reaches_the_next_socket(monkeypatch):
    fresh = FakeSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), None, fresh])
    attempt_started = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep_and_note(delay, *a, **k):
        attempt_started.set()
        await real_sleep(delay, *a, **k)

    monkeypatch.setattr(sarvam_mod.asyncio, "sleep", sleep_and_note)
    await h.svc._connect()
    await h.audio(b"\x01\x00")  # the drop
    await asyncio.wait_for(attempt_started.wait(), timeout=2)
    await h.audio(b"\x02\x00")  # during attempt 1, which fails
    await _until(lambda: h.connects >= 2)
    await h.audio(b"\x03\x00")  # between or during attempt 2
    await h.settle()
    await h.audio(b"\x04\x00")

    assert h.connects == 3
    assert fresh.sent == [b"\x01\x00", b"\x02\x00", b"\x03\x00", b"\x04\x00"]


async def test_replayed_audio_stays_ahead_of_live_audio(monkeypatch):
    """A send that yields during the replay must not let a live frame jump
    ahead of the buffered ones."""

    class SlowSocket(FakeSocket):
        async def transcribe(self, audio, encoding, sample_rate):
            await asyncio.sleep(0)  # the websocket write yields
            await super().transcribe(audio, encoding, sample_rate)

    fresh = SlowSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), fresh])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    for i in range(2, 6):
        await h.audio(bytes([i, 0]))
    task = h.svc._recovery_task
    assert task is not None
    await _until(lambda: bool(fresh.sent))  # the replay has begun
    await h.audio(b"\x09\x00")  # live, mid-replay
    await h.settle()
    assert fresh.sent == [bytes([i, 0]) for i in range(1, 6)] + [b"\x09\x00"]


async def test_a_failed_first_connect_is_retried_too(monkeypatch):
    fresh = FakeSocket()
    h = Harness(monkeypatch, [None, fresh])
    await h.svc._connect()  # as at StartFrame
    assert h.svc._recovery_task is not None
    await h.audio(b"\x01\x00")
    await h.settle()
    assert h.connects == 2
    assert fresh.sent == [b"\x01\x00"]


async def test_a_first_connect_that_never_succeeds_ends_the_call(monkeypatch):
    h = Harness(monkeypatch, [None, None, None])
    fired: list[str] = []
    h.svc.add_event_handler(
        STT_UNAVAILABLE_EVENT, lambda _svc, reason: fired.append(reason)
    )
    await h.svc._connect()
    await h.settle()
    await asyncio.sleep(0.01)
    assert h.connects == 3
    assert len(fired) == 1


async def test_muted_audio_is_not_buffered_for_replay(monkeypatch):
    fresh = FakeSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), fresh])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    h.svc._muted = True
    await h.audio(b"\x02\x00")  # muted, during the reconnect
    h.svc._muted = False
    await h.audio(b"\x03\x00")
    await h.settle()
    assert fresh.sent == [b"\x01\x00", b"\x03\x00"]  # not the muted \x02


# ------------------------------------------------------------ turn state


async def test_a_drop_mid_speech_closes_the_open_user_speech(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_signal("START_SPEECH"))
    assert h.broadcast == [UserStartedSpeakingFrame]

    await h.audio(b"\x01\x00")
    await h.settle()

    assert h.broadcast == [UserStartedSpeakingFrame, UserStoppedSpeakingFrame]
    assert h.svc._sarvam_speech_open is False


async def test_our_own_reconnect_mid_speech_closes_the_open_speech(monkeypatch):
    """pipecat's settings reconnect (_disconnect + _connect) drops the old
    socket's END_SPEECH too; the flag must not stay stuck."""
    h = Harness(monkeypatch, [FakeSocket(), FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_signal("START_SPEECH"))
    await h.svc._disconnect()
    await h.svc._connect()
    assert h.broadcast == [UserStartedSpeakingFrame, UserStoppedSpeakingFrame]
    assert h.svc._sarvam_speech_open is False


async def test_a_drop_between_turns_broadcasts_nothing(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_signal("START_SPEECH"))
    await h.svc._handle_message(_signal("END_SPEECH"))
    await h.audio(b"\x01\x00")
    await h.settle()
    assert h.broadcast == [UserStartedSpeakingFrame, UserStoppedSpeakingFrame]


async def test_the_drop_warning_names_the_last_sarvam_session(monkeypatch):
    dead = FakeSocket(fail_with=_closed())
    h = Harness(monkeypatch, [dead, FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_data("haan", "req-1"))
    await h.audio(b"\x01\x00")
    await h.settle()
    [warning] = h.log.having("warning", "connection lost")
    assert "code=1011" in warning and "request_id=req-1" in warning


async def test_every_frame_of_the_outage_is_replayed(monkeypatch):
    """Nothing the caller says during the outage is dropped."""
    fresh = FakeSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), None, fresh])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    for i in range(2, 200):  # ~4 s of 20 ms frames across a failed attempt
        await h.audio(bytes([i % 256, 0]))
    await h.settle()
    assert fresh.sent == [b"\x01\x00"] + [bytes([i % 256, 0]) for i in range(2, 200)]


async def test_audio_sent_but_never_transcribed_is_replayed(monkeypatch):
    """Sarvam drops the half-heard segment with the socket; the start of the
    caller's sentence must reach the new socket too."""
    first = FakeSocket()
    fresh = FakeSocket()
    h = Harness(monkeypatch, [first, fresh])
    await h.svc._connect()
    await h.audio(b"\x0a\x00")  # answered by the transcript below
    await h.svc._handle_message(_data("haan"))
    await h.audio(b"\x0b\x00")  # "mera order" -- taken, never answered
    await h.audio(b"\x0c\x00")
    first.fail_with = _closed()
    await h.audio(b"\x0d\x00")  # "cancel karo" -- finds the socket closed
    await h.audio(b"\x0e\x00")
    await h.settle()
    assert fresh.sent == [b"\x0b\x00", b"\x0c\x00", b"\x0d\x00", b"\x0e\x00"]


async def test_unanswered_audio_is_bounded(monkeypatch):
    monkeypatch.setattr(sarvam_mod, "SARVAM_UNFINALIZED_MAX_SECS", 0.05)
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    for i in range(10):  # 20 ms frames
        frame = AudioRawFrame(audio=bytes([i]) * 320, sample_rate=8000, num_channels=1)
        await h.svc.process_audio_frame(frame, FrameDirection.DOWNSTREAM)
    assert [f.audio[0] for f, _ in h.svc._unanswered.take()] == [8, 9]


async def test_a_replay_that_drops_again_keeps_what_it_had_sent(monkeypatch):
    class DiesAfterTwo(FakeSocket):
        async def transcribe(self, audio, encoding, sample_rate):
            if self.attempts >= 2:
                self.fail_with = _closed()
            await super().transcribe(audio, encoding, sample_rate)

    second, third = DiesAfterTwo(), FakeSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), second, third])
    await h.svc._connect()
    for i in range(1, 5):
        await h.audio(bytes([i, 0]))
    await h.settle()
    await _until(lambda: h.svc._recovery_task is None and h.connects == 3)
    assert second.sent == [b"\x01\x00", b"\x02\x00"]
    # never answered by the second socket, so sent again, in order
    assert third.sent == [bytes([i, 0]) for i in range(1, 5)]


async def test_a_sarvam_error_message_is_logged(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    message = SimpleNamespace(
        type="error",
        data=SimpleNamespace(error=RESOURCE_EXHAUSTED, code="1011"),
        dict=lambda: {"type": "error"},
    )
    await h.svc._handle_message(message)
    [warning] = h.log.having("warning", "error message")
    assert "RESOURCE_EXHAUSTED" in warning


async def test_a_new_socket_forgets_the_old_session_id(monkeypatch):
    h = Harness(
        monkeypatch, [FakeSocket(), FakeSocket(fail_with=_closed()), FakeSocket()]
    )
    await h.svc._connect()
    await h.svc._handle_message(_data("haan", "req-1"))
    await h.svc._disconnect()
    await h.svc._connect()  # dies before its first transcript
    await h.audio(b"\x01\x00")
    await h.settle()
    [warning] = h.log.having("warning", "connection lost")
    assert "request_id=None" in warning


@pytest.mark.parametrize(
    "state", [{"conversation_ended": True}, {"pending_transfer": object()}]
)
async def test_an_ending_or_transferring_call_is_left_alone(monkeypatch, state):
    import app.ai.voice.agents.breeze_buddy.agent as agent_mod

    calls: list[str] = []

    async def disconnect(reason):
        calls.append(reason)

    bot: Any = SimpleNamespace(
        conversation_ended=False,
        pending_transfer=None,
        _rtvi_processor=None,
        _post_greeting_task=None,
        approval_manager=None,
    )
    for key, value in state.items():
        setattr(bot, key, value)
    bot._handle_unexpected_disconnect = disconnect
    svc = build_sarvam_stt(
        SarvamConfig(api_key="k", model="saaras:v3", sample_rate=8000)
    )
    agent_mod.Agent._end_call_when_stt_is_lost(bot, svc)
    await svc._call_event_handler(STT_UNAVAILABLE_EVENT, "gone")
    await asyncio.sleep(0.01)
    assert calls == []


async def test_a_hung_handshake_counts_as_a_failed_attempt(monkeypatch):
    monkeypatch.setattr(sarvam_mod, "SARVAM_CONNECT_TIMEOUT_SECS", 0.05)
    fresh = FakeSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), "hang", fresh])
    real = h._parent_connect

    async def connect_or_hang(svc):
        if h.sockets and h.sockets[0] == "hang":
            h.sockets.pop(0)
            h.connects += 1
            await asyncio.Event().wait()
        await real(svc)

    h._parent_connect = connect_or_hang
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    await h.settle()
    assert h.connects == 3  # the hung attempt gave way to the next one
    assert h.svc._socket_client is fresh


async def test_a_frame_whose_replay_hits_a_closed_socket_is_kept(monkeypatch):
    class DiesAfter(FakeSocket):
        async def transcribe(self, audio, encoding, sample_rate):
            if self.attempts >= 1:
                self.fail_with = _closed()
            await super().transcribe(audio, encoding, sample_rate)

    second, third = DiesAfter(), FakeSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), second, third])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    for i in range(2, 5):
        await h.audio(bytes([i, 0]))
    await h.settle()
    await _until(lambda: h.svc._recovery_task is None and h.connects == 3)
    await h.settle()
    assert second.sent == [b"\x01\x00"]
    # \x01 went to the second socket unanswered, \x02 hit its close
    assert third.sent == [b"\x01\x00", b"\x02\x00", b"\x03\x00", b"\x04\x00"]


async def test_an_end_already_in_flight_keeps_its_labels(monkeypatch):
    import app.ai.voice.agents.breeze_buddy.agent as agent_mod

    async def fake_end_conversation(context, args):
        pass

    monkeypatch.setattr(agent_mod, "end_conversation", fake_end_conversation)
    monkeypatch.setattr(agent_mod, "TemplateContext", lambda bot: bot)
    bot: Any = SimpleNamespace(
        conversation_ended=False,
        lead=SimpleNamespace(
            outcome="RESOLVED",
            metaData={"call_ended_by": "agent", "call_end_reason": "done"},
        ),
        _transcript_collector=None,
    )
    await agent_mod.Agent._handle_unexpected_disconnect(bot, "stt_unavailable")
    assert bot.lead.metaData == {
        "call_ended_by": "agent",
        "call_end_reason": "done",
        "stt_unavailable": True,  # the outage is still on record
    }
    assert bot.lead.outcome == "RESOLVED"


async def test_an_outage_that_recovers_reports_no_pipeline_error(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), None, FakeSocket()])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    await h.settle()
    assert h.connects == 3
    assert h.errors == []  # the failed attempt is a warning, not a call error
    assert h.log.having("warning", "connect attempt")


async def test_a_lost_outage_is_one_error_naming_the_close(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), None, None])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    await h.settle()
    [error] = h.errors
    assert "code=1011" in error and "used up" in error
    assert h.disconnects >= 3 and h.svc._socket_client is None


async def test_the_first_connect_is_capped_too(monkeypatch):
    monkeypatch.setattr(sarvam_mod, "SARVAM_CONNECT_TIMEOUT_SECS", 0.05)
    fresh = FakeSocket()
    h = Harness(monkeypatch, ["hang", fresh])
    real = h._parent_connect

    async def connect_or_hang(svc):
        if h.sockets and h.sockets[0] == "hang":
            h.sockets.pop(0)
            h.connects += 1
            await asyncio.Event().wait()
        await real(svc)

    h._parent_connect = connect_or_hang
    await asyncio.wait_for(h.svc._connect(), timeout=1)  # as at StartFrame
    await h.settle()
    assert h.svc._socket_client is fresh


async def test_the_frame_that_found_the_socket_closed_is_replayed(monkeypatch):
    fresh = FakeSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), fresh])
    await h.svc._connect()
    await h.audio(b"\x07\x00")  # its send hits the closed socket
    await h.audio(b"\x08\x00")
    await h.settle()
    assert fresh.sent == [b"\x07\x00", b"\x08\x00"]


async def test_flush_mode_holds_the_flush_until_the_replay_is_through(monkeypatch):
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    order: list[str] = []

    class Recording(FakeSocket):
        async def transcribe(self, audio, encoding, sample_rate):
            await super().transcribe(audio, encoding, sample_rate)
            order.append("audio")

        async def flush(self):
            order.append("flush")

    h = Harness(
        monkeypatch,
        [FakeSocket(fail_with=_closed()), Recording()],
        vad_signals=False,
    )
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    await h.audio(b"\x02\x00")
    await h.svc.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.0), FrameDirection.UPSTREAM
    )
    await h.settle()
    assert order == ["audio", "audio", "flush"]


async def test_a_flush_that_found_the_socket_closed_is_sent_after_the_replay(
    monkeypatch,
):
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    order: list[str] = []

    class Recording(FakeSocket):
        async def transcribe(self, audio, encoding, sample_rate):
            await super().transcribe(audio, encoding, sample_rate)
            order.append("audio")

        async def flush(self):
            order.append("flush")

    h = Harness(
        monkeypatch,
        [FakeSocket(fail_with=_closed()), Recording()],
        vad_signals=False,
    )
    await h.svc._connect()
    await h.svc.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.0), FrameDirection.UPSTREAM
    )  # the flush is the first send to find the socket closed
    await h.audio(b"\x02\x00")
    await h.settle()
    # the flush was owed before \x02 arrived, so it goes first
    assert order == ["flush", "audio"]


async def test_a_flush_into_a_socket_that_dies_is_owed_to_the_next(monkeypatch):
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    class DiesOnFlush(FakeSocket):
        async def flush(self):
            raise _closed()

    class Flushes(FakeSocket):
        flushed = 0

        async def flush(self):
            self.flushed += 1

    third = Flushes()
    h = Harness(
        monkeypatch,
        [FakeSocket(fail_with=_closed()), DiesOnFlush(), third],
        vad_signals=False,
    )
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    await h.svc.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.0), FrameDirection.UPSTREAM
    )
    await h.settle()
    await _until(lambda: h.svc._recovery_task is None and h.connects == 3)
    assert third.flushed == 1
    assert h.svc._connection_lost is False


async def test_other_errors_during_a_recovery_still_reach_the_call(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), FakeSocket()])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    assert h.svc._recovery_task is not None
    await h.svc.push_error(error_msg="Failed to handle message: boom")
    await h.settle()
    assert h.errors == ["Failed to handle message: boom"]


async def test_the_owed_flush_lands_between_the_audio_around_it(monkeypatch):
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    order: list[str] = []

    class Recording(FakeSocket):
        async def transcribe(self, audio, encoding, sample_rate):
            await super().transcribe(audio, encoding, sample_rate)
            order.append(f"audio{audio_id(audio)}")

        async def flush(self):
            order.append("flush")

    def audio_id(audio: str) -> int:
        return base64.b64decode(audio)[0]

    h = Harness(
        monkeypatch,
        [FakeSocket(fail_with=_closed()), Recording()],
        vad_signals=False,
    )
    await h.svc._connect()
    await h.audio(b"\x01\x00")  # end of utterance A (finds the socket closed)
    await h.svc.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.0), FrameDirection.UPSTREAM
    )
    await h.audio(b"\x02\x00")  # start of utterance B
    await h.settle()
    assert order == ["audio1", "flush", "audio2"]


async def test_a_late_failure_of_the_old_socket_spares_the_new_one(monkeypatch):
    old, new = FakeSocket(), FakeSocket()
    h = Harness(monkeypatch, [old, new])
    await h.svc._connect()
    await h.svc._disconnect()
    await h.svc._connect()  # now on the new socket
    await h.svc._on_connection_lost("send failed", _closed(), client=old)
    assert h.svc._connection_lost is False
    assert h.svc._recovery_task is None


async def test_a_failed_first_connect_is_reported_at_once(monkeypatch):
    """The /stt stream endpoint rejects a stream on this early error rather
    than telling the client it is ready; the retries stay quiet."""
    h = Harness(monkeypatch, [None, None, FakeSocket()])
    await h.svc._connect()
    assert h.errors == ["Failed to connect to Sarvam"]  # before any retry
    await h.settle()
    assert h.errors == ["Failed to connect to Sarvam"]  # the failed retry is a warning
    assert h.log.having("warning", "connect attempt")
    assert h.connects == 3


async def test_a_socket_healed_by_someone_else_is_not_torn_down(monkeypatch):
    healed = FakeSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), healed, FakeSocket()])
    await h.svc._connect()
    await h.audio(b"\x01\x00")  # the drop; recovery waits out its backoff
    await h.svc._disconnect()
    await h.svc._connect()  # pipecat's settings path reconnects meanwhile
    await h.audio(b"\x05\x00")  # still queued behind the reconnect guard
    await h.settle()
    assert h.svc._socket_client is healed
    assert h.connects == 2
    # nothing reached it out of order: the buffered audio, oldest first
    assert healed.sent == [b"\x01\x00", b"\x05\x00"]


async def test_audio_captured_unmuted_survives_a_later_mute(monkeypatch):
    """A mute that starts during the outage drops only the audio that
    arrives muted, whichever attempt succeeds."""
    fresh = FakeSocket()
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), None, fresh])
    await h.svc._connect()
    await h.audio(b"\x01\x00")  # the drop
    await h.audio(b"\x02\x00")  # captured unmuted
    h.svc._muted = True
    await h.audio(b"\x03\x00")  # muted: dropped
    await _until(lambda: h.connects >= 2)  # attempt 1 fails while muted
    h.svc._muted = False
    await h.audio(b"\x04\x00")
    await h.settle()
    assert fresh.sent == [b"\x01\x00", b"\x02\x00", b"\x04\x00"]


async def test_a_failed_flush_mid_recovery_is_replayed_after_its_audio(monkeypatch):
    """The listener saw the drop first and the recovery is already waiting
    out its backoff when the flush finds the socket closed."""
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    order: list[str] = []

    class Recording(FakeSocket):
        async def transcribe(self, audio, encoding, sample_rate):
            await super().transcribe(audio, encoding, sample_rate)
            order.append(f"audio{base64.b64decode(audio)[0]}")

        async def flush(self):
            order.append("flush")

    dead = FakeSocket(fail_with=_closed())
    h = Harness(monkeypatch, [dead, Recording()], vad_signals=False)
    await h.svc._connect()
    await h.audio(b"\x01\x00")  # finds the socket closed; recovery starts
    await asyncio.sleep(0)  # the recovery enters its backoff
    await h.svc.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.0), FrameDirection.UPSTREAM
    )
    await h.audio(b"\x02\x00")
    await h.settle()
    assert order == ["audio1", "flush", "audio2"]


async def test_a_first_connect_that_times_out_is_reported_at_once(monkeypatch):
    monkeypatch.setattr(sarvam_mod, "SARVAM_CONNECT_TIMEOUT_SECS", 0.05)
    h = Harness(monkeypatch, ["hang", FakeSocket()])
    real = h._parent_connect

    async def connect_or_hang(svc):
        if h.sockets and h.sockets[0] == "hang":
            h.sockets.pop(0)
            h.connects += 1
            await asyncio.Event().wait()
        await real(svc)

    h._parent_connect = connect_or_hang
    await h.svc._connect()
    [error] = h.errors  # the /stt stream endpoint rejects on this
    assert "timed out" in error
    await h.settle()
    assert h.errors == [error]


def test_pipecats_keepalive_keeps_a_quiet_socket_open():
    svc = build_sarvam_stt(
        SarvamConfig(api_key="k", model="saaras:v3", sample_rate=8000)
    )
    assert svc._keepalive_timeout == sarvam_mod.SARVAM_KEEPALIVE_SECS
    svc._socket_client = FakeSocket()
    assert svc._is_keepalive_ready() is True
    svc._connection_lost = True
    assert svc._is_keepalive_ready() is False  # not into a dead socket


async def test_the_give_up_names_why_the_reconnects_failed(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket(fail_with=_closed()), None, None])
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    await h.settle()
    [error] = h.errors
    assert "last attempt: Failed to connect to Sarvam" in error


async def test_pending_approvals_are_denied_when_the_stt_is_lost(monkeypatch):
    import app.ai.voice.agents.breeze_buddy.agent as agent_mod

    denied: list[str] = []
    calls: list[str] = []

    async def disconnect(reason):
        calls.append(reason)

    bot: Any = SimpleNamespace(
        conversation_ended=False,
        pending_transfer=None,
        _rtvi_processor=None,
        _post_greeting_task=None,
        approval_manager=SimpleNamespace(deny_all=denied.append),
    )
    bot._handle_unexpected_disconnect = disconnect
    svc = build_sarvam_stt(
        SarvamConfig(api_key="k", model="saaras:v3", sample_rate=8000)
    )
    agent_mod.Agent._end_call_when_stt_is_lost(bot, svc)
    await svc._call_event_handler(STT_UNAVAILABLE_EVENT, "gone")
    await asyncio.sleep(0.01)
    assert denied == ["stt_unavailable"] and calls == ["stt_unavailable"]


async def test_an_stt_outage_during_the_goodbye_mute_keeps_the_llms_reason(
    monkeypatch,
):
    """end_conversation_global awaits mute_stt; an STT give-up landing in
    that await must not relabel an end the LLM already decided."""
    import app.ai.voice.agents.breeze_buddy.agent as agent_mod
    import app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation_global as ecg

    lead = SimpleNamespace(outcome=None, metaData={})
    bot: Any = SimpleNamespace(
        conversation_ended=False, lead=lead, _transcript_collector=None
    )

    async def outage_during_mute(context, args):
        # the STT gives up right here, before end_conversation runs
        await agent_mod.Agent._handle_unexpected_disconnect(bot, "stt_unavailable")

    async def fake_end(context, args):
        return {}

    monkeypatch.setattr(ecg, "mute_stt", outage_during_mute)
    monkeypatch.setattr(ecg, "end_conversation", fake_end)
    monkeypatch.setattr(agent_mod, "end_conversation", fake_end)
    monkeypatch.setattr(agent_mod, "TemplateContext", lambda b: b)
    context: Any = SimpleNamespace(lead=lead, call_sid="CA-test")
    await ecg.end_conversation_global(context, {"reason": "order_confirmed"})
    assert lead.metaData["call_end_reason"] == "order_confirmed"
    assert lead.metaData["stt_unavailable"] is True  # the outage is on record


async def test_a_frame_sent_while_the_drop_is_declared_is_replayed(monkeypatch):
    """Its send returns fine, but the listener has meanwhile declared the
    socket dead: the frame went nowhere and must be replayed in order."""
    fresh = FakeSocket()

    class Racing(FakeSocket):
        async def transcribe(self, audio, encoding, sample_rate):
            await super().transcribe(audio, encoding, sample_rate)
            if base64.b64decode(audio)[0] == 5:
                await h.svc._on_connection_lost("listener ended", client=self)

    h = Harness(monkeypatch, [Racing(), fresh])
    await h.svc._connect()
    await h.audio(b"\x04\x00")
    await h.audio(b"\x05\x00")  # in flight as the drop is declared
    await h.audio(b"\x06\x00")
    await h.settle()
    assert fresh.sent == [b"\x04\x00", b"\x05\x00", b"\x06\x00"]


async def test_a_flushed_segment_is_replayed_with_its_flush(monkeypatch):
    """Flush mode: the flush reached the old socket but its transcript never
    came; the new socket must get the audio and the flush again."""
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    order: list[str] = []

    class Recording(FakeSocket):
        async def transcribe(self, audio, encoding, sample_rate):
            await super().transcribe(audio, encoding, sample_rate)
            order.append(f"audio{base64.b64decode(audio)[0]}")

        async def flush(self):
            order.append("flush")

    first = FakeSocket()
    h = Harness(monkeypatch, [first, Recording()], vad_signals=False)
    await h.svc._connect()
    await h.audio(b"\x01\x00")
    await h.svc.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.0), FrameDirection.UPSTREAM
    )  # flush sent fine
    first.fail_with = _closed()
    await h.audio(b"\x02\x00")  # the drop, before any transcript
    await h.settle()
    assert order == ["audio1", "flush", "audio2"]


async def test_a_transcript_answers_only_its_own_segment(monkeypatch):
    """The caller starts B before A's transcript lands: that transcript must
    not mark B's start answered."""
    first, fresh = FakeSocket(), FakeSocket()
    h = Harness(monkeypatch, [first, fresh])
    monkeypatch.setattr(sarvam_mod, "SARVAM_END_SPEECH_SLACK_SECS", 0.0)
    await h.svc._connect()
    await h.audio(b"\x0a\x00")  # A
    await h.svc._handle_message(_signal("END_SPEECH"))
    await h.audio(b"\x0b\x00")  # B starts
    await h.svc._handle_message(_data("A's text"))
    first.fail_with = _closed()
    await h.audio(b"\x0c\x00")
    await h.settle()
    assert fresh.sent == [b"\x0b\x00", b"\x0c\x00"]


async def test_a_transcript_read_just_before_the_drop_is_not_replayed(monkeypatch):
    """Sarvam sends the final then closes: the queued transcript must mark its
    audio answered before the drop takes the unanswered audio."""
    first, fresh = FakeSocket(), FakeSocket()
    h = Harness(monkeypatch, [first, fresh])
    await h.svc._connect()
    await h.audio(b"\x0a\x00")
    # the transcript is queued as its own task, the listener then ends
    asyncio.get_running_loop().create_task(h.svc._handle_message(_data("haan")))
    await h.svc._on_connection_lost("listener ended", client=first)
    await h.audio(b"\x0b\x00")
    await h.settle()
    assert fresh.sent == [b"\x0b\x00"]  # \x0a was answered, not sent twice
