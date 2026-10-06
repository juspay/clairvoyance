"""Sarvam STT latency: the turn-close floor, the session id in the logs, and
the per-template silence window.

pipecat 1.1.0's Sarvam segments are never marked finalized, so every turn
waits out the 1.17 s STT safety timer though Sarvam's final arrived sooner.
These lock in the finalized mark and its kill switch, the request_id log
line, and negative_frames_count / negative_frames_window. No network.
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
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.sarvam.stt import SarvamSTTService
from pydantic import ValidationError

import app.ai.voice.agents.breeze_buddy.stt as bb_stt_mod
import app.ai.voice.stt.sarvam as sarvam_mod
from app.ai.voice.agents.breeze_buddy.stt import create_stt_from_config
from app.ai.voice.agents.breeze_buddy.template.types import STTConfiguration
from app.ai.voice.stt import SarvamConfig, build_sarvam_stt
from app.core.config import static


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
        harness = self

        async def parent_connect(svc):
            await harness._parent_connect(svc)

        async def parent_disconnect(svc):
            await harness._parent_disconnect(svc)

        monkeypatch.setattr(SarvamSTTService, "_connect", parent_connect)
        monkeypatch.setattr(SarvamSTTService, "_disconnect", parent_disconnect)

        async def capture_push(svc, frame, direction=FrameDirection.DOWNSTREAM):
            self.pushed.append(frame)

        # Below STTService.push_frame, which applies pipecat's finalized mark.
        monkeypatch.setattr(FrameProcessor, "push_frame", capture_push)
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

        async def push_error(error_msg, exception=None, fatal=False):
            self.errors.append(error_msg)

        async def broadcast_frame(frame_cls, **kwargs):
            self.broadcast.append(frame_cls)

        async def broadcast_interruption():
            pass

        svc.create_task = create_task
        svc.cancel_task = cancel_task
        svc.push_error = push_error
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


# ------------------------------------------------------------- finalized


@pytest.mark.parametrize(
    "vad_signals, kill_switch, expected",
    [(True, True, True), (True, False, False), (False, True, False)],
)
async def test_segments_are_finalized_only_with_sarvams_vad_and_the_switch(
    monkeypatch, vad_signals, kill_switch, expected
):
    h = Harness(
        monkeypatch,
        [FakeSocket()],
        vad_signals=vad_signals,
        finalize_on_segment=kill_switch,
    )
    await h.svc._connect()
    await h.svc._handle_message(_data("haan ji"))
    [frame] = _transcripts(h)
    assert frame.finalized is expected


async def _vad_start(h: "Harness") -> None:
    await h.svc.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.UPSTREAM)


async def _vad_stop(h: "Harness") -> None:
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    await h.svc.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.3), FrameDirection.UPSTREAM
    )


async def test_an_earlier_piece_is_not_finalized_while_the_caller_talks(monkeypatch):
    """Sarvam's text for piece A landing while the caller is already saying
    B must not close the turn at B's first pause."""
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    await _vad_start(h)  # A
    await h.svc._handle_message(_signal("START_SPEECH"))
    await h.svc._handle_message(_data("mujhe kal"))  # Sarvam: still speaking
    await _vad_stop(h)
    await h.svc._handle_message(_signal("END_SPEECH"))
    await _vad_start(h)  # the caller resumes after Sarvam ended piece A
    await h.svc._handle_message(_data("nahi chahiye"))
    assert [f.finalized for f in _transcripts(h)] == [False, False]


async def test_a_segment_is_not_finalized_while_our_vad_hears_speech(monkeypatch):
    """Our VAD still hears speech when Sarvam's text lands (a pause shorter
    than our stop_secs): the caller may be going on, so no mark. pipecat
    clears a mark only on the next VAD start, so one set here would close
    the whole turn on this piece at the eventual stop."""
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    await _vad_start(h)
    await h.svc._handle_message(_signal("START_SPEECH"))
    await h.svc._handle_message(_signal("END_SPEECH"))
    await h.svc._handle_message(_data("mujhe kal"))
    await _vad_stop(h)
    await h.svc._handle_message(_data("nahi chahiye"))
    assert [f.finalized for f in _transcripts(h)] == [False, True]


