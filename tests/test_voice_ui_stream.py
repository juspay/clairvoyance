"""Tests for VoiceUiStreamProcessor + the ui-action text coercion.

Covers the clairvoyance backend of widget voice-as-chat (VOICE_AS_CHAT.md):
  * A1 — generative UI over RTVI: prose passthrough (markers stripped so TTS
    never speaks JSON), ui-op emission reusing chat's heal/parse/validate,
    markers split across frames, per-template allowlist gating, and the
    per-assistant-response known-ids reset.
  * A2 — ui-action coercion: validation + whitespace trim + length cap.

The processor is exercised end-to-end through pipecat's run_test harness so
super().process_frame() + push_frame() behave exactly as in a live pipeline.
"""

from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.tests.utils import run_test as _pipecat_run_test

from app.ai.voice.agents.breeze_buddy.processors.voice_ui_stream import (
    VoiceUiStreamProcessor,
    coerce_ui_action_text,
)
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import resolve_allowlist


# pipecat 1.8's run_test defaults to start_timeout=1.0s, which the first test in
# a module loses to cold start (imports + event loop warm-up) — whichever test
# ran first failed with a TimeoutError. Give the pipeline room to start.
async def run_test(*args, **kwargs):
    kwargs.setdefault("start_timeout", 15.0)
    return await _pipecat_run_test(*args, **kwargs)


def _make(emit: AsyncMock, allowlist=None) -> VoiceUiStreamProcessor:
    return VoiceUiStreamProcessor(
        emit=emit,
        allowlist=allowlist if allowlist is not None else resolve_allowlist(),
    )


def _response(*text_frames: str) -> List[Frame]:
    """Wrap text deltas in a single bot response (start … deltas … end)."""
    frames: List[Frame] = [LLMFullResponseStartFrame()]
    frames.extend(LLMTextFrame(text=t) for t in text_frames)
    frames.append(LLMFullResponseEndFrame())
    return frames


def _spoken(down_frames) -> str:
    return "".join(f.text for f in down_frames if isinstance(f, LLMTextFrame))


def _emitted_ops(emit: AsyncMock):
    return [
        c.args[1]["op"] for c in emit.await_args_list if c.args and c.args[0] == "ui-op"
    ]


# ---------------------------------------------------------------------------
# A1 — generative UI over RTVI
# ---------------------------------------------------------------------------


async def test_prose_only_passes_through_no_ui_op():
    emit = AsyncMock()
    proc = _make(emit)
    down, _ = await run_test(proc, frames_to_send=_response("Hello there!"))
    assert _spoken(down) == "Hello there!"
    assert _emitted_ops(emit) == []


async def test_ui_stream_block_strips_markers_and_emits_ops():
    emit = AsyncMock()
    proc = _make(emit)
    block = (
        "Here are some options. "
        "<ui_stream>\n"
        '{"op":"add","id":"root","type":"Stack"}\n'
        '{"op":"add","id":"t1","type":"Text","parent":"root","props":{"text":"Hi"}}\n'
        "</ui_stream>"
        " Anything else?"
    )
    down, _ = await run_test(proc, frames_to_send=_response(block))

    spoken = _spoken(down)
    # The bot must never speak the JSON or the markers.
    assert "<ui_stream>" not in spoken
    assert "</ui_stream>" not in spoken
    assert '"op"' not in spoken
    assert "Here are some options." in spoken
    assert "Anything else?" in spoken

    ops = _emitted_ops(emit)
    assert len(ops) == 2
    assert ops[0]["id"] == "root" and ops[0]["type"] == "Stack"
    assert ops[1]["id"] == "t1" and ops[1]["type"] == "Text"


