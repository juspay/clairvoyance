# pyrefly: ignore-errors
# LLMContextMessage union narrowing — same limitation as the other
# block-codec tests.
"""Gemini thought-signature persistence round-trip through block_codec.

The chat brain is stateless per turn: signatures captured by the
llm_driver must survive the DB round-trip as internal content blocks and
decode back into the exact LLMSpecificMessage entries the Gemini adapter
re-applies by bookmark. They are LLM-context-only — every widget-facing
read path strips them via the visibility=internal marker.
"""

from __future__ import annotations

import base64

from pipecat.processors.aggregators.llm_context import LLMSpecificMessage

from app.ai.voice.agents.breeze_buddy.chat.history.block_codec import (
    VISIBILITY_INTERNAL,
    blocks_to_llm_context_messages,
    filter_visible_blocks,
    repair_dangling_tool_uses,
)
from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.signatures import (
    _VISIBILITY_INTERNAL,
    GEMINI_THOUGHT_SIGNATURE_BLOCK,
    gemini_signature_blocks,
)


def test_visibility_literal_stays_pinned_to_block_codec():
    """gemini_signatures cannot import block_codec (block_codec imports it),
    so it carries the visibility wire literal locally — pin them equal."""
    assert _VISIBILITY_INTERNAL == VISIBILITY_INTERNAL


def _sig_msg(
    bookmark: dict, signature: bytes = b"\x00\x01binary\xff"
) -> LLMSpecificMessage:
    return LLMSpecificMessage(
        llm="google",
        message={
            "type": "thought_signature",
            "signature": signature,
            "bookmark": bookmark,
        },
    )


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def test_encode_function_call_bookmark():
    blocks = gemini_signature_blocks([_sig_msg({"function_call": "fc-1"})])
    assert blocks == [
        {
            "type": GEMINI_THOUGHT_SIGNATURE_BLOCK,
            "signature": base64.b64encode(b"\x00\x01binary\xff").decode("ascii"),
            "bookmark": {"function_call": "fc-1"},
            "visibility": VISIBILITY_INTERNAL,
        }
    ]


def test_encode_skips_inline_data_bookmarks_and_foreign_messages():
    inline = _sig_msg({"inline_data": object()})
    foreign = LLMSpecificMessage(llm="anthropic", message={"type": "thought"})
    keep = _sig_msg({"text": "hello"})
    blocks = gemini_signature_blocks([inline, foreign, keep])
    assert len(blocks) == 1
    assert blocks[0]["bookmark"] == {"text": "hello"}


# ---------------------------------------------------------------------------
# Round-trip: encode → persist-shape row → decode
# ---------------------------------------------------------------------------


def test_round_trip_restores_llm_specific_message_before_assistant():
    original = _sig_msg({"function_call": "fc-42"})
    row_blocks = [
        {"type": "text", "text": "Looking that up."},
        {
            "type": "tool_use",
            "id": "fc-42",
            "name": "search_catalog",
            "input": {"query": "bras"},
        },
        *gemini_signature_blocks([original]),
    ]
    out = blocks_to_llm_context_messages(
        [{"role": "assistant", "content_blocks": row_blocks}]
    )
    assert len(out) == 2
    # Signature decodes BEFORE the assistant message it annotates — the
    # same stream order the live turn used.
    assert isinstance(out[0], LLMSpecificMessage)
    assert out[0] == original
    assert out[1]["role"] == "assistant"
    assert out[1]["tool_calls"][0]["id"] == "fc-42"


def test_round_trip_text_bookmark():
    original = _sig_msg({"text": "Here you go."}, signature=b"sig3")
    row_blocks = [
        {"type": "text", "text": "Here you go."},
        *gemini_signature_blocks([original]),
    ]
    out = blocks_to_llm_context_messages(
        [{"role": "assistant", "content_blocks": row_blocks}]
    )
    assert out[0] == original
    assert out[1] == {"role": "assistant", "content": "Here you go."}


def test_decode_drops_malformed_signature_blocks():
    row_blocks = [
        {"type": "text", "text": "hi"},
        {"type": GEMINI_THOUGHT_SIGNATURE_BLOCK},  # no signature/bookmark
        {
            "type": GEMINI_THOUGHT_SIGNATURE_BLOCK,
            "signature": "%%%not-base64%%%",
            "bookmark": {"function_call": "fc-1"},
        },
    ]
    out = blocks_to_llm_context_messages(
        [{"role": "assistant", "content_blocks": row_blocks}]
    )
    assert out == [{"role": "assistant", "content": "hi"}]


# ---------------------------------------------------------------------------
# Sanitization + repair interplay
# ---------------------------------------------------------------------------


def test_filter_visible_blocks_strips_signature_blocks():
    blocks = [
        {"type": "text", "text": "visible"},
        *gemini_signature_blocks([_sig_msg({"function_call": "fc-1"})]),
    ]
    assert filter_visible_blocks(blocks) == [{"type": "text", "text": "visible"}]


def test_repair_passes_llm_specific_messages_through():
    sig = _sig_msg({"function_call": "fc-1"})
    messages = [
        {"role": "user", "content": "add it"},
        sig,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "fc-1",
                    "type": "function",
                    "function": {"name": "update_cart", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "fc-1", "content": "{}"},
    ]
    repaired = repair_dangling_tool_uses(list(messages))
    assert repaired == messages