async def test_a_short_pause_does_not_close_the_turn_on_its_first_piece():
    """The scenario end to end through pipecat's stop strategy: piece A's
    text lands while our VAD still hears the caller, who goes on with B.
    At the VAD stop the turn must still wait for B's text."""
    from pipecat.frames.frames import STTMetadataFrame, VADUserStoppedSpeakingFrame
    from pipecat.services.stt_latency import SARVAM_TTFS_P99
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
    from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

    tm = TaskManager()
    tm.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    fired = asyncio.Event()
    strat = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0)
    strat.add_event_handler("on_user_turn_stopped", lambda *_a, **_k: fired.set())
    await strat.setup(tm)
    await strat.process_frame(
        STTMetadataFrame(service_name="sarvam", ttfs_p99_latency=SARVAM_TTFS_P99)
    )
    await strat.process_frame(VADUserStartedSpeakingFrame())
    # Piece A, unmarked because our VAD is still speaking.
    await strat.process_frame(TranscriptionFrame("mujhe kal", "", "now"))
    await strat.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.3))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not fired.is_set()
    assert strat._stt_timeout_task is not None
    await strat.cleanup()


async def test_an_empty_segment_leaves_no_mark_for_the_next(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket()], finalize_on_segment=True)
    await h.svc._connect()
    await h.svc._handle_message(_data("   "))  # nothing pushed
    h.svc._finalize_on_segment = False
    await h.svc._handle_message(_data("haan ji"))
    assert [f.finalized for f in _transcripts(h)] == [False]


async def test_a_finalized_segment_ends_the_turn_without_the_stt_timer():
    """pipecat's own stop strategy: with the mark, the turn closes when the
    text lands; without it, it waits on the STT safety timer
    (SARVAM_TTFS_P99 - stop_secs). Checked by which timer runs, not by the
    clock."""
    from pipecat.frames.frames import STTMetadataFrame, VADUserStoppedSpeakingFrame
    from pipecat.services.stt_latency import SARVAM_TTFS_P99
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
    from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

    async def after_the_text(finalized: bool) -> tuple[bool, bool]:
        tm = TaskManager()
        tm.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
        fired = asyncio.Event()
        strat = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0)
        strat.add_event_handler("on_user_turn_stopped", lambda *_a, **_k: fired.set())
        await strat.setup(tm)
        await strat.process_frame(
            STTMetadataFrame(service_name="sarvam", ttfs_p99_latency=SARVAM_TTFS_P99)
        )
        await strat.process_frame(VADUserStartedSpeakingFrame())
        await strat.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.3))
        await strat.process_frame(
            TranscriptionFrame("haan ji", "", "now", finalized=finalized)
        )
        for _ in range(5):
            await asyncio.sleep(0)
        waiting_on_stt = strat._stt_timeout_task is not None
        stopped = fired.is_set()
        await strat.cleanup()
        return stopped, waiting_on_stt

    assert await after_the_text(True) == (True, False)
    assert await after_the_text(False) == (False, True)


async def test_routing_reads_the_kill_switch(monkeypatch):
    seen: dict[str, Any] = {}

    def fake(config):
        seen["config"] = config
        return "svc"

    async def switch_off():
        return False

    monkeypatch.setattr(bb_stt_mod, "build_sarvam_stt", fake)
    monkeypatch.setattr(bb_stt_mod, "BB_SARVAM_STT_FINALIZE_ON_SEGMENT", switch_off)
    await create_stt_from_config(STTConfiguration(provider="sarvam"))
    assert seen["config"].finalize_on_segment is False


