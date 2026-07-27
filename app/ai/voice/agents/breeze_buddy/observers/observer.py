"""RealtimeObserver — one side-LLM that watches the conversation.

Each observer receives the conversation transcript and sends it to a small LLM
with template-configured tools via function calling.
The LLM calls a tool — structured output, no text parsing.

Tools are template-configurable: custom tools can be defined per observer.
Provider dispatch follows the same ``isinstance`` pattern as
``chat/llm_driver.py`` — covers OpenAI/Azure, Anthropic, and Google.

Tool call = detection. No tool call = clean turn.

When detection triggers, ``execute_action()`` sets the outcome via the
existing ``update_outcome_in_database`` hook and runs the configured
``FlowAction`` through the handler_map.
"""

import time
from typing import Any, Dict

from pipecat.adapters.schemas.function_schema import FunctionSchema

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import ActionType, ObserverConfig
from app.core.logger import logger
from app.services.slack.alert import slack_alert

from .llm import call_llm
from .utils import set_outcome


def _build_tools(tool_defs: list[dict]) -> list[FunctionSchema]:
    """Build FunctionSchema list from template config."""
    return [
        FunctionSchema(
            name=tool["name"],
            description=tool.get("description", ""),
            properties=tool.get("properties", {}),
            required=tool.get("required", []),
        )
        for tool in tool_defs
    ]


class RealtimeObserver:
    """One side-LLM that watches the conversation.

    Lifecycle:
        1. Created by factory at call start.
        2. ``check()`` called by ObserverManager after each eligible turn.
        3. If detected, ``execute_action()`` called by manager.
        4. Discarded at call end.
    """

    def __init__(
        self,
        config: ObserverConfig,
        llm_service: Any,
        agent_context: Any,
        handler_map: Dict[str, Any],
    ) -> None:
        self.config = config
        self.name = config.name
        self._llm_service = llm_service
        self._agent_context = agent_context
        self._handler_map = handler_map
        self._tools = _build_tools(config.tools)
        self._last_detection: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    async def check(self, transcript_text: str) -> bool:
        """Send transcript to observer LLM with configured tools.

        Uses function calling — same pattern as chat/llm_driver.py.
        Provider dispatch via isinstance (OpenAI/Azure, Anthropic, Google).
        LLM calls a tool = detection. No tool call = clean.
        """
        if not transcript_text.strip():
            return False

        try:
            start = time.monotonic()
            tool_name, tool_args = await call_llm(
                llm_service=self._llm_service,
                transcript_text=transcript_text,
                system_prompt=self.config.system_prompt,
                tools=self._tools,
                observer_name=self.name,
            )
            latency_ms = (time.monotonic() - start) * 1000

            if tool_name is None:
                logger.info(
                    f"Observer {self.name} no detection, " f"latency={latency_ms:.0f}ms"
                )
                return False

            self._last_detection = tool_args or {}
            # Capture the active node now — by the time execute_action
            # runs, the main LLM may have already transitioned.
            flow_mgr = getattr(self._agent_context, "flow_manager", None)
            self._detected_at_node = flow_mgr.current_node if flow_mgr else None
            logger.info(
                f"Observer {self.name} detected: "
                f"{tool_name}({tool_args}), latency={latency_ms:.0f}ms"
            )
            return True

        except Exception:
            logger.exception(f"Observer {self.name} check failed")

        return False

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def execute_action(self) -> None:
        """Execute the configured FlowAction via the existing handler_map.

        Sets outcome via update_outcome handler (same hook path as template
        functions), then calls the configured action handler.
        """
        lead = self._agent_context.lead
        action = self.config.action
        handler_name = action.handler or action.type

        if lead:
            if lead.metaData is None:
                lead.metaData = {}
            lead.metaData["observer_triggered"] = self.name

        # Record observer action — only the winning observer reaches
        # execute_action (manager sets _action_taken after first detect).
        # Use the node captured at detection time (check()) since the
        # active node may have changed due to race with main LLM.
        ctx = TemplateContext(self._agent_context)
        ctx.record_observer_action(
            self.name,
            handler_name,
            action.args,
            node_name=getattr(self, "_detected_at_node", None),
        )

        outcome = (action.args or {}).get("outcome")
        if lead and outcome:
            lead.outcome = outcome
            ctx = TemplateContext(self._agent_context)
            await set_outcome(ctx, outcome, triggered_by=self.name)

        if action.type == ActionType.ALERT:
            logger.info(f"Observer {self.name} executing action: alert")
            call_sid = ctx.call_sid
            safe_detection = (
                list(self._last_detection.keys())
                if isinstance(self._last_detection, dict)
                else []
            )
            try:
                await slack_alert.send(
                    title=f"🟠 Breeze Buddy - Observer: {self.name}",
                    fields=[
                        {"name": "Observer", "value": self.name},
                        {"name": "Call SID", "value": f"`{call_sid or 'N/A'}`"},
                        {"name": "Detection fields", "value": f"`{safe_detection}`"},
                    ],
                    fallback_text=f"Observer '{self.name}' flagged call {call_sid}",
                )
                logger.info(
                    f"Observer '{self.name}' sent Slack alert "
                    f"(call={call_sid}, fields={safe_detection})"
                )
            except Exception:
                logger.exception(f"Observer '{self.name}' failed to send Slack alert")
        else:
            handler = self._handler_map.get(handler_name)
            if handler:
                logger.info(
                    f"Observer {self.name} executing action: "
                    f"{handler_name}, outcome={outcome}"
                )
                handler_args = {
                    **(action.args or {}),
                    "detection": self._last_detection,
                }
                await handler(handler_args)
            else:
                logger.error(
                    f"Observer {self.name}: handler {handler_name} "
                    f"not found in handler_map"
                )
