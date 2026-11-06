"""
Channel-shared knowledge base runtime adapter.

Voice (pipeline processor + boot injection), chat (per-turn seeding) and the
query_knowledge_base builtin all go through this thin layer over
``app.services.knowledge_base``. Every function here is FAIL-OPEN: on any
error/timeout it returns None and the conversation proceeds without KB
context (matching greeting-prep / MCP-load degradation patterns).
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    KnowledgeBaseConfig,
    KnowledgeBaseMode,
)
from app.core.logger import logger
from app.services.knowledge_base import (
    get_full_kb_text,
    get_kb_token_count,
    retrieve,
)
from app.services.knowledge_base.chunking import count_tokens

# Vertical-neutral on purpose — this default ships to every KB-enabled
# template (healthcare, logistics, commerce, ...); templates that want
# domain-specific grounding language set config.injection_instruction.
DEFAULT_KB_INSTRUCTION = (
    "When answering questions covered by the knowledge below, use ONLY that "
    "knowledge. If the answer is not present, say you don't have that "
    "information — do not guess or make up details. If different parts of "
    "the knowledge give conflicting or partial answers, mention both rather "
    "than picking one."
)

# Hard ceiling for explicit full_injection mode (AUTO uses the template's
# own threshold); keeps a misconfigured template from exploding the context.
_FULL_INJECTION_HARD_CAP_TOKENS = 60_000

# Hang protection: the shared Redis client and asyncpg pool are configured
# without socket/command timeouts, so the fail-open try/excepts alone cannot
# bound a stalled connection — every await below must carry its own bound.
_RESOLVE_TIMEOUT_SECS = 2.0
_FULL_TEXT_TIMEOUT_SECS = 10.0

ResolvedMode = Literal["full_injection", "auto_retrieve", "tool"]


@dataclass
class ResolvedKbRuntime:
    """A template's KB config with AUTO mode resolved to a concrete mode."""

    config: KnowledgeBaseConfig
    mode: ResolvedMode


async def resolve_kb_runtime(
    configurations: Optional[ConfigurationModel],
) -> Optional[ResolvedKbRuntime]:
    """Resolve the template's KB config to a concrete runtime mode.

    Returns None when no KB is configured/enabled. AUTO compares the
    (Redis-cached) total KB token count against the template threshold;
    if that lookup fails, falls back to auto_retrieve — never blocks boot.
    """
    if configurations is None or configurations.knowledge_base is None:
        return None
    config = configurations.knowledge_base
    if not config.enabled or not config.knowledge_base_ids:
        return None

    if config.mode == KnowledgeBaseMode.FULL_INJECTION:
        return ResolvedKbRuntime(config=config, mode="full_injection")
    if config.mode == KnowledgeBaseMode.AUTO_RETRIEVE:
        return ResolvedKbRuntime(config=config, mode="auto_retrieve")
    if config.mode == KnowledgeBaseMode.TOOL:
        return ResolvedKbRuntime(config=config, mode="tool")

    # AUTO: size-tiered pick.
    try:
        total_tokens = await asyncio.wait_for(
            get_kb_token_count(config.knowledge_base_ids),
            timeout=_RESOLVE_TIMEOUT_SECS,
        )
        if total_tokens <= config.full_injection_token_threshold:
            return ResolvedKbRuntime(config=config, mode="full_injection")
    except Exception as e:
        logger.warning(f"KB AUTO resolution failed, using auto_retrieve: {e}")
    return ResolvedKbRuntime(config=config, mode="auto_retrieve")


def _instruction(config: KnowledgeBaseConfig) -> str:
    """Template's custom grounding instruction, or the refusal-by-default
    one. Prepended to every injected KB block (full-injection and per-turn
    retrieval alike) — it's what tells the LLM to answer only from the
    provided material instead of guessing."""
    return config.injection_instruction or DEFAULT_KB_INSTRUCTION


def build_kb_system_message(
    kb_text: str, config: KnowledgeBaseConfig
) -> Dict[str, Any]:
    """Full-injection system message (stable across the conversation, so it
    stays prompt-cache friendly)."""
    return {
        "role": "system",
        "content": f"{config.injection_header}\n{_instruction(config)}\n\n{kb_text}",
    }


async def fetch_full_kb_text_cached(config: KnowledgeBaseConfig) -> Optional[str]:
    """Full KB text for full_injection mode (Redis-cached upstream).

    Fail-open: returns None on any error/timeout or when the KB outgrew the
    cap. AUTO-resolved fetches re-check the template's own threshold (the KB
    may have grown between resolution and fetch); explicit full_injection
    only enforces the platform hard cap.
    """
    max_tokens = (
        config.full_injection_token_threshold
        if config.mode == KnowledgeBaseMode.AUTO
        else _FULL_INJECTION_HARD_CAP_TOKENS
    )
    try:
        text, tokens = await asyncio.wait_for(
            get_full_kb_text(config.knowledge_base_ids, max_tokens=max_tokens),
            timeout=_FULL_TEXT_TIMEOUT_SECS,
        )
        return text or None
    except Exception as e:
        logger.warning(f"KB full-text fetch failed (continuing without KB): {e}")
        return None


def build_retrieval_query(
    user_query: str,
    recent_user_turns: Optional[List[str]],
    config: KnowledgeBaseConfig,
) -> str:
    """Join the current utterance with up to ``include_recent_turns`` prior
    user turns so anaphoric follow-ups ("how much is it?") retrieve well."""
    parts: List[str] = []
    if recent_user_turns and config.include_recent_turns > 0:
        parts.extend(recent_user_turns[-config.include_recent_turns :])
    parts.append(user_query)
    return "\n".join(part for part in parts if part and part.strip())


async def fetch_kb_context_message(
    config: KnowledgeBaseConfig,
    query: str,
    *,
    timeout: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve chunks for this turn and format the transient context message.

    Fail-open: timeout/error/no-hits all return None and the turn proceeds
    without KB context (logged with elapsed budget for latency tracking).
    """
    if not query or not query.strip():
        return None
    budget = timeout if timeout is not None else config.retrieval_timeout_secs
    try:
        chunks = await asyncio.wait_for(
            retrieve(
                kb_ids=config.knowledge_base_ids,
                query=query,
                top_k=config.top_k,
                score_threshold=config.score_threshold,
            ),
            timeout=budget,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"KB retrieval timed out after {budget}s; continuing without KB context"
        )
        return None
    except Exception as e:
        logger.warning(f"KB retrieval failed (continuing without KB context): {e}")
        return None

    if not chunks:
        return None

    # Enforce the per-turn context budget.
    lines: List[str] = [config.injection_header, _instruction(config), ""]
    used_tokens = 0
    included = 0
    for chunk in chunks:
        chunk_tokens = chunk.token_count or count_tokens(chunk.text)
        if included and used_tokens + chunk_tokens > config.max_context_tokens:
            break
        source = f" [Source: {chunk.document_name}]" if chunk.document_name else ""
        lines.append(f"-{source} {chunk.text}")
        used_tokens += chunk_tokens
        included += 1

    return {"role": "system", "content": "\n".join(lines)}