async def test_a_new_socket_does_not_inherit_an_open_speech(monkeypatch):
    """A reconnect of our own (a settings change) loses the old socket's
    END_SPEECH; the next segment must still be finalized."""
    h = Harness(monkeypatch, [FakeSocket(), FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_signal("START_SPEECH"))
    await h.svc._disconnect()
    await h.svc._connect()
    await h.svc._handle_message(_data("haan ji"))
    [frame] = _transcripts(h)
    assert frame.finalized is True


# ------------------------------------------------------------ request_id


async def test_the_request_id_is_logged_once_per_connection(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket(), FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_data("haan", "req-1"))
    await h.svc._handle_message(_data("ji", "req-1"))
    assert len(h.log.having("info", "request_id=req-1")) == 1

    await h.svc._disconnect()
    await h.svc._connect()  # a new connection, a new Sarvam session
    await h.svc._handle_message(_data("theek hai", "req-2"))
    await h.svc._handle_message(_data("bilkul", "req-2"))
    assert len(h.log.having("info", "request_id=req-2")) == 1


async def test_a_message_without_a_request_id_logs_nothing(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_data("haan", None))
    assert h.log.having("info", "request_id") == []


# ------------------------------------------------------- silence window


def _sarvam(**block: Any) -> STTConfiguration:
    return STTConfiguration.model_validate({"provider": "sarvam", "sarvam": block})


def test_the_silence_window_reaches_the_service_settings():
    svc = build_sarvam_stt(
        SarvamConfig(
            api_key="k",
            model="saaras:v3",
            sample_rate=8000,
            negative_frames_count=12,
            negative_frames_window=16,
        )
    )
    assert svc._settings.negative_frames_count == 12
    assert svc._settings.negative_frames_window == 16


async def _connect_kwargs(monkeypatch, svc) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(
        svc._sarvam_client.speech_to_text_streaming, "connect", fake_connect
    )

    async def push_error(error_msg, exception=None, fatal=False):
        pass

    svc.push_error = push_error
    svc._sample_rate = 8000
    await SarvamSTTService._connect(svc)
    return seen


async def test_the_silence_window_is_sent_on_connect(monkeypatch):
    svc = build_sarvam_stt(
        SarvamConfig(
            api_key="k",
            model="saaras:v3",
            sample_rate=8000,
            negative_frames_count=12,
            negative_frames_window=16,
        )
    )
    kwargs = await _connect_kwargs(monkeypatch, svc)
    assert kwargs["negative_frames_count"] == "12"
    assert kwargs["negative_frames_window"] == "16"


async def test_an_unset_silence_window_sends_nothing(monkeypatch):
    svc = build_sarvam_stt(
        SarvamConfig(api_key="k", model="saaras:v3", sample_rate=8000)
    )
    kwargs = await _connect_kwargs(monkeypatch, svc)
    assert "negative_frames_count" not in kwargs
    assert "negative_frames_window" not in kwargs


async def test_routing_passes_the_template_silence_window(monkeypatch):
    seen: dict[str, Any] = {}

    def fake(config):
        seen["config"] = config
        return "svc"

    monkeypatch.setattr(bb_stt_mod, "build_sarvam_stt", fake)
    await create_stt_from_config(
        _sarvam(model="saaras:v3", negative_frames_count=12, negative_frames_window=16)
    )
    assert (
        seen["config"].negative_frames_count,
        seen["config"].negative_frames_window,
    ) == (12, 16)


@pytest.mark.parametrize(
    "block, message",
    [
        ({"negative_frames_count": 12}, "together"),
        ({"negative_frames_window": 16}, "together"),
        (
            {"negative_frames_count": 17, "negative_frames_window": 16},
            "must not exceed",
        ),
        ({"negative_frames_count": 0, "negative_frames_window": 16}, "greater than"),
        ({"negative_frames_count": 2, "negative_frames_window": 2}, "greater than"),
        ({"negative_frames_count": 12, "negative_frames_window": 65}, "less than"),
        (
            {
                "model": "saarika:v2.5",
                "negative_frames_count": 12,
                "negative_frames_window": 16,
            },
            "saaras:v3 only",
        ),
        (
            {
                "model": "saaras:v2.5",
                "negative_frames_count": 12,
                "negative_frames_window": 16,
            },
            "saaras:v3 only",
        ),
    ],
)
def test_a_bad_silence_window_is_refused_at_parse(block, message):
    with pytest.raises(ValidationError, match=message):
        _sarvam(**block)


def test_a_silence_window_with_the_default_model_parses():
    cfg = _sarvam(negative_frames_count=12, negative_frames_window=16)
    assert cfg.sarvam is not None and cfg.sarvam.negative_frames_window == 16


def test_a_default_model_without_vad_params_drops_the_window_at_build(monkeypatch):
    log = Recorder()
    monkeypatch.setattr(sarvam_mod, "logger", log)
    svc = build_sarvam_stt(
        SarvamConfig(
            api_key="k",
            model="saarika:v2.5",
            sample_rate=8000,
            negative_frames_count=12,
            negative_frames_window=16,
        )
    )
    assert svc._settings.negative_frames_count is None
    assert log.having("warning", "silence window")


async def test_a_segment_handled_while_our_vad_start_is_processed_sees_it(
    monkeypatch,
):
    """pipecat's VAD-start handler can yield before it records the speech; a
    segment handled in that gap must already count the caller as resumed."""
    from pipecat.services.stt_service import STTService

    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_signal("START_SPEECH"))
    await _vad_stop(h)
    await h.svc._handle_message(_signal("END_SPEECH"))
    seen: list[bool] = []

    async def yielding_parent(svc, frame):
        seen.append(svc._should_finalize())  # a data task running in the gap

    monkeypatch.setattr(
        STTService, "_handle_vad_user_started_speaking", yielding_parent
    )
    await _vad_start(h)
    assert seen == [False]


