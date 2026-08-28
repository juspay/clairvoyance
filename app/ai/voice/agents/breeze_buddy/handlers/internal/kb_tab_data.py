"""
Get Tab Data / List Tabs Handlers (KB tool mode, tab-scoped)

Built-in global functions the LLM calls explicitly to pull the raw text of
one ingested spreadsheet tab, or discover which tab names exist, from the
template's attached knowledge base(s). Sibling to query_knowledge_base.py:
same TemplateContext contract, same fail-open/timeout discipline, but reads
a tab deterministically by name instead of running semantic search.

Available in BOTH voice and chat (must never be added to CHAT_DISABLED_NAMES).
"""

import asyncio
from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.services.knowledge_base import (
    DEFAULT_KB_INSTRUCTION,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import KnowledgeBaseConfig
from app.core.logger import logger
from app.services.knowledge_base import get_kb_tab_text, list_kb_tabs

_TAB_FETCH_TIMEOUT_SECS = 5.0


def _attached_kb_config(context: TemplateContext) -> Optional[KnowledgeBaseConfig]:
    """The template's KB config, or None when KB is absent/disabled."""
    config = getattr(context.configurations, "knowledge_base", None)
    if config is None or not config.enabled or not config.knowledge_base_ids:
        return None
    return config


def _no_kb_error() -> Dict[str, Any]:
    return {
        "status": "error",
        "message": "No knowledge base is attached to this template.",
    }


async def get_tab_data(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """Fetch one tab's full text by name from the attached knowledge base(s)."""
    config = _attached_kb_config(context)
    if config is None:
        return _no_kb_error()
    kb_ids = config.knowledge_base_ids

    tab_name = str(args.get("tab_name") or "").strip()
    if not tab_name:
        return {
            "status": "error",
            "message": "A non-empty 'tab_name' argument is required.",
        }

    try:
        text, truncated = await asyncio.wait_for(
            get_kb_tab_text(kb_ids, tab_name), timeout=_TAB_FETCH_TIMEOUT_SECS
        )
    except asyncio.TimeoutError:
        logger.warning(f"[get_tab_data] timed out for call {context.call_sid}")
        return {
            "status": "error",
            "message": "Tab lookup timed out; answer without it or tell the "
            "user you could not check.",
        }
    except Exception as e:
        logger.error(f"[get_tab_data] failed: {e}", exc_info=True)
        return {
            "status": "error",
            "message": "Tab lookup failed; answer without it or tell the "
            "user you could not check.",
        }

    if not text:
        return {
            "status": "not_found",
            "message": f"No tab named '{tab_name}' was found. Call "
            "list_kb_tabs to see available tab names.",
        }

    logger.info(
        f"[get_tab_data] loaded tab '{tab_name}' for call {context.call_sid} "
        f"({len(text)} chars, truncated={truncated})"
    )
    result: Dict[str, Any] = {
        "status": "success",
        "tab_name": tab_name,
        "content": text,
        "instruction": config.injection_instruction or DEFAULT_KB_INSTRUCTION,
    }
    if truncated:
        result["truncated"] = True
        result["message"] = (
            "This tab is large and was truncated to its first portion. Answer "
            "from what's shown, and if it may be incomplete, ask the user to "
            "narrow the request (e.g. a specific item, row, or category)."
        )
    return result


async def list_tabs(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """List the tab names available across the attached knowledge base(s)."""
    config = _attached_kb_config(context)
    if config is None:
        return _no_kb_error()
    kb_ids = config.knowledge_base_ids

    try:
        names = await asyncio.wait_for(
            list_kb_tabs(kb_ids), timeout=_TAB_FETCH_TIMEOUT_SECS
        )
    except asyncio.TimeoutError:
        logger.warning(f"[list_tabs] timed out for call {context.call_sid}")
        return {"status": "error", "message": "Tab lookup timed out."}
    except Exception as e:
        logger.error(f"[list_tabs] failed: {e}", exc_info=True)
        return {"status": "error", "message": "Tab lookup failed."}

    return {"status": "success", "tabs": names}
