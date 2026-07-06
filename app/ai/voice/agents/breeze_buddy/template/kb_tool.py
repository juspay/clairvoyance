"""
Tool-mode knowledge base helpers for the flow config builder.

When a template's ``configurations.knowledge_base`` selects ``mode='tool'``,
the ``query_knowledge_base`` builtin is synthesized from that config instead
of being declared in the flow — one config section drives all retrieval
modes. ``FlowConfigBuilder.build_global_functions`` is the only caller;
voice and chat both flow through it, so the tool appears uniformly on both
channels. Kept out of ``builder.py`` so all KB tool-mode wiring lives in one
place next to the template models it reads.
"""

from typing import Any, Callable, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.template.types import KnowledgeBaseMode
from app.core.logger import logger


def append_kb_tool(
    declared: List[Dict[str, Any]],
    kb_tool_function: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Append the synthesized KB tool unless a same-named function is
    already declared — explicit declarations win, and the LLM must never
    receive duplicate tool names (several providers reject the request).

    Must run BEFORE ``filter_disabled_identifiers`` so per-channel disabling
    applies to the synthesized tool like any other function.
    """
    if kb_tool_function is None:
        return list(declared)
    declared_names = {
        entry.get("name") for entry in declared if isinstance(entry, dict)
    }
    if kb_tool_function.get("name") in declared_names:
        logger.warning(
            f"KB tool mode: '{kb_tool_function.get('name')}' is already "
            "declared in the flow; skipping synthesis"
        )
        return list(declared)
    return list(declared) + [kb_tool_function]


def synthesize_kb_tool_function(
    bot_instance: Any,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Build the query_knowledge_base builtin dict when the template's
    knowledge_base config selects tool mode. Returns None otherwise.

    TOOL is a statically declared mode (AUTO never resolves to it), so
    this stays synchronous and side-effect free. Authors who need filler
    audio or custom placement can instead declare the builtin explicitly
    in the flow — this synthesis only covers the config-driven path.

    Args:
        bot_instance: Voice agent or chat agent; configurations are read
            from the instance or its template (same fallback as
            ``TemplateContext.configurations``).
        log: Builder's pre-bound quiet/verbose logger; defaults to
            ``logger.info``.
    """
    configurations = getattr(bot_instance, "configurations", None)
    if configurations is None:
        template = getattr(bot_instance, "template", None)
        configurations = getattr(template, "configurations", None)
    kb_config = getattr(configurations, "knowledge_base", None)
    if (
        kb_config is None
        or not kb_config.enabled
        or kb_config.mode != KnowledgeBaseMode.TOOL
        or not kb_config.knowledge_base_ids
    ):
        return None

    # Vertical-neutral default; templates set tool_description for
    # domain-specific wording.
    description = kb_config.tool_description or (
        "Search the knowledge base attached to this conversation for "
        "information relevant to the user's question. Call this whenever "
        "the user asks something the knowledge base may answer, before "
        "answering from memory."
    )
    (log or logger.info)(f"KB tool mode: synthesized builtin '{kb_config.tool_name}'")
    return {
        "type": "builtin",
        "handler": "query_knowledge_base",
        "name": kb_config.tool_name,
        "description": description,
        "properties": {
            "query": {
                "type": "string",
                "description": "What the user wants to know, as a "
                "self-contained search query.",
            }
        },
        "required": ["query"],
        "cancel_on_interruption": True,
    }