async def test_each_segment_keeps_its_own_mark_when_tasks_interleave(monkeypatch):
    """Two transcripts in flight at once: each frame is judged on its own,
    when it is pushed, with no mark shared between them."""
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    gate = asyncio.Event()
    real = SarvamSTTService._handle_message

    async def slow_parent(svc, message):
        if message.data.transcript == "first":
            await gate.wait()  # still in flight when the second is pushed
        await real(svc, message)

    monkeypatch.setattr(SarvamSTTService, "_handle_message", slow_parent)
    first = asyncio.get_running_loop().create_task(
        h.svc._handle_message(_data("first"))
    )
    await asyncio.sleep(0)
    await h.svc._handle_message(_data("second"))  # pushed while the caller is quiet
    await _vad_start(h)  # the caller speaks before "first" is pushed
    gate.set()
    await first
    marks = {f.text: f.finalized for f in _transcripts(h)}
    assert marks == {"first": False, "second": True}


def test_high_vad_sensitivity_with_a_window_is_warned_about(monkeypatch):
    log = Recorder()
    monkeypatch.setattr(sarvam_mod, "logger", log)
    build_sarvam_stt(
        SarvamConfig(
            api_key="k",
            model="saaras:v3",
            sample_rate=8000,
            high_vad_sensitivity=True,
            negative_frames_count=12,
            negative_frames_window=16,
        )
    )
    assert log.having("warning", "high_vad_sensitivity")


async def test_a_quick_resume_before_sarvams_end_speech_is_caught(monkeypatch):
    """Telephony: the caller resumes B before Sarvam's END_SPEECH for A
    reaches us. A's text must not close the turn mid-sentence."""
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    await _vad_start(h)  # A
    await h.svc._handle_message(_signal("START_SPEECH"))
    await h.svc.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.3), FrameDirection.UPSTREAM
    )
    await _vad_start(h)  # B, before Sarvam's END_SPEECH for A
    await h.svc._handle_message(_signal("END_SPEECH"))
    await h.svc._handle_message(_data("mujhe kal"))
    assert [f.finalized for f in _transcripts(h)] == [False]


def test_a_blank_model_with_a_window_parses_and_resolves_later():
    cfg = _sarvam(model="", negative_frames_count=12, negative_frames_window=16)
    assert cfg.sarvam is not None and cfg.sarvam.negative_frames_count == 12


async def test_sarvam_hearing_the_speech_first_still_finalizes(monkeypatch):
    """Our VAD reports a start only after start_secs, so Sarvam's
    START_SPEECH often lands first; that is the same speech, not a resume."""
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_signal("START_SPEECH"))
    await _vad_start(h)
    await h.svc._handle_message(_signal("END_SPEECH"))
    await _vad_stop(h)
    await h.svc._handle_message(_data("haan ji"))
    assert [f.finalized for f in _transcripts(h)] == [True]


