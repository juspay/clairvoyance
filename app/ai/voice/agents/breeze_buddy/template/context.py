"""
Handler Context

Provides context and state access for handler functions.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from pipecat.frames.frames import (
    MixerEnableFrame,
    MixerUpdateSettingsFrame,
    TTSSpeakFrame,
)
from pipecat_flows import NodeConfig

from app.core.logger import logger


class TemplateContext:
    """
    Context object that provides handlers access to bot instance state.

    This allows handlers to be stateless functions while still having
    access to the bot's state and methods.
    """

    def __init__(self, bot_instance):
        """
        Initialize context with bot instance.

        Args:
            bot_instance: The OrderConfirmationBot instance
        """
        self.bot = bot_instance

    @property
    def conversation_ended(self) -> bool:
        """Check if conversation has ended"""
        return self.bot.conversation_ended

    @conversation_ended.setter
    def conversation_ended(self, value: bool):
        """Set conversation ended flag"""
        self.bot.conversation_ended = value

    @property
    def vad_analyzer(self):
        """Get VAD analyzer instance"""
        return self.bot.vad_analyzer

    @property
    def speech_gate(self):
        """Get TranscriptionGateProcessor instance.

        Returns None if the pipeline has not been built yet or if an error
        occurred during pipeline construction. Callers should guard with
        ``if context.speech_gate:`` before use.
        """
        return getattr(self.bot, "speech_gate", None)

    @property
    def aiohttp_session(self):
        """Get AIO Http Session instance"""
        return self.bot.aiohttp_session

    @property
    def completion_function(self):
        """Get Completion Function instance"""
        return self.bot.completion_function

    @property
    def transport(self):
        """Get Transport instance"""
        return self.bot.transport

    @property
    def task(self):
        """Get Pipeline Task instance"""
        return self.bot.task

    @property
    def context(self):
        """Get OpenAI LLM context"""
        return self.bot.context

    @property
    def lead(self):
        """Get lead information"""
        return self.bot.lead

    @lead.setter
    def lead(self, value):
        """Set lead information"""
        self.bot.lead = value

    @property
    def call_sid(self):
        """Get call SID"""
        return self.bot.call_sid

    @property
    def root_span(self):
        """Get OpenTelemetry root span"""
        return self.bot.root_span

    async def queue_tts_filler(self, phrase: str) -> None:
        """Queue a filler phrase for TTS synthesis. Non-blocking."""
        if self.task:
            await self.task.queue_frame(TTSSpeakFrame(text=phrase))

    async def manage_audio_mixer(
        self,
        enable: bool,
        settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Enable/disable the transport audio mixer, optionally updating settings.

        Args:
            enable: True to start mixing, False to stop.
            settings: Optional dict with keys 'sound', 'volume', 'loop' to
                update mixer settings before toggling (applied first).
        """
        if not self.task:
            return
        if settings:
            await self.task.queue_frame(MixerUpdateSettingsFrame(settings=settings))
        await self.task.queue_frame(MixerEnableFrame(enable=enable))

    @property
    def provider(self):
        """Get telephony provider"""
        return self.bot.provider

    @property
    def telephony_service(self):
        """Get telephony service instance (TwilioProvider)"""
        return getattr(self.bot, "telephony_service", None)

    @property
    def end_conversation_callbacks(self):
        """Get end conversation callbacks"""
        return self.bot.end_conversation_callbacks

    @property
    def expected_callback_response_schema(self):
        """Get expected callback response schema"""
        return self.bot.expected_callback_response_schema

    def create_node_from_template(self, node_name: str) -> Optional[NodeConfig]:
        """
        Create a NodeConfig from the template configuration.

        Args:
            node_name: Name of the node to create

        Returns:
            NodeConfig if node found in template, None otherwise
        """
        logger.debug(f"create_node_from_template called for node: {node_name}")

        if not hasattr(self.bot, "flow_config") or not self.bot.flow_config:
            logger.warning(
                f"No flow config available for dynamic node creation (requested node: {node_name})"
            )
            return None

        # Get the node from the already-built flow config
        nodes = self.bot.flow_config.get("nodes", {})
        logger.debug(f"Flow config contains {len(nodes)} nodes: {list(nodes.keys())}")

        # The nodes dict contains NodeConfig objects keyed by node name
        if node_name in nodes:
            logger.info(f"Successfully found node '{node_name}' in flow config")
            return nodes[node_name]

        logger.warning(
            f"Node '{node_name}' not found in flow config. "
            f"Available nodes: {list(nodes.keys())}"
        )
        return None

    def _get_ist_timestamp(self) -> str:
        """Get current timestamp in IST (UTC+5:30) in simple readable format."""
        utc_now = datetime.now(timezone.utc)
        ist_offset = timedelta(hours=5, minutes=30)
        ist_now = utc_now + ist_offset
        # Format: "25 Feb 2025, 12:25:32 PM"
        return ist_now.strftime("%d %b %Y, %I:%M:%S %p")

    def _get_epoch_timestamp(self) -> float:
        """Get current Unix epoch timestamp as a float (sub-second precision)."""
        return time.time()

    def record_node_entry(
        self,
        node_name: str,
        via_function: Optional[str] = None,
        function_args: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Record entry into a node.

        Args:
            node_name: Name of the node being entered
            via_function: Name of the function that brought us to this node (null for initial node)
            function_args: Arguments passed to the function that brought us here
        """
        if not self.lead:
            # Expected in chat mode (no lead by design); voice always sets one.
            logger.info("Skipping node entry record: lead is None")
            return

        if self.lead.metaData is None:
            self.lead.metaData = {}

        if "node_traversal" not in self.lead.metaData:
            self.lead.metaData["node_traversal"] = []

        entry = {
            "node_name": node_name,
            "entered_at": self._get_ist_timestamp(),
            "entered_at_epoch": self._get_epoch_timestamp(),
            "exited_at": None,
            "duration_seconds": None,
            "via_function": via_function,
            "function_args": function_args,
        }

        self.lead.metaData["node_traversal"].append(entry)
        logger.info(f"Recorded entry into node: {node_name} via {via_function}")

    def record_node_exit(self) -> None:
        """
        Record exit from the current node.

        Marks the most recent active node as exited and calculates its duration.
        The via_function and function_args are set during node entry, not exit.
        """
        if not self.lead or not self.lead.metaData:
            # Expected in chat mode (no lead by design); voice always sets one.
            logger.info("Skipping node exit record: lead or metaData is None")
            return

        if "node_traversal" not in self.lead.metaData:
            logger.warning("Cannot record node exit: node_traversal not initialized")
            return

        # Find the last entry without an exit timestamp
        for entry in reversed(self.lead.metaData["node_traversal"]):
            if entry.get("exited_at") is None:
                entry["exited_at"] = self._get_ist_timestamp()

                # Calculate duration using raw epoch floats for sub-second precision
                try:
                    start_epoch = entry.get("entered_at_epoch")
                    if start_epoch is not None:
                        duration = time.time() - start_epoch
                        entry["duration_seconds"] = round(duration, 3)
                    else:
                        logger.warning(
                            "No entered_at_epoch found; cannot calculate duration accurately"
                        )
                        entry["duration_seconds"] = None
                except (TypeError, ValueError) as e:
                    logger.warning(f"Failed to calculate duration: {e}")
                    entry["duration_seconds"] = None

                logger.info(
                    f"Recorded exit from node: {entry['node_name']}, "
                    f"duration: {entry['duration_seconds']}s"
                )
                return

        logger.warning("No active node entry found to record exit")


def with_context(bot_instance):
    """
    Decorator factory that injects TemplateContext into handler functions.

    Supports three types of handlers:
    1. Transition handlers: receive (context, args, transition_to, hooks, function_name)
    2. Action handlers: receive (context, args, transition_to)
    3. Global function handlers: receive (context, args, function_config)

    Usage:
        @with_context(bot)
        async def my_handler(context, flow_manager, args):
            # context is TemplateContext instance
            context.outcome = "confirmed"

    Args:
        bot_instance: The OrderConfirmationBot instance

    Returns:
        Decorator function
    """

    def decorator(handler_func: Callable) -> Callable:
        """
        Decorator that wraps handler with context injection.

        Args:
            handler_func: Handler function to wrap

        Returns:
            Wrapped handler function
        """

        async def wrapper(*args, **kwargs):
            # Create context from bot instance
            context = TemplateContext(bot_instance)

            transition_to = kwargs.pop("transition_to", None)
            hooks = kwargs.pop("hooks", None)
            function_name = kwargs.pop("function_name", None)
            function_config = kwargs.pop("function_config", None)

            is_transition_handler = hooks is not None or function_name is not None
            is_global_function_handler = function_config is not None

            llm_args = args[0] if len(args) > 0 else {}

            logger.debug(
                f"with_context wrapper called - handler: {handler_func.__name__}, "
                f"is_transition_handler: {is_transition_handler}, "
                f"is_global_function_handler: {is_global_function_handler}, "
                f"transition_to: {transition_to}, hooks: {hooks}, function_name: {function_name}"
            )

            if is_global_function_handler:
                # Global function handlers receive (context, args, function_config)
                logger.debug(
                    f"Calling global function handler '{handler_func.__name__}' "
                    f"for function '{function_config.name if hasattr(function_config, 'name') else 'unknown'}'"
                )
                return await handler_func(
                    context,
                    llm_args,
                    function_config=function_config,
                )
            elif is_transition_handler:
                logger.debug(
                    f"Calling transition handler '{handler_func.__name__}' "
                    f"for function '{function_name}'"
                )
                return await handler_func(
                    context,
                    llm_args,
                    transition_to=transition_to,
                    hooks=hooks,
                    function_name=function_name,
                )
            else:
                # Action handlers don't need hooks/function_name
                logger.debug(
                    f"Calling action handler '{handler_func.__name__}' "
                    f"with transition_to: {transition_to}"
                )
                return await handler_func(
                    context, llm_args, transition_to=transition_to
                )

        return wrapper

    return decorator
