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
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pipecat.adapters.schemas.function_schema import FunctionSchema

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import ObserverConfig
from app.core.logger import logger

from .llm import call_llm
from .utils import is_alert_action, record_detection, set_outcome

# What an observer LLM may put in the stored detection. Its tool arguments are
# model-authored and can quote the transcript, so anything not named here is
# dropped rather than written to evaluation_result.
_DETECTION_ALLOWED_KEYS = ("reason", "confidence")
_DETECTION_VALUE_MAX_CHARS = 300


def _bounded_detection(raw: Any) -> Dict[str, Any]:
    """Keep only known scalar detection fields, each length-capped."""
    if not isinstance(raw, dict):
        return {}
    bounded: Dict[str, Any] = {}
    for key in _DETECTION_ALLOWED_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or isinstance(value, (int, float)):
            bounded[key] = value
        elif isinstance(value, str) and value.strip():
            bounded[key] = value[:_DETECTION_VALUE_MAX_CHARS]
    # Only keys we refused — an allowed key that came in blank is simply empty,
    # not something withheld.
    dropped = sorted(set(raw) - set(_DETECTION_ALLOWED_KEYS))
    if dropped:
        bounded["dropped_keys"] = dropped
    return bounded


def _build_tool_from_action(config: "ObserverConfig") -> list[FunctionSchema]:
    """Build FunctionSchema from action config.

    Tool name is derived from the configured handler when present, otherwise
    the action type string (e.g. legacy alert configs without a handler).
    """
    action = config.action
    tool_name = action.handler or str(action.type.value)
    return [
        FunctionSchema(
            name=tool_name,
            description=action.description
            or "Call this when the observer condition is detected.",
            properties={},
            required=[],
        )
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
        evaluation_config_id: Optional[str] = None,
    ) -> None:
        self.config = config
        self.name = config.name
        self._llm_service = llm_service
        self._agent_context = agent_context
        self._handler_map = handler_map
        self._evaluation_config_id = evaluation_config_id
        self._tools = _build_tool_from_action(config)
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

            self._last_detection = _bounded_detection(tool_args)
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
        handler_name = action.handler or (
            "send_alert" if is_alert_action(action) else str(action.type.value)
        )

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

        handler = self._handler_map.get(handler_name)
        if not handler:
            logger.error(
                f"Observer {self.name}: handler {handler_name} "
                f"not found in handler_map"
            )
            return

        if is_alert_action(action):
            call_sid = ctx.call_sid
            safe_detection = (
                list(self._last_detection.keys())
                if isinstance(self._last_detection, dict)
                else []
            )
            handler_args = {
                **(action.args or {}),
                "title": (action.args or {}).get("title")
                or f"Breeze Buddy - Observer: {self.name}",
                "fields": (action.args or {}).get("fields")
                or [
                    {"name": "Observer", "value": self.name},
                    {"name": "Call SID", "value": f"`{call_sid or 'N/A'}`"},
                    {"name": "Detection fields", "value": f"`{safe_detection}`"},
                ],
                "fallback_text": (action.args or {}).get("fallback_text")
                or f"Observer '{self.name}' flagged call {call_sid}",
                "source": "observer",
                "observer_name": self.name,
                "call_sid": call_sid,
                "detection": self._last_detection,
            }
        else:
            handler_args = {
                **(action.args or {}),
                "detection": self._last_detection,
            }

        logger.info(
            f"Observer {self.name} executing action: "
            f"{handler_name}, outcome={outcome}"
        )
        # Recorded before the handler runs: end_conversation tears the pipeline
        # down, so anything after it never gets the chance to write. ``type``
        # becomes the ``result`` column on evaluation_result.
        await record_detection(
            agent_context=self._agent_context,
            evaluation_config_id=self._evaluation_config_id,
            observer_name=self.name,
            detection={
                "type": self.name,
                "label": self.name.replace("_", " ").title(),
                "observer_name": self.name,
                "triggered_at": datetime.now(timezone.utc).isoformat(),
                "node": getattr(self, "_detected_at_node", None),
                "action_type": action.type.value,
                "handler": handler_name,
                "args": action.args or {},
                "outcome": outcome,
                "detection": self._last_detection,
            },
        )
        await handler(handler_args)
