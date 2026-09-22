"""The turn timeline: every pipeline event, when it happened, and the dead air
between. Times are driven by a fake clock so the assertions are exact."""

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.processors import metrics_collector_processor
from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (
    MetricsCollectorProcessor,
)

# Pipecat types source/destination as FrameProcessor, and the observer never
# reads either — but a bare None fails the type check, so the events carry a
# real processor.
_LINK = FrameProcessor()


def pushed(frame: Frame, timestamp: int) -> FramePushed:
    """A FramePushed as Pipecat builds one for a downstream hop."""
    return FramePushed(
        source=_LINK,
        destination=_LINK,
        frame=frame,
        direction=FrameDirection.DOWNSTREAM,
        timestamp=timestamp,
    )


class FakeClock:
    """The monotonic clock the test advances by hand, in milliseconds.

    Nanoseconds, because the collector reads the pipeline clock and
    SystemClock.get_time() returns monotonic_ns() offset by the pipeline's
    start. An observer's FramePushed.timestamp is on that same scale.
    """

    def __init__(self) -> None:
        self.ns = 1_000_000_000

    def __call__(self) -> int:
        return self.ns

    def advance(self, ms: float) -> None:
        self.ns += int(ms * 1_000_000)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(metrics_collector_processor.time, "monotonic_ns", fake)
    return fake


