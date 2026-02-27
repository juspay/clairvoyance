"""
Handler Context

Provides context and state access for handler functions.
"""

from typing import Callable, Optional

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
