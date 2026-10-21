"""MeetingSession: start/stop once, batched idempotent writes, idle composed,
heartbeat/cap/rotation loops, all with fakes — no DB, no pipeline."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.ai.voice.agents.breeze_buddy.processors.transcription_gate import CaptureLine
from app.ai.voice.agents.breeze_buddy.recorder import session as session_mod
from app.ai.voice.agents.breeze_buddy.recorder.session import (
    FLUSH_AT_LINES,
    MAX_WRITE_FAILURES,
    MeetingConfig,
    MeetingSession,
)
from app.schemas.breeze_buddy.meetings import (
    DeliveryState,
    Meeting,
    MeetingEndReason,
    MeetingState,
)

NOW = datetime.now(timezone.utc)


def meeting(meeting_id="mtg_1", state=MeetingState.RECORDING, duration=None) -> Meeting:
    return Meeting(
        id=meeting_id,
        reseller_id="breeze",
        merchant_id="NOA",
        lead_id="lead_1",
        state=state,
        started_at=NOW,
        ended_at=None,
        duration_s=duration,
        end_reason=None,
        last_heartbeat_at=NOW,
        stt_provider="soniox",
        delivery_state=DeliveryState.PENDING,
        delivery_attempts=0,
        delivery_error=None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeGate:
    def __init__(self):
        self.captures: list[dict] = []
        self.stops = 0
        self.capturing = False

    def start_capture(self, **kw):
        self.captures.append(kw)
        self.capturing = True

    def stop_capture(self):
        self.stops += 1
        self.capturing = False


class FakeDb:
    """Stands in for app.database.accessor.breeze_buddy.meetings."""

    def __init__(self, monkeypatch, existing=None, fail_appends=0):
        self.appends: list[tuple] = []
        self.finishes: list = []
        self.heartbeats = 0
        self.heartbeat_alive = True
        self.existing = existing
        self.fail_appends = fail_appends
        m = monkeypatch
        m.setattr(
            session_mod.meetings_db, "get_recording_meeting_by_lead", self.get_recording
        )
        m.setattr(session_mod.meetings_db, "get_last_seq", self.get_last_seq)
        m.setattr(session_mod.meetings_db, "create_meeting", self.create)
        m.setattr(session_mod.meetings_db, "append_segments", self.append)
        m.setattr(session_mod.meetings_db, "heartbeat_meeting", self.heartbeat)
        m.setattr(session_mod.meetings_db, "finish_meeting", self.finish)

    async def get_recording(self, lead_id):
        return self.existing

    async def get_last_seq(self, meeting_id):
        return 41

    async def create(self, **kw):
        return meeting(kw["meeting_id"])

    async def append(self, meeting_id, start_seq, lines):
        if self.fail_appends > 0:
            self.fail_appends -= 1
            raise RuntimeError("db down")
        self.appends.append((meeting_id, start_seq, [l.text for l in lines]))
        return len(lines)

    async def heartbeat(self, meeting_id):
        self.heartbeats += 1
        return self.heartbeat_alive

    async def finish(self, meeting_id, reason, final_state=MeetingState.FINISHED):
        self.finishes.append((meeting_id, reason, final_state))
        if len(self.finishes) > 1:
            return None  # already closed
        return meeting(meeting_id, state=final_state, duration=2479)


class FakeStt:
    def __init__(self):
        self.disconnects = 0
        self.connects = 0

    async def _disconnect(self):
        self.disconnects += 1

    async def _connect(self):
        self.connects += 1


def cfg(**over) -> MeetingConfig:
    base = dict(
        stop_phrases=["hey buddy stop recording the meeting"],
        stop_threshold=0.9,
        max_duration_seconds=3600,
        heartbeat_seconds=3600,
        flush_seconds=3600,
        stt_rotate_seconds=0,
    )
    base.update(over)
    return MeetingConfig(**base)


def make(monkeypatch, db=None, **over):
    db = db or FakeDb(monkeypatch)
    gate = FakeGate()
    events: list = []

    async def emit(event, payload):
        events.append((event, payload))

    finished: list = []

    async def on_finished(m):
        finished.append(m)

    idle = SimpleNamespace(suppress_when=None)
    stt = FakeStt()
    s = MeetingSession(
        gate=gate,
        config=cfg(**over),
        lead_id="lead_1",
        merchant_id="NOA",
        reseller_id="breeze",
        stt_provider="soniox",
        idle_handler=idle,
        stt_service=stt,
        emit=emit,
        on_finished=on_finished,
    )
    return SimpleNamespace(
        s=s, db=db, gate=gate, events=events, idle=idle, stt=stt, finished=finished
    )


def line(text, speaker="1", t=0) -> CaptureLine:
    return CaptureLine(speaker=speaker, text=text, start_ms=t, end_ms=t + 500)


@pytest.mark.asyncio
async def test_start_opens_meeting_engages_gate_and_quiets_idle(monkeypatch):
    h = make(monkeypatch)
    m = await h.s.start()
    assert m.id.startswith("mtg_") and h.s.is_recording
    assert h.gate.captures[0]["stop_phrase"] == ["hey buddy stop recording the meeting"]
    assert h.gate.captures[0]["threshold"] == 0.9
    assert h.idle.suppress_when() is True
    assert h.events == [("recording_started", {"meeting_id": m.id})]
    assert await h.s.start() is m  # idempotent
    await h.s.stop(MeetingEndReason.VOICE)


@pytest.mark.asyncio
async def test_idle_predicate_composes_with_an_existing_one(monkeypatch):
    h = make(monkeypatch)
    pending = {"v": False}
    h.idle.suppress_when = lambda: pending["v"]
    await h.s.start()
    assert h.idle.suppress_when() is True  # recording wins
    await h.s.stop(MeetingEndReason.VOICE)
    assert h.idle.suppress_when() is False  # restored
    pending["v"] = True
    assert h.idle.suppress_when() is True  # the original still works


@pytest.mark.asyncio
async def test_lines_are_numbered_and_flushed_as_one_batch_on_stop(monkeypatch):
    h = make(monkeypatch)
    await h.s.start()
    await h.s.on_line(line("Let's start."))
    await h.s.on_line(line("Sure.", speaker="2"))
    assert h.s.pending_lines == 2 and h.db.appends == []
    m = await h.s.stop(MeetingEndReason.VOICE)
    assert h.db.appends == [(m.id, 0, ["Let's start.", "Sure."])]
    assert h.db.finishes[0][1] is MeetingEndReason.VOICE
    assert h.gate.stops == 1 and not h.s.is_recording
    assert h.events[-1] == ("recording_saved", {"meeting_id": m.id, "duration_s": 2479})
    assert h.finished == [m]


@pytest.mark.asyncio
async def test_a_full_buffer_flushes_early_with_continuous_numbering(monkeypatch):
    h = make(monkeypatch)
    await h.s.start()
    for i in range(FLUSH_AT_LINES + 2):
        await h.s.on_line(line(f"l{i}"))
    assert len(h.db.appends) == 1 and h.db.appends[0][1] == 0
    assert len(h.db.appends[0][2]) == FLUSH_AT_LINES
    await h.s.stop(MeetingEndReason.VOICE)
    assert h.db.appends[1][1] == FLUSH_AT_LINES and h.db.appends[1][2] == ["l50", "l51"]


@pytest.mark.asyncio
async def test_a_failed_batch_is_retried_with_the_same_seq(monkeypatch):
    db = FakeDb(monkeypatch, fail_appends=1)
    h = make(monkeypatch, db=db)
    await h.s.start()
    await h.s.on_line(line("a"))
    assert await h.s.flush() == 0 and h.s.pending_lines == 1  # kept
    await h.s.on_line(line("b"))
    assert await h.s.flush() == 2
    assert db.appends == [("mtg_" + h.s.meeting_id[4:], 0, ["a", "b"])]
    await h.s.stop(MeetingEndReason.VOICE)


@pytest.mark.asyncio
async def test_repeated_write_failures_give_the_meeting_up(monkeypatch):
    db = FakeDb(monkeypatch, fail_appends=MAX_WRITE_FAILURES)
    h = make(monkeypatch, db=db)
    await h.s.start()
    await h.s.on_line(line("a"))
    for _ in range(MAX_WRITE_FAILURES):
        await h.s.flush()
    assert not h.s.is_recording
    assert db.finishes[0][1] is MeetingEndReason.STALE


@pytest.mark.asyncio
async def test_stop_is_idempotent_across_doors(monkeypatch):
    h = make(monkeypatch)
    await h.s.start()
    first, second = await asyncio.gather(
        h.s.stop(MeetingEndReason.VOICE), h.s.stop(MeetingEndReason.MAX_DURATION)
    )
    assert (first is None) != (second is None)
    assert len(h.db.finishes) == 1 and h.gate.stops == 1


@pytest.mark.asyncio
async def test_resumes_an_existing_recording_meeting_after_its_last_seq(monkeypatch):
    db = FakeDb(monkeypatch, existing=meeting("mtg_old"))
    h = make(monkeypatch, db=db)
    m = await h.s.start()
    assert m.id == "mtg_old"
    await h.s.on_line(line("back"))
    await h.s.stop(MeetingEndReason.VOICE)
    assert db.appends == [("mtg_old", 42, ["back"])]


@pytest.mark.asyncio
async def test_heartbeat_finding_the_row_closed_stops_locally(monkeypatch):
    h = make(monkeypatch, heartbeat_seconds=1)
    h.db.heartbeat_alive = False
    await h.s.start()
    await asyncio.sleep(1.2)
    assert not h.s.is_recording and h.db.heartbeats == 1
    assert h.db.finishes[0][1] is MeetingEndReason.STALE


@pytest.mark.asyncio
async def test_duration_cap_ends_the_recording(monkeypatch):
    h = make(monkeypatch, max_duration_seconds=1)
    await h.s.start()
    await asyncio.sleep(1.2)
    assert not h.s.is_recording
    assert h.db.finishes[0][1] is MeetingEndReason.MAX_DURATION


@pytest.mark.asyncio
async def test_stt_rotation_reconnects_in_place(monkeypatch):
    h = make(monkeypatch, stt_rotate_seconds=1)
    await h.s.start()
    await asyncio.sleep(1.2)
    assert (h.stt.disconnects, h.stt.connects) == (1, 1)
    assert h.s.is_recording
    await h.s.stop(MeetingEndReason.VOICE)


@pytest.mark.asyncio
async def test_config_loads_from_dynamic_keys(monkeypatch):
    async def fake(name, default, typ):
        return default

    monkeypatch.setattr("app.core.config.dynamic.get_config", fake)
    c = await MeetingConfig.load()
    assert (
        c.stop_phrases[0] == "hey buddy stop recording the meeting"
        and "stop recording" in c.stop_phrases
    )
    assert c.max_duration_seconds == 3 * 60 * 60 and c.flush_seconds == 15