@pytest.fixture
def collector():
    processor = MetricsCollectorProcessor()

    async def _noop(
        frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        return None

    processor.push_frame = _noop  # type: ignore[method-assign]
    return processor


# Pipecat reports the processor's CLASS name, never the bare stage. Tests use
# the real ones this service configures, or they verify a fiction.
LLM_PROCESSOR = "AzureLLMService#0"
TTS_PROCESSOR = "ElevenLabsTTSService#0"
STT_PROCESSOR = "SonioxSTTServiceWithEndpointDelay#0"


def llm_ttfb(seconds: float):
    return MetricsFrame(data=[TTFBMetricsData(processor=LLM_PROCESSOR, value=seconds)])


async def feed(collector, clock, script, with_metrics=True):
    """Run `script` — (gap_ms_before, frame) pairs — through the collector.

    A processor metric is seeded first unless `with_metrics=False`. The
    emission gate is unchanged from before the timeline existed: a turn is
    only emitted when it carries processor metrics or a function call, and
    every real turn raises a MetricsFrame. These tests are about what the
    timeline holds, so the seed keeps them from tripping over that rule;
    `test_turn_without_metrics_is_not_emitted` covers the rule itself.
    """
    if with_metrics and not collector._current_turn_metrics:
        await collector.process_frame(llm_ttfb(0.4), FrameDirection.DOWNSTREAM)
    for gap_ms, frame in script:
        clock.advance(gap_ms)
        await collector.process_frame(frame, FrameDirection.DOWNSTREAM)


def audio_frame():
    return TTSAudioRawFrame(audio=b"\x00\x00", sample_rate=16000, num_channels=1)


def transcription(text="book a table"):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t", finalized=True)


def events(turn):
    return [e["event"] for e in turn["timeline"]]


def at(turn):
    return {e["event"]: e["at_ms"] for e in turn["timeline"]}


@pytest.mark.asyncio
async def test_full_turn_timeline_and_gaps(collector, clock):
    """A complete turn lays out in pipeline order with the idle time named."""
    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (1720, UserStoppedSpeakingFrame()),
            (600, LLMFullResponseStartFrame()),
            (30, TTSStartedFrame()),
            (290, audio_frame()),
            (50, BotStartedSpeakingFrame()),
            (100, LLMFullResponseEndFrame()),
            (200, TTSStoppedFrame()),
            (3190, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert events(turn) == [
        "user_started_speaking",
        "user_stopped_speaking",
        "llm_response_start",
        "tts_request",
        "tts_first_audio",
        "bot_started_speaking",
        "llm_response_end",
        "tts_done",
        "bot_stopped_speaking",
    ]

    assert "gap_ms" not in turn["timeline"][0]
    assert turn["timeline"][0]["at_ms"] == 0.0

    offsets = at(turn)
    assert offsets["user_stopped_speaking"] == 1720
    assert offsets["llm_response_start"] == 2320
    assert offsets["bot_started_speaking"] == 2690
    assert offsets["bot_stopped_speaking"] == 6180

    # The gap is the point of the whole exercise: 430ms waiting on the LLM.
    gaps = {e["event"]: e.get("gap_ms") for e in turn["timeline"]}
    assert gaps["llm_response_start"] == 600
    assert gaps["tts_first_audio"] == 290.0

    assert turn["summary"]["response_latency_ms"] == 970.0
    assert turn["summary"]["user_speech_ms"] == 1720.0
    assert turn["summary"]["bot_speech_ms"] == 3490.0
    assert "interrupted" not in turn["summary"]


@pytest.mark.asyncio
async def test_barge_in_flags_one_turn(collector, clock):
    """Interruption marks the turn; it does not split it in two."""
    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (1000, UserStoppedSpeakingFrame()),
            (400, BotStartedSpeakingFrame()),
            (800, InterruptionFrame()),
            (50, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert len(collector.get_metrics()) == 1
    assert turn["summary"]["interrupted"] is True
    # The interruption must land on the turn it cut short, not the next one.
    assert events(turn)[-2:] == ["user_interrupted", "bot_stopped_speaking"]
    assert at(turn)["user_interrupted"] == 2200.0


@pytest.mark.asyncio
async def test_greeting_turn_has_no_response_latency(collector, clock):
    """An outbound greeting answers nobody — a zero latency would be a lie."""
    await feed(
        collector,
        clock,
        [
            (0, TTSStartedFrame()),
            (300, audio_frame()),
            (40, BotStartedSpeakingFrame()),
            (3200, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert turn["timeline"][0]["event"] == "tts_request"
    assert turn["timeline"][0]["at_ms"] == 0.0
    assert "response_latency_ms" not in turn["summary"]
    assert turn["summary"]["bot_speech_ms"] == 3200.0


@pytest.mark.asyncio
async def test_only_first_audio_chunk_is_marked(collector, clock):
    """The rest of the chunks are the audio stream, not events."""
    await feed(
        collector,
        clock,
        [
            (0, TTSStartedFrame()),
            (200, audio_frame()),
            (20, audio_frame()),
            (20, audio_frame()),
            (10, BotStartedSpeakingFrame()),
            (500, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert events(turn).count("tts_first_audio") == 1


@pytest.mark.asyncio
async def test_function_calls_appear_on_the_timeline(collector, clock):
    """Tool latency is dead air the caller hears; it belongs in the trail."""
    await feed(
        collector,
        clock,
        [
            (0, UserStoppedSpeakingFrame()),
            (
                300,
                FunctionCallInProgressFrame(
                    function_name="check_availability",
                    tool_call_id="call_1",
                    arguments={},
                ),
            ),
            (
                1200,
                FunctionCallResultFrame(
                    function_name="check_availability",
                    tool_call_id="call_1",
                    arguments={},
                    result="ok",
                ),
            ),
            (200, BotStartedSpeakingFrame()),
            (800, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert "function_call_started" in events(turn)
    entry = next(e for e in turn["timeline"] if e["event"] == "function_call_result")
    assert entry["name"] == "check_availability"
    assert entry["gap_ms"] == 1200.0
    assert turn["functions"][0]["latency_ms"] == 1200.0


@pytest.mark.asyncio
async def test_turns_are_independent_and_end_frame_closes_the_last(collector, clock):
    """Each turn restarts the clock; EndFrame flushes the turn in flight."""
    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (500, BotStartedSpeakingFrame()),
            (500, BotStoppedSpeakingFrame()),
            (2000, UserStartedSpeakingFrame()),
            (0, llm_ttfb(0.3)),
            (700, BotStartedSpeakingFrame()),
            (300, EndFrame()),
        ],
    )

    first, second = collector.get_metrics()
    assert first["turn"] == 1 and second["turn"] == 2
    # The 2000ms of silence between turns is not charged to the second turn.
    assert second["timeline"][0]["at_ms"] == 0.0
    assert at(second)["bot_started_speaking"] == 700.0
    assert "bot_stopped_speaking" not in at(second)


@pytest.mark.asyncio
async def test_full_llm_and_tts_spans_are_summarized(collector, clock):
    """First token is the wait; the full span is the cost. Report both."""
    await feed(
        collector,
        clock,
        [
            (0, UserStoppedSpeakingFrame()),
            (500, LLMFullResponseStartFrame()),
            (5, llm_ttfb(0.400)),
            (25, TTSStartedFrame()),
            (290, audio_frame()),
            (50, BotStartedSpeakingFrame()),
            (120, LLMFullResponseEndFrame()),
            (240, TTSStoppedFrame()),
            (2900, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    summary = turn["summary"]
    assert summary["llm_ttft_ms"] == 400.0
    # 500 -> 990: streaming continued well after TTS began speaking it.
    assert summary["llm_total_ms"] == 490.0
    # tts_request at 530ms, tts_done at 1230ms.
    assert summary["tts_total_ms"] == 700.0


@pytest.mark.asyncio
async def test_talkover_without_interruption_frame_stays_positive(collector, clock):
    """Speech events repeat; a flat {event: at_ms} map would subtract backwards."""
    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (1000, UserStoppedSpeakingFrame()),
            (400, BotStartedSpeakingFrame()),
            (300, UserStartedSpeakingFrame()),
            (900, UserStoppedSpeakingFrame()),
            (200, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    summary = turn["summary"]
    # Measured from the stop that PRECEDED the bot, not the later one.
    assert summary["response_latency_ms"] == 400.0
    assert all(v >= 0 for k, v in summary.items() if isinstance(v, (int, float)))
    # Both utterances count toward how long the caller actually talked.
    assert summary["user_speech_ms"] == 1900.0


@pytest.mark.asyncio
async def test_turn_with_no_bot_reply_reports_no_latency(collector, clock):
    """Nothing to measure against; the field is absent rather than zero."""
    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (1200, UserStoppedSpeakingFrame()),
            (5000, EndFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert "response_latency_ms" not in turn["summary"]
    assert "bot_speech_ms" not in turn["summary"]
    assert turn["summary"]["user_speech_ms"] == 1200.0


@pytest.mark.asyncio
async def test_interrupted_turn_reports_time_to_interruption(collector, clock):
    """How long the bot got before being cut off is the number that says why."""
    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (1000, UserStoppedSpeakingFrame()),
            (400, BotStartedSpeakingFrame()),
            (1400, InterruptionFrame()),
            (60, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert turn["summary"]["interrupted"] is True
    assert turn["summary"]["time_to_interruption_ms"] == 1400


@pytest.mark.asyncio
async def test_interruption_before_the_bot_speaks_is_not_a_barge_in(collector, clock):
    """Nothing was cut short, so the turn is not marked at all.

    The frame arrives on every user turn start, so before the bot has spoken
    it carries no information about an interruption.
    """
    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (800, InterruptionFrame()),
            (200, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert "interrupted" not in turn["summary"]
    assert "time_to_interruption_ms" not in turn["summary"]


@pytest.mark.asyncio
async def test_llm_ttft_comes_from_the_provider_metric(collector, clock):
    """LLMFullResponseStartFrame fires before the request on Anthropic and after
    the first token on OpenAI, so TTFB is read from the service, not derived."""
    await feed(
        collector,
        clock,
        [
            (0, UserStoppedSpeakingFrame()),
            (500, LLMFullResponseStartFrame()),
            (10, llm_ttfb(0.405)),
            (100, BotStartedSpeakingFrame()),
            (1000, BotStoppedSpeakingFrame()),
        ],
        with_metrics=False,
    )

    (turn,) = collector.get_metrics()
    assert turn["summary"]["llm_ttft_ms"] == 405
    # No invented event on the timeline, and nothing out of clock order.
    assert "llm_request" not in events(turn)
    assert [e["at_ms"] for e in turn["timeline"]] == sorted(
        e["at_ms"] for e in turn["timeline"]
    )
    assert all(e.get("gap_ms", 0) >= 0 for e in turn["timeline"])


@pytest.mark.asyncio
async def test_observer_reports_moments_the_collector_cannot_see(collector, clock):
    """stt_final and llm_first_token are consumed upstream; the observer relays them."""
    from pipecat.frames.frames import LLMTextFrame

    from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (
        TimelineObserver,
    )

    observer = TimelineObserver(collector)

    async def observe(gap_ms, frame):
        clock.advance(gap_ms)
        await observer.on_push_frame(pushed(frame, clock.ns))

    await feed(collector, clock, [(0, UserStartedSpeakingFrame())])
    await feed(collector, clock, [(1000, UserStoppedSpeakingFrame())])
    await observe(170, transcription())
    await observe(430, LLMTextFrame(text="Sure"))
    await observe(20, LLMTextFrame(text=", one moment"))
    await feed(collector, clock, [(80, BotStartedSpeakingFrame())])
    await feed(collector, clock, [(900, BotStoppedSpeakingFrame())])

    (turn,) = collector.get_metrics()
    assert events(turn) == [
        "user_started_speaking",
        "user_stopped_speaking",
        "stt_final",
        "llm_first_token",
        "bot_started_speaking",
        "bot_stopped_speaking",
    ]
    # Only the FIRST token marks the timeline; the rest are the stream.
    assert events(turn).count("llm_first_token") == 1
    assert at(turn)["llm_first_token"] == 1600
    assert turn["summary"]["response_latency_ms"] == 700


def test_timeline_observer_is_attached_to_every_call():
    """pyrefly passing does not prove the observer is actually attached."""
    import inspect

    from app.ai.voice.agents.breeze_buddy.agent import pipeline
    from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (
        MetricsCollectorProcessor,
        TimelineObserver,
    )

    # It rides the observer list that every call already gets, and is absent
    # when no collector is supplied rather than failing.
    collector = MetricsCollectorProcessor()
    assert any(
        isinstance(o, TimelineObserver) for o in pipeline.get_observers(collector)
    )
    assert not any(isinstance(o, TimelineObserver) for o in pipeline.get_observers())

    # The collector reaches the observer list from the one task call site.
    assert "get_observers(metrics_collector)" in inspect.getsource(
        pipeline.create_pipeline_task
    )
    from app.ai.voice.agents.breeze_buddy import agent

    assert "metrics_collector=self.metrics_collector" in inspect.getsource(agent)

    # No processor is injected into the pipeline for this.
    built = inspect.getsource(pipeline.build_pipeline)
    assert "TimelineObserver(" not in built
    assert "_tap" not in built


@pytest.mark.asyncio
async def test_turn_without_metrics_is_not_emitted(collector, clock):
    """The emission rule predates the timeline and must not change.

    An outbound greeting plays pre-generated audio, so it raises no
    MetricsFrame and no function call, and never produced a turn. The console
    aligns turns to assistant messages BY LIST ORDER, so emitting one now
    would shift every message against the wrong turn.
    """
    await feed(
        collector,
        clock,
        [
            (0, TTSStartedFrame()),
            (280, audio_frame()),
            (40, BotStartedSpeakingFrame()),
            (3100, BotStoppedSpeakingFrame()),
        ],
        with_metrics=False,
    )
    assert collector.get_metrics() == []

    # A turn that DOES carry metrics is emitted, and carries its timeline.
    await feed(
        collector,
        clock,
        [
            (900, UserStartedSpeakingFrame()),
            (1000, UserStoppedSpeakingFrame()),
            (400, BotStartedSpeakingFrame()),
            (900, BotStoppedSpeakingFrame()),
        ],
    )
    (turn,) = collector.get_metrics()
    assert turn["turn"] == 1
    # The discarded greeting did not leak its clock into this turn.
    assert turn["timeline"][0]["at_ms"] == 0
    assert turn["summary"]["response_latency_ms"] == 400


@pytest.mark.asyncio
async def test_llm_ttft_found_under_the_real_processor_class_name(collector, clock):
    """The key is the Pipecat class name, not "llm".

    A fixed self._current_turn_metrics["llm"] lookup passed every synthetic
    test and would never have fired on a real call, where the key is
    AzureLLMService and changes with the configured provider.
    """
    await feed(
        collector,
        clock,
        [
            (0, UserStoppedSpeakingFrame()),
            (
                300,
                MetricsFrame(
                    data=[TTFBMetricsData(processor=STT_PROCESSOR, value=0.168)]
                ),
            ),
            (
                200,
                MetricsFrame(
                    data=[TTFBMetricsData(processor=LLM_PROCESSOR, value=0.42)]
                ),
            ),
            (
                100,
                MetricsFrame(
                    data=[TTFBMetricsData(processor=TTS_PROCESSOR, value=0.29)]
                ),
            ),
            (100, BotStartedSpeakingFrame()),
            (900, BotStoppedSpeakingFrame()),
        ],
        with_metrics=False,
    )

    (turn,) = collector.get_metrics()
    # Stored under the class name, with Pipecat's "#0" suffix stripped.
    assert set(turn["processors"]) == {
        "AzureLLMService",
        "ElevenLabsTTSService",
        "SonioxSTTServiceWithEndpointDelay",
    }
    assert turn["summary"]["llm_ttft_ms"] == 420


@pytest.mark.asyncio
async def test_observer_reports_each_moment_once_per_turn(collector, clock):
    """An observer is called per pipeline HOP, not per frame.

    A TranscriptionFrame crosses stt -> gate -> aggregator, so the same final
    transcript is reported several times; the timeline must carry one row.
    """
    from pipecat.frames.frames import LLMTextFrame

    from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (
        TimelineObserver,
    )

    observer = TimelineObserver(collector)

    async def report(gap_ms, frame):
        clock.advance(gap_ms)
        await observer.on_push_frame(pushed(frame, clock.ns))

    await feed(collector, clock, [(0, UserStartedSpeakingFrame())])
    await feed(collector, clock, [(1000, UserStoppedSpeakingFrame())])
    # The same transcript, reported at each hop it crosses.
    await report(170, transcription())
    await report(2, transcription())
    await report(2, transcription())
    await report(300, LLMTextFrame(text="Sure"))
    await report(1, LLMTextFrame(text=" thing"))
    await feed(collector, clock, [(80, BotStartedSpeakingFrame())])
    await feed(collector, clock, [(900, BotStoppedSpeakingFrame())])

    (turn,) = collector.get_metrics()
    assert events(turn).count("stt_final") == 1
    assert events(turn).count("llm_first_token") == 1
    # The FIRST report wins, so the row carries the moment it happened.
    assert at(turn)["stt_final"] == 1170


@pytest.mark.asyncio
async def test_a_late_observer_report_never_opens_a_turn(collector, clock):
    """The observer drains its own queue and can report after the commit.

    Letting that late report start the next turn would make a moment from the
    PREVIOUS turn the next turn's zero, shifting every offset after it.
    """
    from pipecat.frames.frames import LLMTextFrame

    from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (
        TimelineObserver,
    )

    observer = TimelineObserver(collector)

    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (1000, UserStoppedSpeakingFrame()),
            (400, BotStartedSpeakingFrame()),
            (900, BotStoppedSpeakingFrame()),
        ],
    )

    # Arrives after the turn closed, carrying a timestamp from inside it.
    stale = clock.ns - 500_000_000
    clock.advance(50)
    await observer.on_push_frame(pushed(LLMTextFrame(text="late"), stale))

    # The next turn starts on its OWN first event, at zero.
    await feed(
        collector,
        clock,
        [
            (500, UserStartedSpeakingFrame()),
            (1000, UserStoppedSpeakingFrame()),
            (300, BotStartedSpeakingFrame()),
            (800, BotStoppedSpeakingFrame()),
        ],
    )

    first, second = collector.get_metrics()
    assert "llm_first_token" not in events(second)
    assert second["timeline"][0]["event"] == "user_started_speaking"
    assert second["timeline"][0]["at_ms"] == 0
    assert second["summary"]["response_latency_ms"] == 300


@pytest.mark.asyncio
async def test_observer_never_lets_an_exception_escape(collector, clock):
    """Pipecat wraps the proxy handler in no try/except.

    A raise kills the draining task while the queue it fed keeps filling with
    every frame at every hop, audio payloads included, for the rest of the call.
    """
    from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (
        TimelineObserver,
    )

    observer = TimelineObserver(collector)

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    # The collector is what the observer calls into, so that is where a fault
    # would come from in practice.
    collector.note_stt_final = explode  # type: ignore[method-assign]

    await observer.on_push_frame(pushed(transcription(), clock.ns))

    # Reached, so nothing escaped and the draining task would still be alive.
    assert True


@pytest.mark.asyncio
async def test_tool_cancelled_by_a_barge_in_reports_no_latency(collector, clock):
    """A tool cancelled by a barge-in never sends a result frame.

    Its start time is therefore held for the rest of the call: one string key
    and one int, deliberately not cleared on the interruption. The frame is
    broadcast on EVERY user turn start, so clearing there would wipe the start
    time of a slow tool still in flight — exactly the calls whose latency
    matters most. No latency is reported for a call that never returned.
    """
    await feed(
        collector,
        clock,
        [
            (0, UserStoppedSpeakingFrame()),
            (
                300,
                FunctionCallInProgressFrame(
                    function_name="check_availability",
                    tool_call_id="never_returns",
                    arguments={},
                ),
            ),
            (400, BotStartedSpeakingFrame()),
            (900, InterruptionFrame()),
            (60, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    # It started, so the timeline says so — but it never returned, so there is
    # no latency to report and no row invented for it.
    assert "function_call_started" in events(turn)
    assert "function_call_result" not in events(turn)
    assert "functions" not in turn
    # The accepted leak: the pending start outlives the turn rather than being
    # cleared on a frame that also arrives on every ordinary turn.
    assert list(collector._function_starts) == ["never_returns"]


@pytest.mark.asyncio
async def test_ordinary_turn_is_not_marked_interrupted(collector, clock):
    """InterruptionFrame is broadcast on EVERY user turn start.

    The user aggregator calls broadcast_interruption() whenever a user turn
    begins and enable_interruptions is set — its default, and what this repo
    builds its turn strategies with. Treating the frame as a barge-in marked
    every completed reply as cut off.
    """
    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (1, InterruptionFrame()),
            (1200, UserStoppedSpeakingFrame()),
            (800, BotStartedSpeakingFrame()),
            (3000, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert "interrupted" not in turn["summary"]
    assert "time_to_interruption_ms" not in turn["summary"]
    assert "user_interrupted" not in events(turn)


@pytest.mark.asyncio
async def test_interruption_while_the_bot_speaks_is_a_barge_in(collector, clock):
    """Mid-reply is the only time the frame means what it looks like."""
    await feed(
        collector,
        clock,
        [
            (0, UserStartedSpeakingFrame()),
            (1, InterruptionFrame()),  # turn start — not a barge-in
            (1000, UserStoppedSpeakingFrame()),
            (400, BotStartedSpeakingFrame()),
            (1400, InterruptionFrame()),  # mid-reply — this one is
            (60, BotStoppedSpeakingFrame()),
        ],
    )

    (turn,) = collector.get_metrics()
    assert turn["summary"]["interrupted"] is True
    assert turn["summary"]["time_to_interruption_ms"] == 1400
    assert events(turn).count("user_interrupted") == 1


@pytest.mark.asyncio
async def test_push_time_before_the_turn_origin_never_goes_negative(collector, clock):
    """The observer stamps an upstream PUSH; the origin is the collector's own
    handling of the turn's first frame, which is always later."""
    from pipecat.frames.frames import LLMTextFrame

    from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (
        TimelineObserver,
    )

    observer = TimelineObserver(collector)

    await feed(collector, clock, [(0, LLMFullResponseStartFrame())])
    pushed_before_origin = clock.ns - 5_000_000  # 5ms earlier upstream
    await observer.on_push_frame(
        pushed(LLMTextFrame(text="Sure"), pushed_before_origin)
    )
    await feed(collector, clock, [(200, BotStartedSpeakingFrame())])
    await feed(collector, clock, [(900, BotStoppedSpeakingFrame())])

    (turn,) = collector.get_metrics()
    assert all(e["at_ms"] >= 0 for e in turn["timeline"])
    assert all(e.get("gap_ms", 0) >= 0 for e in turn["timeline"])
    # Gaps still sum to the offsets for the rest of the turn.
    running = 0
    for entry in turn["timeline"]:
        running += entry.get("gap_ms", 0)
        assert running == entry["at_ms"]


@pytest.mark.asyncio
async def test_stt_final_recorded_when_finalized_is_false(collector, clock):
    """`finalized` defaults to False and only Soniox sets it.

    Gating on it meant no stt_final at all on Deepgram calls, which this repo
    also builds.
    """
    from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (
        TimelineObserver,
    )

    observer = TimelineObserver(collector)
    deepgram_style = TranscriptionFrame(text="hi", user_id="u", timestamp="t")
    assert deepgram_style.finalized is False

    await feed(collector, clock, [(0, UserStartedSpeakingFrame())])
    await feed(collector, clock, [(1000, UserStoppedSpeakingFrame())])
    clock.advance(170)
    await observer.on_push_frame(pushed(deepgram_style, clock.ns))
    await feed(collector, clock, [(400, BotStartedSpeakingFrame())])
    await feed(collector, clock, [(900, BotStoppedSpeakingFrame())])

    (turn,) = collector.get_metrics()
    assert "stt_final" in events(turn)


@pytest.mark.asyncio
async def test_slow_tool_result_after_the_turn_keeps_its_latency(collector, clock):
    """A tool can legitimately outlive the turn that called it.

    Clearing the start times on every commit lost the latency of exactly the
    slowest calls in the system — the only ones that can span a boundary.
    """
    await feed(
        collector,
        clock,
        [
            (0, UserStoppedSpeakingFrame()),
            (
                300,
                FunctionCallInProgressFrame(
                    function_name="check_availability",
                    tool_call_id="slow",
                    arguments={},
                ),
            ),
            (400, BotStartedSpeakingFrame()),
            (900, BotStoppedSpeakingFrame()),
        ],
    )

    # The result lands after the turn committed.
    await feed(
        collector,
        clock,
        [
            (1500, UserStartedSpeakingFrame()),
            # Production ALWAYS broadcasts this at a user turn start; the slow
            # tool's start time must survive it.
            (0, InterruptionFrame()),
            (
                100,
                FunctionCallResultFrame(
                    function_name="check_availability",
                    tool_call_id="slow",
                    arguments={},
                    result="ok",
                ),
            ),
            (300, BotStartedSpeakingFrame()),
            (800, BotStoppedSpeakingFrame()),
        ],
    )

    _, second = collector.get_metrics()
    assert second["functions"][0]["name"] == "check_availability"
    # Started at 300ms, returned at 3200ms on the shared clock.
    assert second["functions"][0]["latency_ms"] == 2900.0