async def test_a_new_request_id_is_logged_when_it_changes(monkeypatch):
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    for request_id in ("req-1", "req-1", "req-2"):
        await h.svc._handle_message(_data("haan", request_id))
    assert len(h.log.having("info", "request_id=req-1")) == 1
    assert len(h.log.having("info", "request_id=req-2")) == 1


def test_a_sarvam_window_under_another_provider_is_refused():
    with pytest.raises(ValidationError, match="only to provider='sarvam'"):
        STTConfiguration.model_validate(
            {
                "provider": "soniox",
                "sarvam": {"negative_frames_count": 12, "negative_frames_window": 16},
            }
        )


async def test_a_vad_start_during_pipecats_handler_is_seen_at_push(monkeypatch):
    """The mark is decided when the frame is pushed, so a VAD start handled
    while pipecat's message handler awaits still blocks it."""
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    real = SarvamSTTService._handle_transcription

    async def vad_starts_meanwhile(svc, *args, **kwargs):
        await _vad_start(h)
        return await real(svc, *args, **kwargs)

    monkeypatch.setattr(SarvamSTTService, "_handle_transcription", vad_starts_meanwhile)
    await h.svc._handle_message(_data("haan ji"))
    assert [f.finalized for f in _transcripts(h)] == [False]


async def test_a_vad_start_already_on_its_way_is_seen_first(monkeypatch):
    """Our VAD's start reaches the STT a hop after the aggregator acts on it;
    the segment is judged only after that hop."""
    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    loop = asyncio.get_running_loop()
    data_task = loop.create_task(h.svc._handle_message(_data("mujhe kal")))
    loop.create_task(_vad_start(h))  # queued just behind the transcript
    await data_task
    assert [f.finalized for f in _transcripts(h)] == [False]


async def test_a_real_sarvam_message_through_stock_pipecat_is_finalized(monkeypatch):
    """Pins the wrapper to pipecat 1.1.0's own message path with the SDK's
    real message types, so an upgrade that changes either fails here."""
    from pydantic import TypeAdapter
    from sarvamai.types import SpeechToTextStreamingResponse

    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    adapter = TypeAdapter(SpeechToTextStreamingResponse)
    for raw in (
        {"type": "events", "data": {"signal_type": "START_SPEECH", "occured_at": 0.0}},
        {"type": "events", "data": {"signal_type": "END_SPEECH", "occured_at": 1.0}},
        {
            "type": "data",
            "data": {
                "request_id": "req-real",
                "transcript": "haan ji",
                "language_code": "hi-IN",
                "metrics": {"audio_duration": 1.0, "processing_latency": 0.1},
            },
        },
    ):
        await h.svc._handle_message(adapter.validate_python(raw))
    [frame] = _transcripts(h)
    assert frame.text == "haan ji" and frame.finalized is True
    assert h.log.having("info", "request_id=req-real")


async def test_with_the_switch_off_the_frame_order_is_unchanged(monkeypatch):
    """No yield before the text with the kill switch off: a START_SPEECH
    right behind it is handled after it, as before this change."""
    h = Harness(monkeypatch, [FakeSocket()], finalize_on_segment=False)
    await h.svc._connect()
    order: list[str] = []
    real_push = h.svc.push_frame

    async def note(frame, direction=FrameDirection.DOWNSTREAM):
        order.append(type(frame).__name__)
        await real_push(frame, direction)

    h.svc.push_frame = note
    real_broadcast = h.svc.broadcast_frame

    async def note_broadcast(frame_cls, **kwargs):
        order.append(frame_cls.__name__)
        await real_broadcast(frame_cls, **kwargs)

    h.svc.broadcast_frame = note_broadcast
    loop = asyncio.get_running_loop()
    text = loop.create_task(h.svc._handle_message(_data("haan")))
    speech = loop.create_task(h.svc._handle_message(_signal("START_SPEECH")))
    await text
    await speech
    assert order.index("TranscriptionFrame") < order.index("UserStartedSpeakingFrame")


