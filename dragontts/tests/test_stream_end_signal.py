"""How an utterance ENDED must be visible, not inferred silently.

``ElevenLabsStreamPool.stream`` has two exits and they mean opposite things:

- ``_DONE`` — the server declared the turn finished (``is_final`` /
  ``is_final_audio_for_turn``). Known complete.
- idle timeout — the server merely went quiet for ``idle_timeout`` seconds.
  This is a GUESS. A mid-generation stall longer than the window is
  indistinguishable from a real ending, and the partial clip is returned with
  the same type, served, and (for the v3-conversational family, which stores
  the finished bytes) CACHED as if complete for its whole TTL.

Before this, both exits were silent and identical, so a truncated greeting was
undetectable from logs — it had to be diagnosed from waveform amplitude and
byte-count arithmetic. These tests pin the observability down:

- the idle-timeout exit WARNS and says the audio may be truncated;
- the is_final exit does not warn;
- the warning carries the frame-grid verdict, which is positive evidence: a
  complete generation is a whole number of audio frames, so a byte total that
  is not frame-aligned was cut mid-generation;
- the partial audio is still yielded (this is observability, not a behaviour
  change — callers must keep getting what arrived).
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from loguru import logger

from app.providers.elevenlabs_pool import ElevenLabsStreamPool
from tests.test_elevenlabs_v3 import BASE, V3_MODEL, VOICE, FakeConnect, _wait_for

MSG = {"text": "नमस्ते.", "voice_settings": {"stability": 0.4}}


@pytest.fixture
def logs():
    """Capture loguru records as (level, message) — caplog can't see loguru."""
    captured: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: captured.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    yield captured
    logger.remove(sink)


def _pool(connect: FakeConnect, **kwargs) -> ElevenLabsStreamPool:
    kwargs.setdefault("output_format", "pcm_8000")
    kwargs.setdefault("idle_timeout", 0.05)
    return ElevenLabsStreamPool(
        api_key="test-key",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        min_size=1,
        max_size=1,
        **kwargs,
    )


async def _drive(pool, chunks: list[bytes], *, send_final: bool) -> list[bytes]:
    """Feed ``chunks`` to one utterance; optionally declare the turn finished.

    With ``send_final=False`` the socket simply goes quiet, so the stream can
    only end via the idle timeout — the truncation-risk path.
    """
    await pool._conns[0].ready.wait()
    socket = pool._conns[0].ws
    collected: list[bytes] = []

    async def consume():
        async for chunk in pool.stream(MSG):
            collected.append(chunk)

    task = asyncio.create_task(consume())
    await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
    ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
    for chunk in chunks:
        socket.feed({"context_id": ctx_id, "audio": base64.b64encode(chunk).decode()})
    if send_final:
        socket.feed({"context_id": ctx_id, "is_final_audio_for_turn": True})
    await asyncio.wait_for(task, timeout=3.0)
    return collected


def _warnings(logs) -> list[str]:
    return [m for lvl, m in logs if lvl == "WARNING" and "IDLE TIMEOUT" in m]


# ---------------------------------------------------------------------------
# the two exits must be distinguishable
# ---------------------------------------------------------------------------


async def test_idle_timeout_exit_warns_about_possible_truncation(logs):
    """Server goes quiet -> we GUESSED the end. That guess must be logged."""
    pool = _pool(FakeConnect())
    await pool.start()
    try:
        chunks = await _drive(pool, [b"abcd"], send_final=False)
    finally:
        await pool.aclose()

    assert chunks == [b"abcd"], "partial audio must still reach the caller"
    warned = _warnings(logs)
    assert len(warned) == 1, f"expected one truncation warning, got {warned}"
    assert "MAY BE TRUNCATED" in warned[0]
    assert "no is_final" in warned[0]
    assert "chunks=1" in warned[0]
    assert "bytes=4" in warned[0]


async def test_is_final_exit_does_not_warn(logs):
    """A server-declared end is known-complete — it must not cry truncation."""
    pool = _pool(FakeConnect())
    await pool.start()
    try:
        chunks = await _drive(pool, [b"abcd"], send_final=True)
    finally:
        await pool.aclose()

    assert chunks == [b"abcd"]
    assert _warnings(logs) == []


async def test_warning_counts_every_chunk_received(logs):
    """chunks/bytes in the warning describe what actually arrived."""
    pool = _pool(FakeConnect())
    await pool.start()
    try:
        chunks = await _drive(pool, [b"ab", b"cd", b"ef"], send_final=False)
    finally:
        await pool.aclose()

    assert chunks == [b"ab", b"cd", b"ef"]
    warned = _warnings(logs)[0]
    assert "chunks=3" in warned
    assert "bytes=6" in warned


async def test_idle_window_does_not_apply_before_the_first_chunk(logs):
    """Silence BEFORE any audio must not end the utterance as "complete".

    The short idle window only means end-of-speech once speech has started;
    before that the stream waits on the long first-chunk timeout and then
    raises. If the short window leaked into the pre-audio wait, a cold start
    would return an EMPTY clip and cache it — worse than a truncated one.
    """
    pool = _pool(FakeConnect(), idle_timeout=0.05)
    await pool.start()
    collected: list[bytes] = []
    try:
        await pool._conns[0].ready.wait()

        async def consume():
            async for chunk in pool.stream(MSG):
                collected.append(chunk)

        task = asyncio.create_task(consume())
        # Stay quiet far longer than idle_timeout, well short of the 10s
        # first-chunk timeout: the stream must still be waiting, not finished.
        await asyncio.sleep(0.4)
        assert not task.done(), "idle window must not end a stream with no audio"
        assert _warnings(logs) == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await pool.aclose()
    assert collected == []


# ---------------------------------------------------------------------------
# the frame-grid verdict — positive evidence a clip was cut
# ---------------------------------------------------------------------------


def test_end_state_flags_off_grid_byte_totals():
    """A complete generation is a whole number of 0.04s frames; a prefix isn't."""
    pool = _pool(FakeConnect(), output_format="pcm_8000")
    frame = 8000 * 2 // 25  # 640 bytes

    aligned = pool._end_state(frame * 3)
    assert "frame-aligned" in aligned
    assert "OFF-GRID" not in aligned
    assert "0.12s" in aligned

    cut = pool._end_state(frame * 3 + 17)
    assert "OFF-GRID" in cut
    assert "cut mid-generation" in cut


def test_end_state_is_quiet_when_it_cannot_judge():
    """No bytes, or a non-pcm format, means no verdict rather than a wrong one."""
    pool = _pool(FakeConnect(), output_format="pcm_8000")
    assert pool._end_state(0) == ""

    opaque = _pool(FakeConnect(), output_format="mp3_44100_128")
    assert opaque._sample_rate == 0
    assert opaque._end_state(1234) == ""


@pytest.mark.parametrize(
    "output_format,expected",
    [("pcm_8000", 8000), ("pcm_16000", 16000), ("pcm_44100", 44100)],
)
def test_sample_rate_parsed_from_output_format(output_format, expected):
    pool = _pool(FakeConnect(), output_format=output_format)
    assert pool._sample_rate == expected


async def test_off_grid_verdict_reaches_the_warning(logs):
    """The grid verdict is only useful if it lands in the operator-facing line."""
    pool = _pool(FakeConnect(), output_format="pcm_8000")
    await pool.start()
    try:
        await _drive(pool, [b"x" * 100], send_final=False)  # 100 % 640 != 0
    finally:
        await pool.aclose()

    assert "OFF-GRID" in _warnings(logs)[0]
