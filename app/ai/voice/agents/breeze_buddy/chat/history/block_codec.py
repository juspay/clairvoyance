"""Anthropic-shape ↔ OpenAI/LLMContext-shape conversion for chat history.

Persistence format (``chat_message.content_blocks`` JSONB) is canonical
Anthropic content blocks. Pipecat's ``LLMContext`` speaks OpenAI shape
internally and converts on the wire to whichever provider is in use.
This module is the seam between those two shapes.

Why Anthropic-shape on disk:
- Pairs cleanly with the existing ``chat_message`` ``role`` CHECK
  constraint (``user``/``assistant`` only — tool_result blocks live
  inside a user row).
- Matches every reference implementation in the research set
  (`shop-chat-agent`, `claude-cookbooks`, `mcp-python-sdk`).
- Provider-stable: when we eventually swap Claude for a non-Anthropic
  model, the on-disk shape is still the standard.

Block shapes
------------
Assistant row blocks::

    [
        {"type": "text", "text": "..."},                         # optional
        {"type": "tool_use", "id": "...", "name": "...",
         "input": {...}},                                        # 0..N
        {"type": "gemini_thought_signature",                     # 0..N,
         "signature": "<base64>", "bookmark": {...},             # internal
         "visibility": "internal"},                              # only
    ]

User row blocks (text turn)::

    [{"type": "text", "text": "..."}]

User row blocks (tool-result turn — emitted between tool cycles)::

    [
        {"type": "tool_result", "tool_use_id": "...", "content": "..."},
        ...
    ]
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, cast

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import (
    LLMContextMessage,
    LLMSpecificMessage,
)

# Provider-specific sub-codec: this module stays provider-neutral and only
# ROUTES gemini_thought_signature blocks to their own codec during decode
# (see gemini_signatures for the why/how). New provider persistence quirks
# should follow the same pattern rather than growing inline here.
from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.signatures import (
    split_signature_blocks,
)
from app.core.logger import logger

# A content_block carrying this visibility marker is LLM-context only:
# the history loader concatenates it into the LLM's prior-turn memory,
# but every widget-facing read path strips it before responding. This
# is how internal-only memory (rendered-UI summaries today; tool-intent
# traces, refine_ui hints, eval breadcrumbs tomorrow) rides on the
# canonical content_blocks pipe without leaking onto the user's screen.
VISIBILITY_INTERNAL = "internal"

# ---------------------------------------------------------------------------
# Encoding: write side (turn-loop → DB)
# ---------------------------------------------------------------------------


def assistant_turn_to_blocks(
    text: str,
    tool_calls: List[FunctionCallFromLLM],
) -> List[Dict[str, Any]]:
    """Build an Anthropic-shape block list for one assistant turn-step.

    ``text`` may be empty (the LLM emitted only a tool_use); ``tool_calls``
    may be empty (the LLM emitted only prose). At least one of the two
    is always present when the caller invokes this — otherwise the turn
    step is a no-op and should not be persisted.
    """
    blocks: List[Dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for call in tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.tool_call_id,
                "name": call.function_name,
                "input": dict(call.arguments),
            }
        )
    return blocks


def tool_results_to_user_blocks(
    pairs: List[tuple],
) -> List[Dict[str, Any]]:
    """Coalesce N tool results into one Anthropic-shape user turn.

    ``pairs`` is a list of ``(tool_call_id, result_payload)``. The
    result_payload is whatever our dispatcher returned (FlowResult-ish
    dict or string) — we JSON-encode for the on-disk content.
    """
    blocks: List[Dict[str, Any]] = []
    for tool_call_id, result_payload in pairs:
        if isinstance(result_payload, str):
            encoded = result_payload
        else:
            try:
                encoded = json.dumps(result_payload, default=str)
            except (TypeError, ValueError):
                encoded = str(result_payload)
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": encoded,
            }
        )
    return blocks


def plain_text_blocks(text: str) -> List[Dict[str, Any]]:
    """Single-element [text] block. Used for the user's prose question
    and for the final assistant prose reply."""
    return [{"type": "text", "text": text}]


def internal_text_block(text: str) -> Dict[str, Any]:
    """A text block flagged visibility=internal — LLM-context only.

    The history loader concatenates this into the LLM's prior-turn
    memory; widget-facing read paths strip it via [[filter_visible_blocks]].
    """
    return {"type": "text", "text": text, "visibility": VISIBILITY_INTERNAL}


# ---------------------------------------------------------------------------
# Visibility filtering: widget-facing read side
# ---------------------------------------------------------------------------


def filter_visible_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop any block tagged ``visibility=internal``.

    Pre-fix rows that carried the summary inline in a normal text block
    are left untouched — minimal-blast-radius fix that only governs new
    writes. Existing transcripts continue to display the legacy
    ``[ui rendered: …]`` tail until they age out.
    """
    if not blocks:
        return blocks
    return [b for b in blocks if b.get("visibility") != VISIBILITY_INTERNAL]