def test_a_template_window_wins_over_high_vad_sensitivity(monkeypatch):
    log = Recorder()
    monkeypatch.setattr(sarvam_mod, "logger", log)
    svc = build_sarvam_stt(
        SarvamConfig(
            api_key="k",
            model="saaras:v3",
            sample_rate=8000,
            high_vad_sensitivity=True,
            negative_frames_count=12,
            negative_frames_window=16,
        )
    )
    assert svc._settings.high_vad_sensitivity is False
    assert svc._settings.negative_frames_count == 12
    assert log.having("warning", "high_vad_sensitivity ignored")


def test_high_vad_sensitivity_alone_is_left_as_set():
    svc = build_sarvam_stt(
        SarvamConfig(
            api_key="k", model="saaras:v3", sample_rate=8000, high_vad_sensitivity=True
        )
    )
    assert svc._settings.high_vad_sensitivity is True


def test_the_batch_endpoint_refuses_a_silence_window():
    from app.schemas.breeze_buddy.stt import TranscriptionRequest

    with pytest.raises(ValidationError, match="streaming STT only"):
        TranscriptionRequest.model_validate(
            {
                "provider": "sarvam",
                "sarvam": {"negative_frames_count": 12, "negative_frames_window": 16},
            }
        )


async def test_routing_reads_the_global_language_only_when_needed(monkeypatch):
    calls: list[str] = []
    seen: dict[str, Any] = {}

    async def language():
        calls.append("language")
        return "hi-IN"

    def fake(config):
        seen["config"] = config
        return "svc"

    monkeypatch.setattr(bb_stt_mod, "build_sarvam_stt", fake)
    monkeypatch.setattr(bb_stt_mod, "BB_SARVAM_STT_LANGUAGE_CODE", language)
    await create_stt_from_config(_sarvam(model="saaras:v3"))
    assert calls == [] and seen["config"].language_code is None
    await create_stt_from_config(_sarvam(model="saarika:v2.5"))
    assert calls == ["language"] and seen["config"].language_code == "hi-IN"
    await create_stt_from_config(_sarvam(model="saarika:v2.5", language_code="ta-IN"))
    assert calls == ["language"] and seen["config"].language_code == "ta-IN"


@pytest.mark.parametrize("hops", [2, 3])  # agent mode, stream mode
async def test_a_vad_start_crossing_real_processor_queues_is_seen(monkeypatch, hops):
    """The VAD start travels upstream through real pipecat processor queues
    (transcription_gate, transcript_collector in stream mode, then the STT's
    own input queue) before the STT sees it. A segment handled just after the
    aggregator sent it must not be marked final."""
    from pipecat.clocks.system_clock import SystemClock
    from pipecat.processors.frame_processor import FrameProcessorSetup
    from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

    h = Harness(monkeypatch, [FakeSocket()])
    await h.svc._connect()
    await h.svc._handle_message(_signal("START_SPEECH"))
    await _vad_stop(h)
    await h.svc._handle_message(_signal("END_SPEECH"))

    from pipecat.frames.frames import StartFrame

    class Forward(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if not isinstance(frame, StartFrame):
                # what push_frame does upstream (the harness stubs push_frame)
                assert self._prev is not None
                await self._prev.queue_frame(frame, direction)

    class IntoStt(FrameProcessor):
        """Stands in for the STT's own input queue: hands the frame to it."""

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if not isinstance(frame, StartFrame):
                await h.svc.process_frame(frame, direction)

    tm = TaskManager()
    tm.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    stt_queue = IntoStt()
    chain = [Forward() for _ in range(hops - 1)]
    upstream = stt_queue
    for p in chain:  # stt_queue <- chain[0] <- chain[1] ...
        upstream.link(p)
        upstream = p
    for p in [stt_queue, *chain]:
        await p.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=tm))
        await p.queue_frame(StartFrame())
    await asyncio.sleep(0.05)  # every processor started, queues idle
    try:
        # the aggregator has just pushed the caller's VAD start upstream...
        await upstream.queue_frame(
            VADUserStartedSpeakingFrame(), FrameDirection.UPSTREAM
        )
        # ...and Sarvam's text for the earlier piece is handled right after
        await h.svc._handle_message(_data("mujhe kal"))
        assert [f.finalized for f in _transcripts(h)] == [False]
    finally:
        for p in [stt_queue, *chain]:
            await p.cleanup()
