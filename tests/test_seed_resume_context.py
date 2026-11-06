# pyrefly: ignore-errors
# LLMContextMessage union narrowing — same limitation as the other
# block-codec tests.
"""_seed_resume_context — tool-message adjacency invariant.

The resume seed must be [role, task, system_block?, *history] with NO new
user message: the replayed history tail is an assistant tool_calls message
(+ tool results), and wedging the client-context system message between a
tool_calls and its tool responses is rejected by OpenAI and breaks the
Anthropic adapter's role merge (plan review, adversary:chat-design).
"""

from __future__ import annotations

from typing import Any, Dict, List

import app.ai.voice.agents.breeze_buddy.chat.agent as agent_module
from app.ai.voice.agents.breeze_buddy.chat.agent import (
    approval as agent_approval,
    context as agent_context,
    core as agent_core,
    cycle as agent_cycle,
    render_ui as agent_render_ui,
    tooling as agent_tooling,
)
from app.ai.voice.agents.breeze_buddy.chat.llm import driver as llm_driver

# agent.py is a package of subsystem modules now — a patched seam must
# land on every submodule that calls it (autoflake prunes unused
# imports per module, hence the hasattr guard).
_AGENT_MODULES = (
    agent_core,
    agent_cycle,
    agent_approval,
    agent_render_ui,
    agent_context,
    agent_tooling,
)


def _patch_agent_attr(monkeypatch, name, value):
    for _mod in _AGENT_MODULES:
        if hasattr(_mod, name):
            monkeypatch.setattr(_mod, name, value)


from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel


def _make_agent() -> ChatAgent:
    template = TemplateModel.model_construct(
        id="tpl-1",
        name="t",
        flow={},
        configurations=None,
    )
    return ChatAgent(
        session_id="sess-1",
        template=template,
        llm=object(),
        template_vars={},
    )


_NODE: Dict[str, Any] = {
    "name": "start",
    "role_messages": [{"role": "system", "content": "you are a bot"}],
    "task_messages": [{"role": "system", "content": "do the task"}],
    "functions": [],
}

_HISTORY: List[Dict[str, Any]] = [
    {"role": "user", "content": "refund my order"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "t1",
                "type": "function",
                "function": {"name": "issue_refund", "arguments": "{}"},
            }
        ],
    },
]


def test_resume_seed_has_no_user_message_and_history_is_tail():
    agent = _make_agent()
    ctx = agent._seed_resume_context(_NODE, list(_HISTORY), [])
    messages = ctx.get_messages()
    # Tail must be the replayed history verbatim — nothing after the
    # assistant tool_calls message.
    assert messages[-1]["role"] == "assistant"
    assert messages[-1].get("tool_calls")
    assert messages[-2] == {"role": "user", "content": "refund my order"}


def test_resume_seed_places_system_block_before_history(monkeypatch):
    _patch_agent_attr(
        monkeypatch,
        "render_client_context",
        lambda *a, **k: ("USER_BLOCK", "SYSTEM_BLOCK"),
    )
    agent = _make_agent()
    ctx = agent._seed_resume_context(_NODE, list(_HISTORY), [])
    messages = ctx.get_messages()

    system_positions = [
        i for i, m in enumerate(messages) if m.get("content") == "SYSTEM_BLOCK"
    ]
    first_history_pos = next(
        i for i, m in enumerate(messages) if m.get("content") == "refund my order"
    )
    assert len(system_positions) == 1
    assert system_positions[0] < first_history_pos
    # user_block never rides a resume turn (there is no user message).
    assert all("USER_BLOCK" not in str(m.get("content")) for m in messages)
    # And nothing sits between the tool_calls message and the seed's end.
    assert messages[-1].get("tool_calls")


def test_normal_seed_still_ends_with_user_message(monkeypatch):
    _patch_agent_attr(
        monkeypatch,
        "render_client_context",
        lambda *a, **k: (None, "SYSTEM_BLOCK"),
    )
    agent = _make_agent()
    ctx = agent._seed_context(_NODE, list(_HISTORY), "new question", [])
    messages = ctx.get_messages()
    assert messages[-1] == {"role": "user", "content": "new question"}
    assert messages[-2].get("content") == "SYSTEM_BLOCK"
