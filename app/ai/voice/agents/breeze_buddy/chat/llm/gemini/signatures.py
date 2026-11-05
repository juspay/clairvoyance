"""Gemini thought-signature persistence codec — chat-mode only.

Gemini 3 + thinking REQUIRES every replayed ``functionCall`` part to
carry its ``thought_signature`` — without it Vertex rejects the request
with a 400 ("Function call is missing a thought_signature"). The chat
brain is stateless per turn, so signatures captured mid-stream must
survive the DB round-trip: encoded here into internal content blocks
(signature bytes → base64) on the write side, decoded back into the
pipecat ``LLMSpecificMessage`` the ``GeminiLLMAdapter`` re-applies by
bookmark on the read side.

Placement: provider-specific sibling of ``gemini_adapter_patch`` —
``block_codec`` stays the provider-NEUTRAL persistence seam and only
routes blocks of this type here during decode. This module is a leaf
(pipecat + logger only): ``block_codec`` imports us, so importing back
would be circular — which is why ``_VISIBILITY_INTERNAL`` is a local
literal, pinned equal to ``block_codec.VISIBILITY_INTERNAL`` by test.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Tuple

from pipecat.processors.aggregators.llm_context import (
    LLMContextMessage,
    LLMSpecificMessage,
)

from app.core.logger import logger

# Internal content-block type persisting a Gemini thought signature
# alongside the assistant row it annotates. Tagged visibility=internal so
# every widget-facing read path strips it via
# block_codec.filter_visible_blocks.
GEMINI_THOUGHT_SIGNATURE_BLOCK = "gemini_thought_signature"

# LLMSpecificMessage provider tag for Gemini — must match
# GeminiLLMAdapter.id_for_llm_specific_messages. Non-Google adapters
# filter entries with this tag out of their invocation params.
GOOGLE_LLM_TAG = "google"

# Wire literal for block_codec.VISIBILITY_INTERNAL (see module docstring
# for why it is not imported).
_VISIBILITY_INTERNAL = "internal"


def gemini_signature_blocks(
    messages: List[LLMSpecificMessage],
) -> List[Dict[str, Any]]:
    """Encode captured Gemini thought-signature messages as internal blocks.

    ``messages`` are the ``("context_message", …)`` events the llm_driver
    yielded during one LLM cycle. Only Google-tagged thought_signature
    entries are encoded; the signature (bytes) rides as base64 text.
    ``inline_data`` bookmarks are dropped: they hold a genai Blob (not
    JSON-serializable) and chat never replays inline_data parts, so a
    persisted copy could never re-match anyway.
    """
    blocks: List[Dict[str, Any]] = []
    for msg in messages:
        payload = msg.message
        if (
            msg.llm != GOOGLE_LLM_TAG
            or not isinstance(payload, dict)
            or payload.get("type") != "thought_signature"
        ):
            logger.warning(
                "[gemini_signatures] skipping non-signature LLMSpecificMessage "
                f"(llm={msg.llm!r})"
            )
            continue
        signature = payload.get("signature")
        bookmark = payload.get("bookmark") or {}
        if not signature:
            continue
        if "inline_data" in bookmark:
            logger.debug(
                "[gemini_signatures] dropping inline_data-bookmarked thought "
                "signature (not persistable)"
            )
            continue
        blocks.append(
            {
                "type": GEMINI_THOUGHT_SIGNATURE_BLOCK,
                "signature": base64.b64encode(signature).decode("ascii"),
                "bookmark": {
                    k: v for k, v in bookmark.items() if k in ("function_call", "text")
                },
                "visibility": _VISIBILITY_INTERNAL,
            }
        )
    return blocks


def split_signature_blocks(
    blocks: List[Dict[str, Any]],
) -> Tuple[List[LLMContextMessage], List[Dict[str, Any]]]:
    """Partition an assistant row's blocks into (decoded thought-signature
    messages, remaining blocks). Malformed signature blocks are dropped
    with a warning — a broken signature must never break history replay."""
    sig_messages: List[LLMContextMessage] = []
    plain_blocks: List[Dict[str, Any]] = []
    for block in blocks:
        if block.get("type") != GEMINI_THOUGHT_SIGNATURE_BLOCK:
            plain_blocks.append(block)
            continue
        encoded = block.get("signature")
        bookmark = block.get("bookmark")
        if not encoded or not isinstance(bookmark, dict) or not bookmark:
            logger.warning(
                "[gemini_signatures] dropping malformed "
                "gemini_thought_signature block"
            )
            continue
        try:
            signature = base64.b64decode(encoded)
        except (ValueError, TypeError):
            logger.warning(
                "[gemini_signatures] dropping gemini_thought_signature block "
                "with undecodable signature"
            )
            continue
        sig_messages.append(
            LLMSpecificMessage(
                llm=GOOGLE_LLM_TAG,
                message={
                    "type": "thought_signature",
                    "signature": signature,
                    "bookmark": dict(bookmark),
                },
            )
        )
    return sig_messages, plain_blocks


__all__ = [
    "GEMINI_THOUGHT_SIGNATURE_BLOCK",
    "GOOGLE_LLM_TAG",
    "gemini_signature_blocks",
    "split_signature_blocks",
]
