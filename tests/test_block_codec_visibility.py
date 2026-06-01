# pyrefly: ignore-errors
# Same TypedDict-union narrowing limitation as test_ui_stream.py —
# blocks_to_llm_context_messages returns LLMContextMessage (a Union of
# LLMSpecificMessage variants); pyrefly can't narrow the indexable
# variant from a runtime ``role`` check.
"""Tests for the visibility-flag split that keeps LLM-only blocks
(rendered-UI summaries today; agent-memory breadcrumbs tomorrow) off
the widget's wire while preserving them for the LLM's next-turn replay.

Companion: [[block_codec.py:filter_visible_blocks]] and the agent.py
persistence path that emits ``visibility=internal`` text blocks.
"""

from __future__ import annotations

import os

from app.ai.voice.agents.breeze_buddy.chat.block_codec import (
    VISIBILITY_INTERNAL,
    blocks_to_llm_context_messages,
    filter_visible_blocks,
    internal_text_block,
)

# Handler import needs JWT_SECRET_KEY at module load time. Tests don't
# verify tokens, so a dummy value is fine.
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-not-used-by-these-tests")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

from app.api.routers.breeze_buddy.chat.handlers import (  # noqa: E402
    _sanitize_messages_for_widget,
)
from app.schemas.breeze_buddy.chat import ChatMessage, ChatMessageRole  # noqa: E402


def _mk(idx, **kwargs):
    return ChatMessage(
        session_id="s1", idx=idx, role=ChatMessageRole.ASSISTANT, **kwargs
    )


# ---------------------------------------------------------------------------
# filter_visible_blocks — widget-facing read side
# ---------------------------------------------------------------------------


def test_filter_drops_internal_text_blocks():
    blocks = [
        {"type": "text", "text": "Hi there!"},
        internal_text_block("[ui rendered: 1 Tile(s): 'Mug']"),
        {"type": "tool_use", "id": "t1", "name": "search", "input": {}},
    ]
    out = filter_visible_blocks(blocks)
    assert len(out) == 2
    assert out[0] == {"type": "text", "text": "Hi there!"}
    assert out[1]["type"] == "tool_use"


def test_filter_passes_text_blocks_through_unchanged_when_clean():
    blocks = [{"type": "text", "text": "Clean prose."}]
    assert filter_visible_blocks(blocks) == blocks


def test_filter_leaves_pre_fix_rows_alone():
    # Existing rows persisted before the visibility split carry the
    # summary inline. The fix is forward-only; legacy rows pass through
    # unchanged so we don't need a backfill.
    legacy = [
        {
            "type": "text",
            "text": (
                "Here are a few options.\n\n"
                "[ui rendered: 2 Tile(s): 'Mug', 'Bottle']"
            ),
        }
    ]
    assert filter_visible_blocks(legacy) == legacy


def test_filter_handles_empty_input():
    assert filter_visible_blocks([]) == []
    assert filter_visible_blocks(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LLM-side replay — visibility flag must NOT filter on this path
# ---------------------------------------------------------------------------


def test_llm_replay_keeps_internal_text_in_context():
    rows = [
        {
            "role": "assistant",
            "content": "Here is a Mug.",
            "content_blocks": [
                {"type": "text", "text": "Here is a Mug."},
                internal_text_block("[ui rendered: 1 Tile(s): 'Mug']"),
            ],
        }
    ]
    messages = blocks_to_llm_context_messages(rows)
    assert len(messages) == 1
    # Both blocks flow into the LLM's prior-turn memory: visible prose
    # plus the internal summary, concatenated in storage order.
    assert messages[0]["content"] == "Here is a Mug.[ui rendered: 1 Tile(s): 'Mug']"


def test_internal_text_block_carries_visibility_marker():
    block = internal_text_block("noted")
    assert block["type"] == "text"
    assert block["text"] == "noted"
    assert block["visibility"] == VISIBILITY_INTERNAL


# ---------------------------------------------------------------------------
# _sanitize_messages_for_widget — handler-level row pruning
# ---------------------------------------------------------------------------


def test_ui_only_row_preserved_with_content_cleared():
    """UI-only assistant turns (LLM rendered a Carousel with no prose)
    persist as ``content_blocks=[internal_summary]`` plus ``ui_blocks``.
    The widget needs ui_blocks delivered so it can repaint tiles on
    resume — without this case the entire row would be dropped and the
    UI state would vanish on refresh."""
    row = _mk(
        1,
        content=None,
        content_blocks=[internal_text_block("[ui rendered: 1 Tile(s)]")],
        ui_blocks=[
            {"op": "add", "id": "root", "type": "Carousel", "props": {}},
            {
                "op": "add",
                "id": "t-1",
                "type": "Tile",
                "parent": "root",
                "props": {"title": "X"},
            },
        ],
    )
    out = _sanitize_messages_for_widget([row])
    assert (
        len(out) == 1
    ), "UI-only row was dropped — widget would lose tile state on resume"
    assert out[0].content is None
    assert (
        out[0].content_blocks is None
    ), "content_blocks must be cleared so no empty bubble renders"
    assert (
        out[0].ui_blocks == row.ui_blocks
    ), "ui_blocks must survive for widget tile replay"


def test_summary_only_row_dropped_when_no_ui_blocks():
    row = _mk(
        2,
        content=None,
        content_blocks=[internal_text_block("[ui rendered: …]")],
        ui_blocks=None,
    )
    assert _sanitize_messages_for_widget([row]) == []


def test_mixed_row_keeps_visible_and_ui_blocks():
    row = _mk(
        3,
        content="Here are some bottles.",
        content_blocks=[
            {"type": "text", "text": "Here are some bottles."},
            internal_text_block("[ui rendered: 2 Tile(s)]"),
        ],
        ui_blocks=[{"op": "add", "id": "root", "type": "Carousel"}],
    )
    out = _sanitize_messages_for_widget([row])
    assert len(out) == 1
    assert len(out[0].content_blocks) == 1
    assert out[0].content_blocks[0]["text"] == "Here are some bottles."
    assert out[0].ui_blocks == row.ui_blocks


def test_row_without_content_blocks_untouched():
    """Greeting-style rows (legacy or pre-blocks) pass through verbatim."""
    row = _mk(4, content="Hi!", content_blocks=None)
    out = _sanitize_messages_for_widget([row])
    assert len(out) == 1
    assert out[0].content == "Hi!"
    assert out[0].content_blocks is None
