# pyrefly: ignore-errors
# Same TypedDict-union narrowing limitation as test_block_codec_visibility.py.
"""repair_dangling_tool_uses — both-direction provider-safety repair.

The replay invariant: every assistant tool_calls id must be answered by a
{role:"tool"} message, and every tool message must answer a preceding
tool_calls. The repair covers the two failure directions:

- decided-but-lost rows (crash/cancel between approval claim and result
  persist) → synthetic error result injected;
- window-truncation orphans (CHAT_HISTORY_REPLAY_LIMIT cuts a batch in
  half) → leading orphan tool messages dropped.

``exclude_ids`` semantics are load-bearing (plan review blocker): only the
ids the caller is about to answer itself may stay unanswered.
"""

from __future__ import annotations

import json

from app.ai.voice.agents.breeze_buddy.chat.block_codec import (
    repair_dangling_tool_uses,
)


def _assistant(tool_ids, content=None):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": tid,
                "type": "function",
                "function": {"name": f"fn_{tid}", "arguments": "{}"},
            }
            for tid in tool_ids
        ],
    }


def _tool(tid, content="{}"):
    return {"role": "tool", "tool_call_id": tid, "content": content}


def test_valid_history_passes_through_unchanged():
    messages = [
        {"role": "user", "content": "hi"},
        _assistant(["t1", "t2"]),
        _tool("t1"),
        _tool("t2"),
        {"role": "assistant", "content": "done"},
    ]
    assert repair_dangling_tool_uses(list(messages)) == messages


def test_dangling_tool_use_gets_synthetic_result():
    messages = [
        _assistant(["t1"]),
        {"role": "user", "content": "hello?"},
    ]
    repaired = repair_dangling_tool_uses(messages)
    assert repaired[1]["role"] == "tool"
    assert repaired[1]["tool_call_id"] == "t1"
    payload = json.loads(repaired[1]["content"])
    assert payload["status"] == "error"
    assert repaired[2] == {"role": "user", "content": "hello?"}


def test_partial_batch_gets_synthetic_for_missing_sibling_only():
    messages = [
        _assistant(["t1", "t2"]),
        _tool("t1", '{"ok": true}'),
    ]
    repaired = repair_dangling_tool_uses(messages)
    assert [m["tool_call_id"] for m in repaired[1:]] == ["t1", "t2"]
    assert json.loads(repaired[2]["content"])["status"] == "error"


def test_exclude_ids_stay_unanswered():
    """The approval handler excludes the claimed id + pending siblings —
    those must remain dangling for the resume turn to answer."""
    messages = [
        _assistant(["claimed", "pending_sib", "lost"]),
    ]
    repaired = repair_dangling_tool_uses(
        messages, exclude_ids={"claimed", "pending_sib"}
    )
    answered = [m["tool_call_id"] for m in repaired if m["role"] == "tool"]
    assert answered == ["lost"]


def test_orphan_leading_tool_result_dropped():
    """Window truncation can cut the assistant row off the top, leaving
    tool messages that answer nothing — they must be dropped."""
    messages = [
        _tool("from_truncated_batch"),
        {"role": "user", "content": "next question"},
        _assistant(["t9"]),
        _tool("t9"),
    ]
    repaired = repair_dangling_tool_uses(messages)
    assert repaired[0] == {"role": "user", "content": "next question"}
    assert [m.get("tool_call_id") for m in repaired if m["role"] == "tool"] == ["t9"]


def test_already_answered_id_not_duplicated():
    """A non-contiguous real answer is already-broken history; the repair
    must not add a duplicate answer for that id."""
    messages = [
        _assistant(["t1"]),
        {"role": "user", "content": "wedged"},
        _tool("t1"),
    ]
    repaired = repair_dangling_tool_uses(messages)
    answers = [m for m in repaired if m["role"] == "tool"]
    assert len(answers) == 1


def test_duplicate_tool_results_deduped_first_wins():
    """A historical cancel-race double write must self-heal on replay —
    providers reject two answers for one tool_use id."""
    messages = [
        _assistant(["t1"]),
        _tool("t1", '{"real": true}'),
        _tool("t1", '{"status": "error"}'),
    ]
    repaired = repair_dangling_tool_uses(messages)
    answers = [m for m in repaired if m["role"] == "tool"]
    assert len(answers) == 1
    assert answers[0]["content"] == '{"real": true}'


def test_consecutive_assistant_batches():
    """Back-to-back assistant tool_calls messages: the synthetic answer for
    the first batch must land between them, not after the second."""
    messages = [
        _assistant(["a1"]),
        _assistant(["b1"]),
        _tool("b1"),
    ]
    repaired = repair_dangling_tool_uses(messages)
    roles_and_ids = [(m["role"], m.get("tool_call_id")) for m in repaired]
    assert roles_and_ids == [
        ("assistant", None),
        ("tool", "a1"),
        ("assistant", None),
        ("tool", "b1"),
    ]


def test_multiple_batches_repaired_independently():
    messages = [
        _assistant(["a1"]),
        _tool("a1"),
        {"role": "user", "content": "more"},
        _assistant(["b1", "b2"]),
        _tool("b1"),
    ]
    repaired = repair_dangling_tool_uses(messages)
    b_answers = [
        m["tool_call_id"]
        for m in repaired
        if m["role"] == "tool" and m["tool_call_id"].startswith("b")
    ]
    assert b_answers == ["b1", "b2"]