async def test_markers_split_across_frames():
    emit = AsyncMock()
    proc = _make(emit)
    parts = [
        "Look ",
        "<ui_st",
        "ream>\n",
        '{"op":"add","id":"roo',
        't","type":"Stack"}\n',
        "</ui_str",
        "eam> done",
    ]
    down, _ = await run_test(proc, frames_to_send=_response(*parts))

    spoken = _spoken(down)
    assert "ui_stream" not in spoken
    assert "Look" in spoken
    assert "done" in spoken

    ops = _emitted_ops(emit)
    assert len(ops) == 1
    assert ops[0]["id"] == "root" and ops[0]["type"] == "Stack"


async def test_disabled_primitive_is_not_emitted():
    emit = AsyncMock()
    allow = resolve_allowlist(enabled_groups=["core"], disabled_primitives=["Carousel"])
    proc = _make(emit, allow)
    block = '<ui_stream>\n{"op":"add","id":"root","type":"Carousel"}\n</ui_stream>'
    down, _ = await run_test(proc, frames_to_send=_response(block))

    # Carousel disabled for this template → dropped server-side, never emitted.
    assert _emitted_ops(emit) == []
    assert "Carousel" not in _spoken(down)


async def test_allowed_primitive_still_emits_with_restricted_allowlist():
    emit = AsyncMock()
    allow = resolve_allowlist(enabled_groups=["core"], disabled_primitives=["Carousel"])
    proc = _make(emit, allow)
    block = '<ui_stream>\n{"op":"add","id":"root","type":"Stack"}\n</ui_stream>'
    await run_test(proc, frames_to_send=_response(block))

    ops = _emitted_ops(emit)
    assert len(ops) == 1 and ops[0]["type"] == "Stack"


async def test_known_ids_reset_per_assistant_response():
    """A voice call is one long session; each bot response must start with a
    fresh id registry. Without the reset the healer would rename the second
    response's duplicate ``root`` to ``root__2`` (A1.3)."""
    emit = AsyncMock()
    proc = _make(emit)
    block = '<ui_stream>\n{"op":"add","id":"root","type":"Stack"}\n</ui_stream>'
    frames = _response(block) + _response(block)
    await run_test(proc, frames_to_send=frames)

    ops = _emitted_ops(emit)
    assert len(ops) == 2
    # Reset ⇒ both responses emit a clean ``root`` (no dedupe rename).
    assert all(op["id"] == "root" for op in ops)


async def test_unclosed_block_drops_partial_and_keeps_prose():
    emit = AsyncMock()
    proc = _make(emit)
    # Opens a block but never closes it before the response ends.
    block = 'before <ui_stream>\n{"op":"add","id":"root","ty'
    down, _ = await run_test(proc, frames_to_send=_response(block))

    spoken = _spoken(down)
    assert "before" in spoken
    assert "ui_stream" not in spoken
    # Partial JSONL inside the unclosed block is dropped, not emitted/spoken.
    assert _emitted_ops(emit) == []
    assert '"op"' not in spoken


# ---------------------------------------------------------------------------
# A2 — ui-action text coercion
# ---------------------------------------------------------------------------


def test_coerce_ui_action_valid():
    assert coerce_ui_action_text({"msg": "Tell me about Dawn"}, 2000) == (
        "Tell me about Dawn"
    )


def test_coerce_ui_action_trims_whitespace():
    assert coerce_ui_action_text({"msg": "  hi  "}, 2000) == "hi"


def test_coerce_ui_action_truncates_to_cap():
    assert coerce_ui_action_text({"msg": "x" * 50}, 10) == "x" * 10


def test_coerce_ui_action_rejects_blank():
    assert coerce_ui_action_text({"msg": "   "}, 2000) is None


def test_coerce_ui_action_rejects_missing():
    assert coerce_ui_action_text({}, 2000) is None
    assert coerce_ui_action_text(None, 2000) is None


def test_coerce_ui_action_rejects_non_string():
    assert coerce_ui_action_text({"msg": 123}, 2000) is None
    assert coerce_ui_action_text({"msg": {"x": 1}}, 2000) is None
