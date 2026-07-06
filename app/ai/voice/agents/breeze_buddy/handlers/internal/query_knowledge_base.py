"""
Query Knowledge Base Handler (tool mode)

Built-in global function the LLM calls explicitly to search the template's
attached knowledge bases. Available in BOTH voice and chat (must never be
added to CHAT_DISABLED_NAMES). Voice templates can pair it with filler_audio
to mask retrieval latency; the handler itself has a generous 5s budget since
tool mode already costs an extra LLM round trip.
"""

import asyncio
from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.services.knowledge_base import (
    DEFAULT_KB_INSTRUCTION,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger
from app.services.knowledge_base import retrieve

_TOOL_TIMEOUT_SECS = 5.0


async def query_knowledge_base(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Search the template's knowledge bases and return matching content.

    Args:
        context: Handler context with bot state access
        args: LLM function arguments; expects a "query" string

    Returns:
        Dict with retrieved chunks, or an error status (never raises past
        the dispatcher).
    """
    configurations = context.configurations
    config = getattr(configurations, "knowledge_base", None)
    if config is None or not config.enabled or not config.knowledge_base_ids:
        return {
            "status": "error",
            "message": "No knowledge base is attached to this template.",
        }

    query = str(args.get("query") or "").strip()
    if not query:
        return {
            "status": "error",
            "message": "A non-empty 'query' argument is required.",
        }

    try:
        chunks = await asyncio.wait_for(
            retrieve(
                kb_ids=config.knowledge_base_ids,
                query=query,
                top_k=config.top_k,
                score_threshold=config.score_threshold,
            ),
            timeout=_TOOL_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[query_knowledge_base] timed out for call {context.call_sid}")
        return {
            "status": "error",
            "message": "Knowledge base lookup timed out; answer without it "
            "or tell the user you could not check.",
        }
    except Exception as e:
        logger.error(f"[query_knowledge_base] failed: {e}", exc_info=True)
        return {
            "status": "error",
            "message": "Knowledge base lookup failed; answer without it "
            "or tell the user you could not check.",
        }

    if not chunks:
        return {
            "status": "success",
            "results": [],
            "message": "No matching knowledge found for this query.",
            "instruction": DEFAULT_KB_INSTRUCTION,
        }

    logger.info(
        f"[query_knowledge_base] {len(chunks)} chunk(s) for call "
        f"{context.call_sid}: {query[:80]!r}"
    )
    return {
        "status": "success",
        "results": [
            {
                "content": chunk.text,
                "source": chunk.document_name,
                "score": round(chunk.score, 4),
            }
            for chunk in chunks
        ],
        "instruction": config.injection_instruction or DEFAULT_KB_INSTRUCTION,
    }
