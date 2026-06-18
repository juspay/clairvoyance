"""LLM-based fact extraction and consolidation.

One Azure LLM call per conversation. Returns ADD/UPDATE/DELETE ops
against the user's existing fact set. App-side cosine over the small
per-user fact list acts as a dedup safety net before INSERT.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

from pipecat.adapters.services.open_ai_adapter import OpenAILLMInvocationParams

from app.ai.voice.agents.breeze_buddy.llm import _resolve_azure
from app.core.logger import logger
from app.schemas.breeze_buddy.memory import UserMemory

_SYSTEM_PROMPT = """You are a memory curator for a voice/chat AI assistant.

Given:
- KNOWN_FACTS: existing durable facts about the customer (may be empty)
- TRANSCRIPT: a conversation transcript

Output a JSON list of memory operations. Each operation has:
  "op": "ADD" | "UPDATE" | "DELETE"
  "fact": short sentence (one durable fact about the customer)
  "category": one of ["preference", "attribute", "outcome", "context"]
  "structured": optional dict with machine-readable fields, e.g. {"name": "Amreet"}
  "supersedes_fact": (UPDATE/DELETE only) the closest matching text from KNOWN_FACTS

Rules:
- Only capture facts durable enough to be useful in a future conversation.
- ALWAYS capture: the customer's name or preferred form of address, any explicit
  corrections they make to previously stated information, travel preferences,
  stated outcomes, and personal attributes they volunteer.
- A customer stating their name is NOT a greeting to ignore — it is a high-value
  attribute. Capture it with category "attribute" and structured {"name": "<value>"}.
- Ignore: passwords, full card numbers, OTPs, bank account numbers, and one-time
  transactional details with no future value. Do NOT treat a customer's name or
  identity as PII to ignore.
- If a new fact contradicts or corrects a KNOWN_FACT, emit UPDATE (not ADD).
  Set supersedes_fact to the closest matching text from KNOWN_FACTS.
- If a known fact is confirmed still true, emit nothing.
- If no facts are worth storing, return [].
- Keep each fact concise (one sentence).

Return ONLY valid JSON — a list of operation objects, no markdown fences."""


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if not mag_a or not mag_b:
        return 0.0
    return dot / (mag_a * mag_b)


def _find_duplicate(
    new_embedding: Optional[List[float]],
    new_fact: str,
    existing: List[UserMemory],
    cos_threshold: float = 0.92,
) -> Optional[UserMemory]:
    """Return the existing memory that is semantically equivalent, or None."""
    # Embedding-based dedup
    if new_embedding:
        for mem in existing:
            if mem.embedding:
                sim = _cosine_similarity(new_embedding, mem.embedding)
                if sim >= cos_threshold:
                    return mem

    # Exact-text fallback
    norm_new = new_fact.strip().lower()
    for mem in existing:
        if mem.fact.strip().lower() == norm_new:
            return mem

    return None


async def consolidate(
    existing_facts: List[UserMemory],
    transcript: List[Dict[str, Any]],
    extraction_prompt: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run the LLM extraction and return raw op dicts.

    Uses `extraction_prompt` if provided (template-level override), otherwise
    falls back to the built-in `_SYSTEM_PROMPT`.
    The worker applies these ops against the DB (insert/supersede).
    Returns [] on failure (safe to ignore).
    """
    if not transcript:
        return []

    try:
        llm = await _resolve_azure(None)

        system_prompt = extraction_prompt or _SYSTEM_PROMPT

        known_lines = (
            "\n".join(f"- [{m.category or 'fact'}] {m.fact}" for m in existing_facts)
            if existing_facts
            else "(none)"
        )
        convo_text = "\n".join(
            f"{t['role']}: {t['content']}"
            for t in transcript
            if t.get("role") in ("user", "assistant") and t.get("content")
        )

        params = OpenAILLMInvocationParams(  # type: ignore[call-overload]
            messages=[  # type: ignore[arg-type]
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"KNOWN_FACTS:\n{known_lines}\n\n" f"TRANSCRIPT:\n{convo_text}"
                    ),
                },
            ]
        )
        chunks = await llm.get_chat_completions(params)  # type: ignore[arg-type]
        parts = [
            chunk.choices[0].delta.content
            async for chunk in chunks
            if chunk.choices
            and chunk.choices[0].delta
            and chunk.choices[0].delta.content
        ]
        raw = "".join(parts).strip()

        if not raw:
            return []

        ops: List[Dict[str, Any]] = json.loads(raw)
        if not isinstance(ops, list):
            logger.warning(f"[memory.extract] LLM returned non-list: {raw[:200]}")
            return []
        return ops

    except json.JSONDecodeError as e:
        logger.warning(f"[memory.extract] JSON parse failed: {e}")
        return []
    except Exception as e:
        logger.error(f"[memory.extract] consolidate failed: {e}", exc_info=True)
        return []
