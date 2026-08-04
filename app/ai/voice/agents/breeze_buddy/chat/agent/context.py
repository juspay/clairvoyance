"""Context seeding: node resolution, template-var message rendering,
LLM context assembly, KB prefix/tail placement, node transitions.

Method bodies are verbatim from the monolithic agent.py (split 2026-08-05);
this mixin holds no state of its own — every attribute lives on
``ChatAgent`` (see ``core``)."""

from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

from pipecat.processors.aggregators.llm_context import (
    LLMContext,
    LLMContextMessage,
)
from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import (  # noqa: F401
    _CHIPS_NUDGE,
    _MAX_TOOL_CYCLES,
    _chip_labels,
    _KbMessage,
    _partition_gated_calls,
    _PreparedTools,
    _summarize_result,
    _tools_schema,
)
from app.ai.voice.agents.breeze_buddy.chat.client_context import (
    render_client_context,
)
from app.ai.voice.agents.breeze_buddy.services.knowledge_base import (
    build_kb_system_message,
    build_retrieval_query,
    fetch_full_kb_text_cached,
    fetch_kb_context_message,
    resolve_kb_runtime,
)
from app.ai.voice.agents.breeze_buddy.template.utils import render_messages_with_vars
from app.ai.voice.agents.breeze_buddy.utils.language_utils.prompt_injections import (
    inject_language_rules,
)
from app.core.logger import logger

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.chat.agent.core import ChatAgent


class ContextSeedMixin:
    async def _prepare_kb_message(
        self: "ChatAgent",
        user_content: str,
        history: List[Dict[str, Any]],
    ) -> Optional[_KbMessage]:
        """Resolve the template's KB mode and fetch this turn's KB message.

        Returns None when no KB is attached or on any failure (fail-open).
        Tool mode needs nothing here — the builder synthesizes the
        query_knowledge_base function instead.
        """
        try:
            runtime = await resolve_kb_runtime(
                self.template.configurations if self.template else None
            )
            if runtime is None or runtime.mode == "tool":
                return None
            if runtime.mode == "full_injection":
                text = await fetch_full_kb_text_cached(runtime.config)
                if not text:
                    return None
                return _KbMessage(
                    message=build_kb_system_message(text, runtime.config),
                    placement="prefix",
                )
            # auto_retrieve
            recent_user_turns = [
                m["content"]
                for m in history
                if isinstance(m, dict)
                and m.get("role") == "user"
                and isinstance(m.get("content"), str)
            ]
            query = build_retrieval_query(
                user_content, recent_user_turns, runtime.config
            )
            message = await fetch_kb_context_message(runtime.config, query, timeout=1.0)
            if message is None:
                return None
            return _KbMessage(message=message, placement="tail")
        except Exception as e:
            logger.warning(
                f"ChatAgent {self.session_id}: KB prep failed (continuing): {e}"
            )
            return None

    def _resolve_node(
        self: "ChatAgent", flow_config: Dict[str, Any], current_node: Optional[str]
    ) -> Dict[str, Any]:
        target = current_node or flow_config["initial_node"]
        if target not in flow_config["nodes"]:
            logger.warning(
                f"ChatAgent {self.session_id}: current_node '{target}' missing "
                f"from template; falling back to '{flow_config['initial_node']}'"
            )
            target = flow_config["initial_node"]
        return cast(Dict[str, Any], flow_config["nodes"][target])

    def _render_node_messages(
        self: "ChatAgent", node: Dict[str, Any]
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Render role + task messages with template_vars and inject language
        rules — chat parity with FlowConfigLoader.load_template."""
        role_messages = render_messages_with_vars(
            list(node.get("role_messages", [])), self.template_vars
        )
        stt_config = (
            getattr(self.template.configurations, "stt_configuration", None)
            if self.template.configurations
            else None
        )
        payload_selection = False
        if stt_config is not None:
            payload_selection = stt_config.payload_based_language_selection
        else:
            payload_selection = getattr(
                getattr(self.template, "configurations", None),
                "payload_based_language_selection",
                False,
            )

        role_messages = inject_language_rules(
            role_messages,
            self.template_vars.get("language_name", "English"),
            payload_selection,
        )
        task_messages = render_messages_with_vars(
            list(node.get("task_messages", [])), self.template_vars
        )
        return role_messages, task_messages

    def _seed_context(
        self: "ChatAgent",
        node: Dict[str, Any],
        history: List[Dict[str, Any]],
        user_content: str,
        global_funcs: List[FlowsFunctionSchema],
        kb_message: Optional["_KbMessage"] = None,
    ) -> LLMContext:
        """Build initial LLMContext: [role, task, kb_full?, …history…,
        system_block?, kb_chunks?, new user].

        Client-pushed facts (offers, cart summary, …) are rendered here:
        ``user_block`` rides the user turn as untrusted data (cache-safe,
        injection-safe); ``system_block`` (trusted, opt-in) is appended as
        a system message right before the user turn. Both are EPHEMERAL —
        never persisted to chat_message, re-derived from agent_state every
        turn so they always reflect current truth (latest-wins).

        KB placement follows the same logic: full-injection text sits right
        after task messages (stable across turns → prompt-cache friendly);
        per-turn retrieved chunks sit just before the user turn (fresh each
        turn, invalidates only the suffix). Both are ephemeral like the
        client-context blocks.
        """
        role_messages, task_messages = self._render_node_messages(node)
        user_block, system_block = render_client_context(
            self.agent_state,
            self._client_context_config,
            self._context_placement,
        )
        user_text = f"{user_block}\n\n{user_content}" if user_block else user_content
        messages: List[Dict[str, Any]] = [
            *role_messages,
            *task_messages,
        ]
        if kb_message is not None and kb_message.placement == "prefix":
            messages.append(kb_message.message)
        messages.extend(history)
        if system_block:
            messages.append({"role": "system", "content": system_block})
        if kb_message is not None and kb_message.placement == "tail":
            messages.append(kb_message.message)
        messages.append({"role": "user", "content": user_text})
        return LLMContext(
            messages=cast(List[LLMContextMessage], messages),
            tools=_tools_schema(node, global_funcs),
        )

    def _apply_node_transition(
        self: "ChatAgent",
        context: LLMContext,
        next_node: Dict[str, Any],
        global_funcs: List[FlowsFunctionSchema],
    ) -> None:
        """Mid-turn transition — append new task_messages + swap tools.

        Mirrors FlowManager._set_node: role_messages stay from the original
        node (only sent on the very first node); task_messages append so the
        previous node's instructions remain in scope for the model.
        """
        _, task_messages = self._render_node_messages(next_node)
        for msg in task_messages:
            context.add_message(cast(LLMContextMessage, msg))
        context.set_tools(_tools_schema(next_node, global_funcs))

    # ------------------------------------------------------------------
    # Direct (no-LLM) dispatch surface — used by chat/intent_router.py.
    #
    # The intent router executes whitelisted cart tools through the SAME
    # machinery an LLM turn uses (builder-wrapped handlers running
    # apply_result_pipeline, inject_tool_args, state reducers, binding
    # store) without ever constructing an LLM client. These are thin
    # public seams over the private per-turn internals so the router
    # doesn't reach into them.
    # ------------------------------------------------------------------