# ---------------------------------------------------------------------------
# Decoding: read side (DB → LLMContext seed)
# ---------------------------------------------------------------------------


def blocks_to_llm_context_messages(
    rows: List[Dict[str, Any]],
) -> List[LLMContextMessage]:
    """Convert a chronological list of chat_message rows into the
    OpenAI-shape LLMContextMessage list that ``LLMContext`` consumes.

    Each row is a dict with at least ``role`` and ``content_blocks``
    (or legacy ``content``). Tool_result blocks expand into multiple
    ``{role:"tool"}`` messages. Unknown block types are dropped with a
    warning so a forward-incompatible block never breaks replay.
    """
    out: List[LLMContextMessage] = []

    for row in rows:
        role = row.get("role")
        blocks = row.get("content_blocks")
        fallback_text = row.get("content")

        # Pre-migration rows or rows somehow missing content_blocks:
        # synthesise a text block from the denormalised content column.
        if not blocks and fallback_text:
            blocks = [{"type": "text", "text": fallback_text}]
        if not blocks:
            continue

        if role == "assistant":
            # Thought-signature blocks decode into LLMSpecificMessage
            # entries placed immediately BEFORE the assistant message they
            # annotate — the same stream order the live turn used
            # (signatures arrive mid-stream, the assistant message is
            # appended after stream close). The Gemini adapter extracts
            # them wherever they sit and re-applies by bookmark, but their
            # relative order against assistant messages must be preserved.
            sig_messages, plain_blocks = split_signature_blocks(blocks)
            out.extend(sig_messages)
            out.append(_assistant_row_to_openai(plain_blocks))
        elif role == "user":
            out.extend(_user_row_to_openai(blocks))
        else:
            logger.warning(f"[block_codec] skipping unknown role {role!r}")

    return out


_LOST_RESULT_PAYLOAD = json.dumps(
    {
        "status": "error",
        "reason": (
            "the execution result for this call was lost; " "treat this call as failed"
        ),
    }
)


