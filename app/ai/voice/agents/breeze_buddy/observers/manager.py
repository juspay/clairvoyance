"""ObserverManager — coordinates N real-time observers.

Reads the conversation transcript from the pipeline's existing LLMContext,
builds a formatted transcript string, and feeds it to all eligible observers
in parallel when configured events fire.

Not a pipeline processor. Completely separate async system.
"""

import asyncio
from typing import Any, List

from pipecat.processors.aggregators.llm_context import LLMContext

from app.ai.voice.agents.breeze_buddy.template.types import ActionType
from app.core.logger import logger

from .observer import RealtimeObserver


class ObserverManager:
    """Coordinates N observers. Reads existing LLMContext.

    Triggered by pipeline events (configurable per observer via ``trigger_on``).
    All eligible observers run concurrently and independently — each
    observer acts on its own detection without blocking or being
    blocked by others.
    """

    def __init__(
        self,
        observers: List[RealtimeObserver],
        llm_context: LLMContext,
    ):
        self._observers = observers
        self._llm_context = llm_context
        self._turn_count: int = 0
        self._acted: set[str] = set()  # non-ALERT observers that already fired
        self._fired_alerts: set[str] = set()  # ALERT observers that already fired
        self._action_taken: bool = False  # True once any non-ALERT action executes
        self._check_lock = asyncio.Lock()
        # Strong references to in-flight tasks — prevents GC from collecting
        # them mid-flight when PipelineRunner runs with force_gc=True.
        self._pending: set[asyncio.Task] = set()
        self._stopped: bool = False

    # ------------------------------------------------------------------
    # Data ingestion (called by pipeline event hooks in agent/__init__.py)
    # ------------------------------------------------------------------

    def _track_task(self, task: asyncio.Task) -> None:
        """Hold a strong ref so GC cannot collect in-flight tasks."""
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def on_event(self, event_name: str):
        """A pipeline event fired — kick off eligible observer checks."""
        if self._stopped:
            return
        # Only count on one event to avoid double-counting per turn
        if event_name == "on_user_turn_message_added":
            self._turn_count += 1
        self._track_task(
            asyncio.create_task(
                self._run_checks(event_name), name=f"observer:{event_name}"
            )
        )

    # ------------------------------------------------------------------
    # Check execution
    # ------------------------------------------------------------------

    async def _run_checks(self, event_name: str):
        """Run all eligible observers concurrently and independently.

        Each observer that detects fires its action as a background task
        without waiting for or blocking other observers.
        """
        if self._stopped:
            return

        # Lock only for eligibility + transcript snapshot — released
        # before any LLM call so the lock is never held while waiting
        # for slow observers.
        async with self._check_lock:
            if self._stopped:
                return

            def _is_alert(obs: RealtimeObserver) -> bool:
                return (
                    obs.config.action is not None
                    and obs.config.action.type == ActionType.ALERT
                )

            eligible = [
                obs
                for obs in self._observers
                if event_name in obs.config.trigger_on
                and self._turn_count >= obs.config.start_after_turn
                and (
                    _is_alert(obs)
                    and obs.name not in self._fired_alerts
                    or not _is_alert(obs)
                    and obs.name not in self._acted
                )
            ]
            if not eligible:
                return

            transcript = self._build_transcript()

            logger.info(
                f"Observer check ({event_name}): turn {self._turn_count}, "
                f"running {len(eligible)} observer(s): "
                f"{[obs.name for obs in eligible]}"
            )

            # gather() over as_completed(): as_completed wraps futures in
            # new coroutines so the original future→observer mapping breaks.
            # gather() returns results in input order which is deterministic
            # and keeps the observer→result pairing trivial via zip().
            results = await asyncio.gather(
                *[obs.check(transcript) for obs in eligible],
                return_exceptions=True,
            )

            for obs, result in zip(eligible, results):
                if self._action_taken:
                    return
                if isinstance(result, Exception):
                    logger.error(f"Observer {obs.name} check failed: {result}")
                    continue
                if result is True:
                    is_alert = _is_alert(obs)
                    if is_alert:
                        self._fired_alerts.add(obs.name)
                    else:
                        self._acted.add(obs.name)
                        self._action_taken = True
                    try:
                        await obs.execute_action()
                    except Exception as e:
                        logger.error(f"Observer {obs.name} execute_action failed: {e}")
                    if self._action_taken:
                        return

    # ------------------------------------------------------------------
    # Transcript building
    # ------------------------------------------------------------------

    def _build_transcript(self) -> str:
        """Build transcript from LLMContext messages.

        Reads user/assistant messages and tool_calls directly from
        the LLMContext — single source of truth, no separate tracking.
        """
        lines: List[str] = []

        for msg in self._llm_context.messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user" and content:
                lines.append(f"[customer] {content}")
            elif role == "assistant":
                if content:
                    lines.append(f"[bot] {content}")
                for tool_call in msg.get("tool_calls", []):
                    function = tool_call.get("function", {})
                    lines.append(
                        f"[bot_action] {function.get('name', '')}({function.get('arguments', '')})"
                    )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def subscribed_events(self) -> set[str]:
        """Return all events any observer is subscribed to."""
        events: set[str] = set()
        for obs in self._observers:
            events.update(obs.config.trigger_on)
        return events

    def subscribe_to_pipeline(self, context_aggregator: Any, flow_manager: Any) -> None:
        """Wire pipeline events to on_event(). Only subscribes to events
        that observers actually need. Called from agent.__init__.py."""
        subscribed = self.subscribed_events()
        user_agg = context_aggregator.user()
        assistant_agg = context_aggregator.assistant()

        event_sources = {
            "on_user_turn_started": user_agg,
            "on_user_turn_stopped": user_agg,
            "on_user_turn_message_added": user_agg,
            "on_user_turn_idle": user_agg,
            "on_assistant_turn_started": assistant_agg,
            "on_assistant_turn_stopped": assistant_agg,
        }

        supported = set(event_sources) | {"on_function_calls_started"}
        invalid = subscribed - supported
        if invalid:
            logger.warning(f"Unknown observer trigger events (ignored): {invalid}")

        for event_name, source in event_sources.items():
            if event_name in subscribed:

                def _make_handler(evt_name):
                    async def _handler(*args, **kwargs):
                        self.on_event(evt_name)

                    return _handler

                source.event_handler(event_name)(_make_handler(event_name))

        if "on_function_calls_started" in subscribed and flow_manager:
            llm_service = getattr(flow_manager, "_llm", None)
            if llm_service:

                def _make_fn_handler():
                    async def _handler(*args, **kwargs):
                        self.on_event("on_function_calls_started")

                    return _handler

                llm_service.event_handler("on_function_calls_started")(
                    _make_fn_handler()
                )

    async def stop(self):
        """Cleanup. Called when the call ends.

        Waits up to 15 s for in-flight observer checks to finish before
        cancelling them — prevents CancelledError cutting off an LLM call
        that is mid-flight when the main pipeline shuts down.
        """
        self._stopped = True
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=15)
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
