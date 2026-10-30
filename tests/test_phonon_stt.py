"""Phonon STT: Clairvoyance's client for Phonon (our self-hosted STT server).

Locks in: the provider enum and routing (default URL, a template's own URL,
the legacy env map, key, language, fallback), config validation, the connect
URL and auth header Phonon expects, how Phonon's Deepgram-style messages map
to Pipecat frames and back, the close-code policy (resume with replay of the
unfinished turn, or give up), and the Sarvam fallback taking the audio.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    ServiceSwitcherRequestMetadataFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.stt_service import STTService
from pipecat.tests.utils import SleepFrame, run_test
from pydantic import ValidationError
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response
from websockets.protocol import State

import app.ai.voice.agents.breeze_buddy.stt as bb_stt_mod
import app.ai.voice.stt.phonon as phonon_mod
from app.ai.voice.agents.breeze_buddy.stt import create_stt_from_config
from app.ai.voice.agents.breeze_buddy.template.types import (
    STTConfiguration,
    STTProvider,
)
from app.ai.voice.stt import PhononConfig, PhononSTTService, build_phonon_stt
from app.ai.voice.stt.phonon import (
    REPLAY_MAX_SECONDS,
    PhononUnavailable,
    PhononWithFallback,
    resume_delay,
)
from app.ai.voice.stt.sarvam import SarvamConfig, build_sarvam_stt
from app.core.config import static


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake(config: Any, fallback: Any = None) -> str:
        seen["config"] = config
        seen["fallback"] = fallback
        return "svc"

    async def fake_fallback(*_: Any) -> str:
        return "sarvam-svc"

    monkeypatch.setattr(bb_stt_mod, "build_phonon_stt", fake)
    monkeypatch.setattr(bb_stt_mod, "_phonon_fallback", fake_fallback)
    return seen


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    """Resume back-offs (0.5-4 s) would make the suite slow; skip the wait."""

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(phonon_mod, "_sleep", no_wait)


class FakeSocket:
    """Stands in for the Phonon connection: records what the client sends,
    yields scripted messages, then ends with the scripted close."""

    def __init__(
        self,
        messages: list[str] | None = None,
        close_code: int | None = 1000,
        close_reason: str = "",
    ) -> None:
        self.state = State.OPEN
        self.sent: list[bytes | str] = []
        self.messages = messages or []
        self.close_code = close_code
        self.close_reason = close_reason

    async def send(self, data: bytes | str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.state = State.CLOSED

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for message in self.messages:
            yield message
        self.state = State.CLOSED


class FakeConnect:
    """Replaces websocket_connect: hands out sockets in order (an exception
    in the list is raised instead) and records each URL and header set."""

    def __init__(self, *outcomes: FakeSocket | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **kwargs: Any) -> FakeSocket:
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def query(self, i: int) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.calls[i][0]).query)


def _service(
    sample_rate: int = 8000, call_id: str | None = None, **kwargs: Any
) -> PhononSTTService:
    svc = PhononSTTService(url="ws://phonon:8098/", call_id=call_id, **kwargs)
    svc._sample_rate = sample_rate  # normally set from the pipeline's StartFrame
    return svc


def _errors(svc: PhononSTTService) -> list[dict[str, Any]]:
    """Capture what the service reports upstream instead of a pipeline."""
    pushed: list[dict[str, Any]] = []

    async def capture(error_msg: str, exception: Any = None, fatal: bool = False):
        pushed.append({"msg": error_msg, "exception": exception, "fatal": fatal})

    svc.push_error = capture  # type: ignore[method-assign]
    return pushed


async def _no_parent(*_: Any) -> None:  # skip the pipeline plumbing
    return None


async def _feed(svc: PhononSTTService, *chunks: bytes) -> None:
    for chunk in chunks:
        async for _ in svc.run_stt(chunk):
            pass


def _results(text: str, *, is_final: bool, from_finalize: bool = False) -> dict:
    return {
        "type": "Results",
        "is_final": is_final,
        "speech_final": is_final,
        "from_finalize": from_finalize,
        "start": 0.0,
        "duration": 1.2,
        "channel": {"alternatives": [{"transcript": text}]},
    }


# ---- provider, config, routing ----------------------------------------------------


def test_phonon_provider_enum_value():
    assert STTProvider("phonon") is STTProvider.PHONON


async def test_routes_to_the_default_phonon_url(monkeypatch):
    seen = _capture(monkeypatch)
    assert await create_stt_from_config(STTConfiguration(provider="phonon")) == "svc"
    assert seen["config"] == PhononConfig(
        url=static.PHONON_STT_URL, api_key=static.PHONON_STT_API_KEY or ""
    )


async def test_a_template_can_point_at_another_phonon(monkeypatch):
    seen = _capture(monkeypatch)
    config = STTConfiguration(
        provider="phonon", phonon={"url": "wss://phonon.stt.svc:8098/"}
    )
    await create_stt_from_config(config)
    assert seen["config"].url == "wss://phonon.stt.svc:8098"  # trailing / dropped


async def test_legacy_env_map_knows_phonon(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(bb_stt_mod, "BREEZE_BUDDY_STT_SERVICE", "phonon")
    assert await bb_stt_mod.get_stt_service() == "svc"
    assert seen["config"].url == static.PHONON_STT_URL


@pytest.mark.parametrize(
    "url",
    ["http://phonon:8098", "phonon:8098", "ws://phonon:8098/?x=1", "ws://phonon#a"],
)
def test_bad_urls_are_refused_at_template_save(url):
    with pytest.raises(ValidationError):
        STTConfiguration(provider="phonon", phonon={"url": url})


def test_end_of_speech_is_not_phonons_job():
    # Turn-taking stays with the pipeline; Phonon only transcribes.
    with pytest.raises(ValidationError):
        STTConfiguration(provider="phonon", end_of_speech_ms=500)


def test_builder_makes_the_service():
    svc = build_phonon_stt(PhononConfig(url="ws://phonon:8098", call_id="C1"))
    assert isinstance(svc, PhononSTTService)


# ---- the wire: connect URL and client messages ------------------------------------


def test_connect_url_carries_the_calls_audio_settings():
    url = urlparse(_service(sample_rate=8000, call_id="C-42").connect_url())
    assert (url.scheme, url.netloc, url.path) == ("ws", "phonon:8098", "/v1/listen")
    assert parse_qs(url.query) == {
        "encoding": ["linear16"],
        "sample_rate": ["8000"],
        "channels": ["1"],
        "call_id": ["C-42"],
    }


def test_a_call_id_cannot_inject_query_params():
    query = parse_qs(urlparse(_service(call_id="x&sample_rate=1").connect_url()).query)
    assert query["sample_rate"] == ["8000"] and query["call_id"] == ["x-sample_rate-1"]


async def test_audio_goes_out_as_binary_frames():
    svc = _service()
    svc._websocket = FakeSocket()
    async for _ in svc.run_stt(b"\x01\x02"):
        pass
    assert svc._websocket.sent == [b"\x01\x02"]


async def test_vad_stop_sends_finalize(monkeypatch):
    svc = _service()
    svc._websocket = FakeSocket()
    monkeypatch.setattr(
        "pipecat.services.stt_service.STTService.process_frame", _no_parent
    )
    await svc.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert svc._websocket.sent == [json.dumps({"type": "Finalize"})]


async def test_keepalive_is_a_message_not_silence():
    svc = _service()
    svc._websocket = FakeSocket()
    await svc._send_keepalive(b"\x00" * 320)
    assert svc._websocket.sent == [json.dumps({"type": "KeepAlive"})]


async def test_nothing_is_sent_on_a_closed_socket():
    svc = _service()
    svc._websocket = FakeSocket()
    svc._websocket.state = State.CLOSED
    await svc._send_keepalive(b"")
    assert svc._websocket.sent == []


# ---- results → frames -------------------------------------------------------------


async def _frames_for(message: dict) -> list[Any]:
    svc = _service()
    pushed: list[Any] = []

    async def capture(frame: Any, direction: Any = None) -> None:
        pushed.append(frame)

    async def no_metrics() -> None:
        return None

    svc.push_frame = capture  # type: ignore[method-assign]
    svc.stop_processing_metrics = no_metrics  # type: ignore[method-assign]
    await svc.handle_message(message)
    return pushed


async def test_a_partial_becomes_an_interim_frame():
    frames = await _frames_for(_results("namaste", is_final=False))
    assert len(frames) == 1 and isinstance(frames[0], InterimTranscriptionFrame)
    assert frames[0].text == "namaste"


async def test_a_final_becomes_one_finalized_transcription_frame():
    frames = await _frames_for(
        _results("namaste ji", is_final=True, from_finalize=True)
    )
    assert len(frames) == 1 and isinstance(frames[0], TranscriptionFrame)
    assert frames[0].text == "namaste ji" and frames[0].finalized


@pytest.mark.parametrize(
    "message",
    [
        _results("", is_final=True, from_finalize=True),  # finalize with no speech
        {"type": "Metadata", "duration": 1.2},
        {"type": "Something"},
        {"type": "Results", "channel": {}},
    ],
)
async def test_messages_without_text_push_nothing(message):
    assert await _frames_for(message) == []


# ---- template: key, language, fallback --------------------------------------------


async def test_the_key_comes_from_static_config_never_the_template(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(bb_stt_mod, "PHONON_STT_API_KEY", "k-123")
    await create_stt_from_config(STTConfiguration(provider="phonon"))
    assert seen["config"].api_key == "k-123"
    assert "k-123" not in repr(seen["config"])  # never logged via the config
    # A key in a template is not a field: it is dropped, never used.
    block = STTConfiguration(provider="phonon", phonon={"api_key": "k"}).phonon
    assert block is not None and not hasattr(block, "api_key")


@pytest.mark.parametrize(
    "language, sent",
    [("hi-IN", "hi-IN"), (["hi"], "hi"), (None, None), (["hi", "en"], None)],
)
async def test_the_templates_language_is_passed_as_is(monkeypatch, language, sent):
    seen = _capture(monkeypatch)
    await create_stt_from_config(STTConfiguration(provider="phonon", language=language))
    assert seen["config"].language == sent


async def test_no_fallback_unless_the_template_selects_one(monkeypatch):
    seen = _capture(monkeypatch)
    await create_stt_from_config(STTConfiguration(provider="phonon"))
    assert seen["fallback"] is None


async def test_a_template_can_opt_into_the_sarvam_fallback(monkeypatch):
    seen = _capture(monkeypatch)
    await create_stt_from_config(
        STTConfiguration(provider="phonon", phonon={"fallback": "sarvam"})
    )
    assert seen["fallback"] == "sarvam-svc"


async def test_fallback_none_builds_no_fallback(monkeypatch):
    seen = _capture(monkeypatch)
    await create_stt_from_config(
        STTConfiguration(provider="phonon", phonon={"fallback": "none"})
    )
    assert seen["fallback"] is None


@pytest.mark.parametrize("fallback", ["deepgram", "", None, "Sarvam"])
def test_unknown_fallbacks_are_refused_at_template_save(fallback):
    with pytest.raises(ValidationError):
        STTConfiguration(provider="phonon", phonon={"fallback": fallback})


async def test_the_sarvam_fallback_is_built_from_the_template_and_held(monkeypatch):
    built: dict[str, Any] = {}
    sarvam = _sarvam()

    async def fake_create(config: Any, accounts: Any = None, **kwargs: Any) -> Any:
        built["config"] = config
        return sarvam

    monkeypatch.setattr(bb_stt_mod, "create_stt_from_config", fake_create)
    config = STTConfiguration(
        provider="phonon", sarvam={"language_code": "hi-IN"}, language="hi"
    )
    assert await bb_stt_mod._phonon_fallback(config, None, True) is sarvam
    assert built["config"].provider == STTProvider.SARVAM
    assert built["config"].sarvam.language_code == "hi-IN"
    assert sarvam._standby  # no Sarvam session until it is needed


async def test_a_fallback_that_cannot_be_built_fails_open(monkeypatch):
    async def broken(*_: Any, **__: Any) -> Any:
        raise ValueError("SARVAM_API_KEY is required")

    monkeypatch.setattr(bb_stt_mod, "create_stt_from_config", broken)
    config = STTConfiguration(provider="phonon")
    assert await bb_stt_mod._phonon_fallback(config, None, True) is None


def test_builder_wraps_the_service_when_given_a_fallback():
    fallback = _RecordingSTT()
    wrapped = build_phonon_stt(PhononConfig(url="ws://p"), fallback=fallback)
    assert isinstance(wrapped, PhononWithFallback)
    primary = wrapped.services[0]
    assert isinstance(primary, PhononSTTService) and not primary._fatal_on_failure
    alone = build_phonon_stt(PhononConfig(url="ws://p"))
    assert isinstance(alone, PhononSTTService) and alone._fatal_on_failure


# ---- connect: auth header, language, call_id --------------------------------------


async def test_the_key_goes_in_the_authorization_header(monkeypatch):
    connect = FakeConnect(FakeSocket())
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(api_key="secret-key")
    await svc._connect_websocket()
    url, kwargs = connect.calls[0]
    assert kwargs["additional_headers"] == {"Authorization": "Token secret-key"}
    assert "secret-key" not in url


async def test_no_key_sends_no_authorization_header(monkeypatch):
    connect = FakeConnect(FakeSocket())
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    await _service()._connect_websocket()
    assert connect.calls[0][1]["additional_headers"] == {}


def test_language_rides_the_query_only_when_set():
    assert parse_qs(urlparse(_service(language="hi-IN").connect_url()).query)[
        "language"
    ] == ["hi-IN"]
    assert "language" not in parse_qs(urlparse(_service().connect_url()).query)


def test_a_call_without_an_id_gets_one_that_resume_can_reuse():
    svc = _service()
    first = parse_qs(urlparse(svc.connect_url()).query)["call_id"]
    resumed = parse_qs(urlparse(svc.connect_url(resume=True)).query)
    assert resumed["call_id"] == first and resumed["resume"] == ["true"]


async def test_a_rejected_key_gives_up_at_once(monkeypatch):
    rejected = InvalidStatus(Response(401, "Unauthorized", Headers(), b""))
    connect = FakeConnect(rejected)
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(fatal_on_failure=False)
    errors = _errors(svc)
    await svc._connect_websocket()
    assert len(connect.calls) == 1 and svc.failed
    assert isinstance(errors[0]["exception"], PhononUnavailable)
    assert not errors[0]["fatal"]


async def test_without_a_fallback_giving_up_is_fatal(monkeypatch):
    monkeypatch.setattr(phonon_mod, "websocket_connect", FakeConnect(OSError("x")))
    svc = _service()  # fatal_on_failure defaults to True
    errors = _errors(svc)
    await svc._connect_websocket()
    assert errors[0]["fatal"]


# ---- close codes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, used, delay",
    [
        (1001, {}, 0.5),  # draining: back off 0.5, 1, 2, 4 s
        (1001, {1001: 3}, 4.0),
        (1001, {1001: 4}, None),  # draining forever is not a plan
        (1011, {}, 1.0),  # not at once: the dying server may still answer
        (1011, {1011: 1}, 2.0),
        (1011, {1011: 2}, None),
        (1013, {}, 0.5),
        (1013, {1013: 1}, None),
        (1006, {}, 0.0),  # network drop: at once, then after 1 s
        (1006, {1006: 1}, 1.0),
        (1006, {1006: 2}, None),
        (None, {}, 0.0),
        (1000, {}, 0.0),  # a close the client did not ask for
        (1008, {}, None),
        (1009, {}, None),
        (4000, {}, None),
    ],
)
def test_resume_policy_per_close_code(code, used, delay):
    assert resume_delay(code, used) == delay


async def _turn_with_pending_finalize(monkeypatch, svc: PhononSTTService) -> None:
    """A finished turn, then the caller speaks again and stops: two chunks of
    the new turn are unfinished and its Finalize is unanswered."""
    monkeypatch.setattr(
        "pipecat.services.stt_service.STTService.process_frame", _no_parent
    )
    svc._websocket = FakeSocket()
    await _feed(svc, b"old-1", b"old-2")
    await svc.handle_message(_results("", is_final=True))  # nothing said
    await _feed(svc, b"new-1", b"new-2")
    await svc.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


@pytest.mark.parametrize("code", [1001, 1011])
async def test_resume_replays_exactly_the_unfinished_turn_then_finalize(
    monkeypatch, code
):
    resumed = FakeSocket()
    connect = FakeConnect(resumed)
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(call_id="C-7")
    await _turn_with_pending_finalize(monkeypatch, svc)

    assert await svc._after_close(code, "draining: restart")
    query = connect.query(0)
    assert query["resume"] == ["true"] and query["call_id"] == ["C-7"]
    assert resumed.sent == [b"new-1", b"new-2", json.dumps({"type": "Finalize"})]
    assert svc._websocket is resumed


async def test_no_finalize_is_resent_once_its_final_arrived(monkeypatch):
    resumed = FakeSocket()
    monkeypatch.setattr(phonon_mod, "websocket_connect", FakeConnect(resumed))
    svc = _service()
    await _turn_with_pending_finalize(monkeypatch, svc)
    await svc.handle_message(_results("", is_final=True, from_finalize=True))
    await _feed(svc, b"next")
    assert await svc._after_close(1011, "internal: boom")
    assert resumed.sent == [b"next"]


async def test_replay_is_bounded_to_the_last_30_seconds(monkeypatch):
    resumed = FakeSocket()
    monkeypatch.setattr(phonon_mod, "websocket_connect", FakeConnect(resumed))
    svc = _service(sample_rate=8000)
    svc._websocket = FakeSocket()
    one_second = 8000 * 2
    chunks = [bytes([i]) * one_second for i in range(35)]
    await _feed(svc, *chunks)
    assert await svc._after_close(1001, "draining: restart")
    assert resumed.sent == chunks[-int(REPLAY_MAX_SECONDS) :]


async def test_audio_during_the_outage_is_replayed_in_order(monkeypatch):
    svc = _service()
    svc._websocket = FakeSocket()
    await _feed(svc, b"a")

    resumed = FakeSocket()

    async def connect_while_audio_arrives(url: str, **kwargs: Any) -> FakeSocket:
        await _feed(svc, b"b")  # the caller keeps talking meanwhile
        return resumed

    monkeypatch.setattr(phonon_mod, "websocket_connect", connect_while_audio_arrives)
    assert await svc._after_close(1011, "internal: boom")
    await _feed(svc, b"c")
    assert resumed.sent == [b"a", b"b", b"c"]


async def test_1008_is_never_retried(monkeypatch):
    connect = FakeConnect(FakeSocket())
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(fatal_on_failure=False)
    errors = _errors(svc)
    assert not await svc._after_close(
        1008, "unsupported_language: language 'kn' is not served here"
    )
    assert connect.calls == [] and svc.failed
    assert "unsupported_language" in errors[0]["msg"]
    assert "'kn'" not in errors[0]["msg"]  # only the reason's code is used


async def test_1013_retries_once_after_a_pause_then_gives_up(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(phonon_mod, "_sleep", fake_sleep)
    connect = FakeConnect(FakeSocket(), FakeSocket())
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(fatal_on_failure=False)
    errors = _errors(svc)
    assert await svc._after_close(1013, "overloaded: busy")
    assert slept == [0.5] and len(connect.calls) == 1
    assert not await svc._after_close(1013, "overloaded: busy")
    assert len(connect.calls) == 1 and len(errors) == 1


async def test_a_resume_that_never_connects_gives_up_after_its_budget(monkeypatch):
    connect = FakeConnect(*[OSError("refused")] * 4)
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(fatal_on_failure=False)
    errors = _errors(svc)
    assert not await svc._after_close(1001, "draining: restart")
    assert len(connect.calls) == 4  # every 1001 attempt was tried
    assert svc.failed and len(errors) == 1


async def test_receive_loop_resumes_twice_on_1011_then_gives_up(monkeypatch):
    first = FakeSocket(close_code=1011, close_reason="internal: boom")
    second = FakeSocket(
        messages=[json.dumps(_results("namaste", is_final=False))],
        close_code=1011,
        close_reason="internal: boom",
    )
    third = FakeSocket(close_code=1011, close_reason="internal: boom")
    connect = FakeConnect(second, third)
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(fatal_on_failure=False)
    errors = _errors(svc)
    pushed: list[Frame] = []

    async def capture(frame: Frame, direction: Any = None) -> None:
        pushed.append(frame)

    svc.push_frame = capture  # type: ignore[method-assign]
    svc._websocket = first
    await asyncio.wait_for(svc._receive_task_handler(svc._report_error), 2)
    assert connect.query(0)["resume"] == ["true"]
    texts = [f.text for f in pushed if isinstance(f, InterimTranscriptionFrame)]
    assert texts == ["namaste"]  # the resumed socket served
    assert svc.failed and len(errors) == 1


async def test_a_requested_close_is_not_a_failure(monkeypatch):
    connect = FakeConnect()
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(fatal_on_failure=False)
    errors = _errors(svc)
    svc._websocket = FakeSocket(close_code=1000)
    svc._closing = True  # CloseStream was sent
    await svc._receive_task_handler(svc._report_error)
    assert connect.calls == [] and errors == [] and not svc.failed


async def test_nothing_is_sent_after_giving_up(monkeypatch):
    monkeypatch.setattr(phonon_mod, "websocket_connect", FakeConnect(OSError("x")))
    svc = _service(fatal_on_failure=False)
    _errors(svc)
    await svc._connect_websocket()
    socket = FakeSocket()
    svc._websocket = socket
    await _feed(svc, b"\x01")
    assert socket.sent == [] and not svc._turn


# ---- fallback ---------------------------------------------------------------------


class _RecordingSTT(STTService):
    """A fallback STT that records the audio it is asked to transcribe."""

    def __init__(self) -> None:
        super().__init__()
        self.audio: list[bytes] = []
        self.activated = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, ServiceSwitcherRequestMetadataFrame):
            self.activated = True
        await super().process_frame(frame, direction)

    async def run_stt(self, audio: bytes):
        self.audio.append(audio)
        yield None


def _audio(payload: bytes) -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=payload, sample_rate=16000, num_channels=1)


async def test_audio_flows_to_the_fallback_once_phonon_gives_up(monkeypatch):
    monkeypatch.setattr(
        phonon_mod, "websocket_connect", FakeConnect(OSError("refused"))
    )
    fallback = _RecordingSTT()
    switcher = build_phonon_stt(PhononConfig(url="ws://p"), fallback=fallback)
    assert isinstance(switcher, PhononWithFallback)
    _, up = await run_test(
        switcher,
        frames_to_send=[SleepFrame(0.1), _audio(b"\x01\x02"), _audio(b"\x03\x04")],
    )
    assert fallback.activated
    assert fallback.audio == [b"\x01\x02", b"\x03\x04"]
    # The give-up still reaches the pipeline (track_error via on_pipeline_error).
    errors = [f for f in up if isinstance(f, ErrorFrame)]
    assert errors and isinstance(errors[0].exception, PhononUnavailable)
    assert not errors[0].fatal


async def test_phonon_keeps_the_audio_while_it_is_healthy(monkeypatch):
    sockets = FakeSocket()

    async def stay_open(url: str, **kwargs: Any) -> FakeSocket:
        return sockets

    # A socket whose receive loop never ends on its own.
    async def forever(self: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(phonon_mod, "websocket_connect", stay_open)
    monkeypatch.setattr(PhononSTTService, "_receive_messages", forever)
    monkeypatch.setattr(phonon_mod, "_CLOSE_STREAM_WAIT_SECS", 0.05)
    fallback = _RecordingSTT()
    switcher = build_phonon_stt(PhononConfig(url="ws://p"), fallback=fallback)
    await run_test(switcher, frames_to_send=[SleepFrame(0.05), _audio(b"\x01\x02")])
    assert b"\x01\x02" in sockets.sent and fallback.audio == []
    assert not fallback.activated


async def test_only_phonon_giving_up_moves_the_call():
    fallback = _RecordingSTT()
    switcher = build_phonon_stt(PhononConfig(url="ws://p"), fallback=fallback)
    assert isinstance(switcher, PhononWithFallback)
    primary = switcher.services[0]
    other = ErrorFrame("send failed", processor=primary)
    assert await switcher.strategy.handle_error(other) is None
    assert switcher.strategy.active_service is primary


def _sarvam() -> Any:
    return build_sarvam_stt(
        SarvamConfig(api_key="k", model="saarika:v2.5", sample_rate=8000)
    )


async def test_a_held_sarvam_connects_only_when_activated(monkeypatch):
    connects: list[str] = []

    async def record(self: Any) -> None:
        connects.append("connect")

    monkeypatch.setattr(SarvamSTTService, "_connect", record)
    monkeypatch.setattr(
        "pipecat.services.stt_service.STTService.process_frame", _no_parent
    )
    sarvam = _sarvam()
    sarvam.hold_connection_until_active()
    sarvam._sample_rate = 8000
    await sarvam._connect()  # what start() does
    assert connects == []
    await sarvam.process_frame(
        ServiceSwitcherRequestMetadataFrame(service=sarvam), FrameDirection.DOWNSTREAM
    )
    assert connects == ["connect"]


async def test_set_self_interrupt_reaches_the_fallback():
    sarvam = _sarvam()
    switcher = build_phonon_stt(PhononConfig(url="ws://p"), fallback=sarvam)
    assert isinstance(switcher, PhononWithFallback)
    switcher.set_self_interrupt(False)
    assert sarvam._self_interrupt is False


# ---- replay keeps every chunk and every turn boundary ------------------------------


class _AudioDuringFinalizeSocket(FakeSocket):
    """A resumed socket whose Finalize send yields, and a live chunk arrives
    in exactly that window (review: it used to be buffered but never sent)."""

    def __init__(self, svc: PhononSTTService) -> None:
        super().__init__()
        self.svc = svc
        self.fed = False

    async def send(self, data: bytes | str) -> None:
        if data == _FINALIZE_MSG and not self.fed:
            self.fed = True
            await _feed(self.svc, b"RACE-CHUNK")
        await super().send(data)


_FINALIZE_MSG = json.dumps({"type": "Finalize"})


async def test_audio_arriving_during_the_finalize_resend_is_still_sent(monkeypatch):
    svc = _service(call_id="C-9")
    await _turn_with_pending_finalize(monkeypatch, svc)
    resumed = _AudioDuringFinalizeSocket(svc)
    monkeypatch.setattr(phonon_mod, "websocket_connect", FakeConnect(resumed))
    assert await svc._after_close(1011, "internal: boom")
    await _feed(svc, b"AFTER")
    # The racing chunk came after the Finalize: it is the next turn's audio.
    assert resumed.sent == [
        b"new-1",
        b"new-2",
        _FINALIZE_MSG,
        b"RACE-CHUNK",
        b"AFTER",
    ]


async def test_replay_puts_each_finalize_back_at_its_place(monkeypatch):
    # Turn 1 is finalized, the caller starts turn 2, then the socket drops
    # before turn 1's final: turn 2 must not be merged into turn 1's final.
    monkeypatch.setattr(
        "pipecat.services.stt_service.STTService.process_frame", _no_parent
    )
    svc = _service(call_id="C-10")
    svc._websocket = FakeSocket()
    await _feed(svc, b"t1-a", b"t1-b")
    await svc.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _feed(svc, b"t2-a")
    resumed = FakeSocket()
    monkeypatch.setattr(phonon_mod, "websocket_connect", FakeConnect(resumed))
    assert await svc._after_close(1001, "draining: restart")
    assert resumed.sent == [b"t1-a", b"t1-b", _FINALIZE_MSG, b"t2-a"]


async def test_a_final_keeps_the_next_turns_audio_for_a_replay(monkeypatch):
    monkeypatch.setattr(
        "pipecat.services.stt_service.STTService.process_frame", _no_parent
    )
    svc = _service(call_id="C-11")
    svc._websocket = FakeSocket()
    svc.push_frame = _swallow  # type: ignore[method-assign]
    svc.stop_processing_metrics = _nothing  # type: ignore[method-assign]
    await _feed(svc, b"t1-a")
    await svc.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _feed(svc, b"t2-a")  # the caller already speaks again
    await svc.handle_message(_results("turn one", is_final=True, from_finalize=True))
    resumed = FakeSocket()
    monkeypatch.setattr(phonon_mod, "websocket_connect", FakeConnect(resumed))
    assert await svc._after_close(1011, "internal: boom")
    # Turn 1 is done (not replayed); turn 2's audio is not lost.
    assert resumed.sent == [b"t2-a"]


async def _swallow(*_: Any, **__: Any) -> None:
    return None


async def _nothing() -> None:
    return None


# ---- edge cases from the 29 Oct tests ----------------------------------------------


@pytest.mark.parametrize(
    "given, sent",
    [
        ("acct/123@twilio", "acct-123-twilio"),
        ("call 1", "call-1"),
        ("CA5f2e9a1b0c", "CA5f2e9a1b0c"),
        ("call-1_2.3:4", "call-1_2.3:4"),
        ("x" * 200, "x" * 128),
    ],
)
def test_call_ids_are_made_safe_for_phonon(given, sent):
    assert phonon_mod.safe_call_id(given) == sent


@pytest.mark.parametrize("given", [None, "", "///", "नमस्ते"])
def test_an_unusable_call_id_gets_a_generated_one(given):
    assert phonon_mod.safe_call_id(given).startswith("bb-")


def test_every_resume_sends_the_same_safe_call_id():
    svc = _service(call_id="acct/9@x")
    first = parse_qs(urlparse(svc.connect_url()).query)["call_id"]
    again = parse_qs(urlparse(svc.connect_url(resume=True)).query)["call_id"]
    assert first == again == ["acct-9-x"]


async def test_a_refused_connect_while_draining_tries_again(monkeypatch):
    # One server restarting: the first attempts are refused, a later one lands.
    resumed = FakeSocket()
    connect = FakeConnect(OSError("refused"), OSError("refused"), resumed)
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(fatal_on_failure=False)
    assert await svc._after_close(1001, "draining: restart")
    assert svc._websocket is resumed and not svc.failed
    assert len(connect.calls) == 3


def _http(status: int) -> InvalidStatus:
    return InvalidStatus(Response(status, "", Headers()))


async def test_http_503_on_resume_tries_again_but_401_gives_up(monkeypatch):
    resumed = FakeSocket()
    connect = FakeConnect(_http(503), resumed)
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(fatal_on_failure=False)
    assert await svc._after_close(1011, "server_error: model gone")
    assert svc._websocket is resumed

    connect = FakeConnect(_http(401), FakeSocket())
    monkeypatch.setattr(phonon_mod, "websocket_connect", connect)
    svc = _service(fatal_on_failure=False)
    assert not await svc._after_close(1011, "server_error: model gone")
    assert len(connect.calls) == 1 and svc.failed


async def test_a_healthy_stretch_restores_the_retry_budget(monkeypatch):
    svc = _service(fatal_on_failure=False)
    svc._resumes_used = {1006: 2}  # both network-drop retries already spent
    svc._live_since = phonon_mod.time.monotonic() - 61  # healthy for 61 s
    monkeypatch.setattr(phonon_mod, "websocket_connect", FakeConnect(FakeSocket()))
    assert await svc._after_close(1006, "")
    assert not svc.failed


async def test_without_a_healthy_stretch_the_budget_still_runs_out(monkeypatch):
    svc = _service(fatal_on_failure=False)
    _errors(svc)
    svc._resumes_used = {1006: 2}
    svc._live_since = phonon_mod.time.monotonic() - 5  # only 5 s since
    monkeypatch.setattr(phonon_mod, "websocket_connect", FakeConnect(FakeSocket()))
    assert not await svc._after_close(1006, "")
    assert svc.failed


async def test_a_warning_is_logged_not_turned_into_text():
    frames = await _frames_for(
        {"type": "Warning", "code": "audio_dropped", "dropped_s": 1.2, "message": "x"}
    )
    assert frames == []


async def test_a_hang_up_mid_sentence_finalizes_the_turn_first(monkeypatch):
    monkeypatch.setattr(
        "pipecat.services.stt_service.STTService.process_frame", _no_parent
    )
    monkeypatch.setattr("pipecat.services.stt_service.STTService.stop", _swallow)
    socket = FakeSocket()
    svc = _service()
    svc._websocket = socket
    svc._receive_done.set()  # the server closes at once in this fake
    await svc.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _feed(svc, b"last-words")  # ...and the caller hangs up mid-sentence
    await svc.stop(EndFrame())
    assert socket.sent[-2:] == [_FINALIZE_MSG, json.dumps({"type": "CloseStream"})]


async def test_silence_after_the_last_turn_sends_no_extra_finalize(monkeypatch):
    # A normal call end: the caller's last turn was finalized on VAD stop, then
    # only silence flows until the call ends. No second Finalize (it would just
    # make Phonon return an empty final).
    monkeypatch.setattr(
        "pipecat.services.stt_service.STTService.process_frame", _no_parent
    )
    monkeypatch.setattr("pipecat.services.stt_service.STTService.stop", _swallow)
    socket = FakeSocket()
    svc = _service()
    svc._websocket = socket
    svc._receive_done.set()
    await svc.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _feed(svc, b"theek hai")
    await svc.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _feed(svc, b"silence-1", b"silence-2")
    await svc.stop(EndFrame())
    assert socket.sent.count(_FINALIZE_MSG) == 1
    assert socket.sent[-1] == json.dumps({"type": "CloseStream"})


def test_the_pipeline_is_told_phonons_measured_latency():
    assert _service()._ttfs_p99_latency == 0.5


async def test_no_extra_finalize_when_the_turn_was_already_finalized(monkeypatch):
    monkeypatch.setattr(
        "pipecat.services.stt_service.STTService.process_frame", _no_parent
    )
    monkeypatch.setattr("pipecat.services.stt_service.STTService.stop", _swallow)
    socket = FakeSocket()
    svc = _service()
    svc._websocket = socket
    svc._receive_done.set()
    await _feed(svc, b"words")
    await svc.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await svc.stop(EndFrame())
    assert socket.sent.count(_FINALIZE_MSG) == 1


def test_the_call_end_wait_matches_phonons():
    assert phonon_mod._CLOSE_STREAM_WAIT_SECS == 5.0


def test_pipecat_settings_are_complete():
    svc = _service()
    assert svc._settings.model == "phonon" and svc._settings.language is None