def repair_dangling_tool_uses(
    messages: List[LLMContextMessage],
    exclude_ids: Optional[Set[str]] = None,
) -> List[LLMContextMessage]:
    """Make a replayed history provider-safe in BOTH directions.

    1. Dangling tool_use: an assistant message carries ``tool_calls`` with
       no answering ``{role:"tool"}`` message anywhere later — inject a
       synthetic error result right after that batch's contiguous tool-run.
       Covers crash/cancel windows where an approval was DECIDED but its
       result write was lost (those rows are NOT re-claimable by
       resolve_dangling_approvals, which only touches PENDING rows).
    2. Orphan tool_result: a ``{role:"tool"}`` message whose tool_call_id
       has no preceding assistant ``tool_calls`` in the (windowed) list —
       dropped. Happens when CHAT_HISTORY_REPLAY_LIMIT cuts a batch in
       half at the window boundary.

    ``exclude_ids`` are tool_call ids the CALLER is about to answer itself
    and must stay unanswered here: the approval handler passes the claimed
    id plus the still-PENDING sibling ids. The ``/message`` path passes
    nothing (every pending row was resolved + persisted before the history
    load). Never exclude all decided ids — a decided-but-lost row must be
    repaired or the session bricks.
    """
    exclude: Set[str] = exclude_ids or set()
    # Ids answered ANYWHERE in the window — a non-contiguous real answer is
    # already-broken history we won't make worse by adding a duplicate.
    answered_global: Set[str] = {
        cast(str, m.get("tool_call_id"))
        for m in messages
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id")
    }

    repaired: List[LLMContextMessage] = []
    declared_so_far: Set[str] = set()
    # First answer wins — a second tool_result for the same id (e.g. a
    # historical cancel-race double write) would make providers reject the
    # whole conversation on every replay, so later duplicates are dropped.
    answered_kept: Set[str] = set()

    def _keep_tool(tmsg: Dict[str, Any]) -> None:
        tcid = tmsg.get("tool_call_id")
        if tcid not in declared_so_far:
            logger.warning(
                f"[block_codec] dropping orphan tool_result "
                f"{tcid!r} (no preceding tool_use in replay window)"
            )
            return
        if tcid in answered_kept:
            logger.warning(
                f"[block_codec] dropping duplicate tool_result for {tcid!r} "
                "(first answer wins)"
            )
            return
        answered_kept.add(cast(str, tcid))
        repaired.append(cast(LLMContextMessage, tmsg))

    n = len(messages)
    i = 0
    while i < n:
        # LLM-specific entries (Gemini thought signatures) are opaque to
        # the repair pass — they carry no tool_use/tool_result semantics.
        # Pass them through in place.
        if isinstance(messages[i], LLMSpecificMessage):
            repaired.append(messages[i])
            i += 1
            continue
        msg = cast(Dict[str, Any], messages[i])
        role = msg.get("role")

        if role == "tool":
            _keep_tool(msg)
            i += 1
            continue

        repaired.append(cast(LLMContextMessage, msg))
        tool_calls = msg.get("tool_calls") if role == "assistant" else None
        if tool_calls:
            batch_ids = [tc.get("id") for tc in tool_calls if tc.get("id")]
            declared_so_far.update(batch_ids)
            # Consume the contiguous run of answering tool messages.
            j = i + 1
            while (
                j < n
                and isinstance(messages[j], dict)
                and cast(Dict[str, Any], messages[j]).get("role") == "tool"
            ):
                _keep_tool(cast(Dict[str, Any], messages[j]))
                j += 1
            for tcid in batch_ids:
                if tcid in exclude or tcid in answered_global:
                    continue
                logger.warning(
                    f"[block_codec] injecting synthetic result for dangling "
                    f"tool_use {tcid!r}"
                )
                repaired.append(
                    cast(
                        LLMContextMessage,
                        {
                            "role": "tool",
                            "tool_call_id": tcid,
                            "content": _LOST_RESULT_PAYLOAD,
                        },
                    )
                )
            i = j
            continue

        i += 1

    return repaired


def _assistant_row_to_openai(blocks: List[Dict[str, Any]]) -> LLMContextMessage:
    """Anthropic assistant blocks → one OpenAI-shape assistant message.

    Concatenates text blocks; collects tool_use blocks into the
    OpenAI ``tool_calls`` array.
    """
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if t:
                text_parts.append(t)
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
        else:
            logger.warning(
                f"[block_codec] skipping unknown assistant block type {btype!r}"
            )

    msg: Dict[str, Any] = {"role": "assistant"}
    if text_parts:
        msg["content"] = "".join(text_parts)
    else:
        msg["content"] = None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return cast(LLMContextMessage, msg)


def _user_row_to_openai(
    blocks: List[Dict[str, Any]],
) -> List[LLMContextMessage]:
    """Anthropic user blocks → 1+ OpenAI messages.

    Text-only blocks → one ``{role:"user"}`` message. Tool_result
    blocks → one ``{role:"tool"}`` message per result. A row can in
    theory mix both (Anthropic allows it); we emit the tool_result
    messages first (closer to the originating tool_use), then a
    user-text message if any.
    """
    text_parts: List[str] = []
    tool_msgs: List[LLMContextMessage] = []

    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if t:
                text_parts.append(t)
        elif btype == "tool_result":
            tool_msgs.append(
                cast(
                    LLMContextMessage,
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id"),
                        "content": block.get("content") or "",
                    },
                )
            )
        else:
            logger.warning(f"[block_codec] skipping unknown user block type {btype!r}")

    msgs: List[LLMContextMessage] = list(tool_msgs)
    if text_parts:
        msgs.append(
            cast(
                LLMContextMessage,
                {"role": "user", "content": "".join(text_parts)},
            )
        )
    return msgs


__all__ = [
    "VISIBILITY_INTERNAL",
    "assistant_turn_to_blocks",
    "tool_results_to_user_blocks",
    "plain_text_blocks",
    "internal_text_block",
    "filter_visible_blocks",
    "blocks_to_llm_context_messages",
    "repair_dangling_tool_uses",
]
