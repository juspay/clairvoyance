"""Voice agent for handling conversations via Daily or telephony transports."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import WebSocket
from opentelemetry import trace
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import (
    _create_telephony_transport,
    create_transport,
    parse_telephony_websocket,
)
from pipecat_flows import FlowManager

from app.ai.voice.agents.breeze_buddy.agent.flow import (
    build_flow_config,
    load_template_config,
    prepare_initial_node,
    setup_flow_manager,
)
from app.ai.voice.agents.breeze_buddy.agent.inbound import (
    create_lead_from_template_id,
    handle_inbound_call,
)
from app.ai.voice.agents.breeze_buddy.agent.ivr import get_template_id_from_call
from app.ai.voice.agents.breeze_buddy.agent.pipeline import (
    build_pipeline,
    create_pipeline_task,
    create_services,
    generate_conversation_id,
)
from app.ai.voice.agents.breeze_buddy.agent.transport import (
    TRANSPORT_TYPE_DAILY,
    get_transport_params,
)
from app.ai.voice.agents.breeze_buddy.agent.utils import (
    end_call_with_errors,
    send_initial_greeting,
)
from app.ai.voice.agents.breeze_buddy.agent.vad import create_vad_analyzer
from app.ai.voice.agents.breeze_buddy.agent.websocket import (
    close_websocket_safely,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import (
    create_root_span,
)
from app.ai.voice.agents.breeze_buddy.template import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.context import with_context
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.utils.common import track_error
from app.core.config.static import ENABLE_BREEZE_BUDDY_TRACING
from app.core.logger import logger
from app.database.accessor import update_lead_call_initiated_time
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_initiated_time_by_id,
)
from app.schemas import CallProvider
from app.schemas.breeze_buddy.core import LeadCallTracker

DEFAULT_OUTCOME = "BUSY"


class Agent:
    """Voice agent that handles conversations via Daily or telephony transports."""

    def __init__(
        self,
        transport_type: str,
        ws: Optional[WebSocket] = None,
        aiohttp_session: Any = None,
        hangup_function: Optional[Callable] = None,
        completion_function: Optional[Callable] = None,
        provider: Optional[str] = None,
    ):
        # Transport configuration
        self.transport_type = transport_type
        self.ws = ws
        self.aiohttp_session = aiohttp_session
        self.provider = provider
        self.hangup_function = hangup_function
        self.completion_function = completion_function

        # Runtime state
        self.task: Optional[PipelineTask] = None
        self.context: Optional[OpenAILLMContext] = None
        self.conversation_ended = False
        self.call_sid: Optional[str] = None
        self.stream_sid: Optional[str] = None
        self.vad_analyzer: Optional[SileroVADAnalyzer] = None
        self.transport: Any = None
        self.lead: Optional[LeadCallTracker] = None
        self.root_span: Any = None
        self.flow_manager: Optional[FlowManager] = None
        self.conversation_id: Optional[str] = None

        # Template configuration
        self.flow_builder: Any = None
        self.template: Optional[TemplateModel] = None
        self.configurations: Optional[ConfigurationModel] = None
        self.flow_config: Optional[Dict[str, Any]] = None
        self.end_conversation_callbacks: List = []
        self.expected_callback_response_schema: Any = None
        self.greeting_source: Optional[str] = None
        self.default_vad_params: Optional[VADParams] = None

        # Error tracking
        self.errors: List[Dict[str, Any]] = []

    @property
    def is_daily_mode(self) -> bool:
        return self.transport_type == TRANSPORT_TYPE_DAILY

    async def _setup_daily_transport(self, runner_args: RunnerArguments) -> None:
        """Initialize transport for Daily mode."""
        if not runner_args or not runner_args.body:
            raise ValueError("runner_args with body is required for Daily mode")

        lead_id = runner_args.body.get("lead_id")
        if not lead_id:
            raise ValueError("lead_id is required in runner_args.body for Daily mode")

        call_initiated_time = datetime.now(timezone.utc)
        self.lead = await update_lead_call_initiated_time_by_id(
            lead_id, call_initiated_time
        )
        if not self.lead:
            raise ValueError(f"Lead not found for lead_id: {lead_id}")

        logger.info(f"Starting Daily bot for lead_id: {lead_id}")

        self.flow_builder = FlowConfigBuilder()

        for handler_name, handler_func in self.flow_builder.handler_map.items():
            self.flow_builder.handler_map[handler_name] = with_context(self)(
                handler_func
            )

        self.template, self.configurations, self.template_vars = (
            await load_template_config(self.lead)
        )

        self.vad_analyzer, self.default_vad_params = await create_vad_analyzer(
            is_daily_mode=True
        )

        transport_params = get_transport_params(self.vad_analyzer)
        self.transport = await create_transport(runner_args, transport_params)

    async def _setup_telephony_transport(self) -> bool:
        """Initialize transport for telephony mode. Returns False if setup fails."""
        logger.info("Starting WebSocket bot")
        if not self.ws:
            logger.error("WebSocket not initialized")
            return False
        await self.ws.accept()
        call_initiated_time = datetime.now(timezone.utc)

        # Parse WebSocket messages to get transport type and call data
        transport_type, call_data = await parse_telephony_websocket(self.ws)

        self.call_sid = call_data.get("call_id")
        self.stream_sid = call_data.get("stream_id")

        if not self.stream_sid or not self.call_sid:
            logger.error(
                f"Missing required call identifiers: stream_sid={self.stream_sid}, call_id={self.call_sid}"
            )
            return False

        logger.info(
            f"Parsed WebSocket: transport_type={transport_type}, "
            f"call_sid: {self.call_sid}, stream_sid: {self.stream_sid}"
        )

        self.lead = await update_lead_call_initiated_time(
            self.call_sid, call_initiated_time
        )
        if not self.lead:
            # Inbound call - extract template_id (handles IVR mode if enabled)
            template_id_from_query, error_reason = await get_template_id_from_call(
                ws=self.ws,
                stream_sid=self.stream_sid,
                call_sid=self.call_sid,
                call_data=call_data,
                provider=self.provider or "",
            )

            # Check if there was an error (IVR failed or invalid template_id)
            if error_reason:
                # WebSocket already closed by get_template_id_from_call
                return False

            # Handle inbound call - create lead on-the-fly
            if template_id_from_query:
                # Template ID from IVR selection or query param
                self.lead, error_reason = await create_lead_from_template_id(
                    template_id=template_id_from_query,
                    call_sid=self.call_sid,
                    call_data=call_data,
                    call_initiated_time=call_initiated_time,
                )
                if not self.lead:
                    error_msg = f"Inbound call handling failed (IVR): {error_reason}"
                    logger.info(error_msg)
                    track_error(self.errors, error_msg)
                    await close_websocket_safely(
                        self.ws,
                        code=4000,
                        reason=error_reason or "Failed to create lead from template",
                    )
                    return False
            else:
                # Standard inbound handling (no IVR, no template_id in params)
                self.lead, error_reason = await handle_inbound_call(
                    call_sid=self.call_sid,
                    call_data=call_data,
                    call_initiated_time=call_initiated_time,
                    provider=self.provider or "",
                )
                if not self.lead:
                    error_msg = f"Inbound call handling failed: {error_reason}"
                    logger.info(error_msg)
                    track_error(self.errors, error_msg)
                    await close_websocket_safely(
                        self.ws,
                        code=4000,
                        reason=error_reason or "Failed to handle inbound call",
                    )
                    return False

        try:
            self.template, self.configurations, self.template_vars = (
                await load_template_config(self.lead)
            )
        except ValueError as e:
            error_msg = f"Template loading failed: {str(e)}"
            logger.error(error_msg)
            track_error(self.errors, error_msg)
            if self.completion_function:
                await end_call_with_errors(
                    lead=self.lead,
                    errors=self.errors,
                    completion_function=self.completion_function,
                    transport_type=self.transport_type,
                    call_sid=self.call_sid,
                )
            await close_websocket_safely(self.ws, code=4000, reason=str(e))
            return False
        except Exception as e:
            error_msg = f"Unexpected error loading template: {str(e)}"
            logger.error(error_msg)
            track_error(self.errors, error_msg)
            if self.completion_function:
                await end_call_with_errors(
                    lead=self.lead,
                    errors=self.errors,
                    completion_function=self.completion_function,
                    transport_type=self.transport_type,
                    call_sid=self.call_sid,
                )
            await close_websocket_safely(
                self.ws, code=4000, reason="Template load error"
            )
            return False

        self.flow_builder = FlowConfigBuilder()
        for handler_name, handler_func in self.flow_builder.handler_map.items():
            self.flow_builder.handler_map[handler_name] = with_context(self)(
                handler_func
            )

        # Safe access for required attributes after lead check above
        if not self.ws or not self.stream_sid or not self.lead or not self.template:
            logger.error("Missing required attributes after setup")
            return False

        self.greeting_source = await send_initial_greeting(
            ws=self.ws,
            stream_sid=self.stream_sid,
            lead=self.lead,
            template=self.template,
            provider=self.provider or "",
            errors=self.errors,
        )

        self.vad_analyzer, self.default_vad_params = await create_vad_analyzer(
            is_daily_mode=False,
            template=self.template,
        )

        # Get transport params using the detected transport type
        transport_params = get_transport_params(self.vad_analyzer)
        params = transport_params[transport_type]()

        # Create transport with the call data
        self.transport = await _create_telephony_transport(
            self.ws, params, transport_type, call_data
        )

        logger.info(f"Created transport: {self.transport.__class__.__name__}")
        return True

    def _register_event_handlers(self) -> None:
        """Register transport and task event handlers."""
        if not self.transport or not self.task:
            logger.error("Transport or task not initialized")
            return

        @self.task.event_handler("on_pipeline_error")
        async def on_pipeline_error(task, error):
            """Capture TTS/STT/LLM pipeline failures."""
            processor = getattr(error, "processor", "unknown")
            error_msg = getattr(error, "error", str(error))
            detailed_msg = f"[PIPELINE] {processor}: {error_msg}"
            logger.info(f"[PIPELINE_ERROR] {detailed_msg}")
            track_error(self.errors, detailed_msg)

        @self.transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Client connected: {client}")
            await self._handle_client_connected()

        @self.transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info(f"Client disconnected: {client}")
            await self._handle_unexpected_disconnect("client_disconnected")

        @self.task.event_handler("on_idle_timeout")
        async def on_idle_timeout(task):
            logger.info("Idle timeout detected.")
            await self._handle_unexpected_disconnect("idle_timeout")

    async def _handle_client_connected(self) -> None:
        """Handle client connection and initialize flow."""
        if (
            not self.flow_builder
            or not self.template
            or not self.lead
            or not self.flow_manager
        ):
            logger.error("Required attributes not initialized for client connection")
            return

        (
            self.flow_config,
            self.end_conversation_callbacks,
            self.expected_callback_response_schema,
        ) = build_flow_config(self.flow_builder, self.template)

        lead_payload = self.lead.payload or {}
        initial_node_config = prepare_initial_node(
            flow_config=self.flow_config,
            lead_payload=lead_payload,
            configurations=self.configurations,
            has_greeting_source=bool(self.greeting_source),
        )

        await self.flow_manager.initialize(initial_node_config)
        logger.info(
            f"FlowManager initialized with initial node: {self.flow_config['initial_node']}"
        )

    async def _run_with_tracing(self, runner: PipelineRunner) -> None:
        """Run the pipeline with OpenTelemetry tracing."""
        if not self.lead or not self.task:
            logger.error("Lead or task not initialized for tracing")
            return

        lead_payload = self.lead.payload or {}
        self.root_span = create_root_span(
            conversation_id=self.conversation_id or "unknown",
            transport_type=self.transport_type,
            customer_name=lead_payload.get("customer_name", "unknown"),
            shop_name=lead_payload.get("shop_name", "unknown"),
            call_sid=self.call_sid or "unknown",
            order_id=self.lead.request_id or "unknown",
            provider=self.provider or "",
            template_type=self.lead.template,
        )
        try:
            with trace.use_span(self.root_span, end_on_exit=True):
                await runner.run(self.task)
        except Exception as e:
            error_msg = f"Error during traced pipeline execution: {e}"
            logger.error(error_msg)
            track_error(self.errors, error_msg)
            self.root_span.end()

    async def run(self, runner_args: Optional[RunnerArguments] = None) -> None:
        """Main entry point for running the agent.

        Args:
            runner_args: Required for Daily mode, contains room info and lead data
        """
        # Setup transport based on mode
        if self.is_daily_mode:
            if not runner_args:
                logger.error("runner_args is required for Daily mode")
                return
            await self._setup_daily_transport(runner_args)
        else:
            if not await self._setup_telephony_transport():
                return

        # Create services and pipeline
        stt, llm, tts = await create_services(self.configurations)
        pipeline, self.context, context_aggregator = await build_pipeline(
            self.transport, stt, llm, tts
        )

        # Generate conversation ID and create task
        lead_payload = self.lead.payload if self.lead else None
        self.conversation_id = generate_conversation_id(lead_payload)
        self.task = await create_pipeline_task(pipeline, self.conversation_id)

        # Validate required attributes for flow setup
        if not self.template:
            logger.error("Template is not set, cannot setup flow manager")
            return

        # Setup flow management
        self.flow_manager = setup_flow_manager(
            task=self.task,
            llm=llm,
            context_aggregator=context_aggregator,
            transport=self.transport,
            flow_builder=self.flow_builder,
            template=self.template,
        )
        self._register_event_handlers()

        # Run the pipeline
        runner = PipelineRunner(handle_sigint=False, force_gc=True)

        try:
            if ENABLE_BREEZE_BUDDY_TRACING:
                await self._run_with_tracing(runner)
            else:
                logger.info(
                    f"Running pipeline without tracing for conversation: {self.conversation_id}"
                )
                await runner.run(self.task)
        except asyncio.CancelledError:
            logger.info("Pipeline task cancelled. Exiting gracefully.")

    async def _handle_unexpected_disconnect(self, reason: str) -> None:
        """Handle unexpected disconnection and cleanup."""
        if self.conversation_ended:
            return

        logger.info(f"{reason}. Updating call status.")
        track_error(self.errors, reason)

        if self.lead:
            if self.lead.outcome is None:
                self.lead.outcome = DEFAULT_OUTCOME

            if self.lead.metaData is None:
                self.lead.metaData = {}

            # Simple mapping - we control these exact strings from our event handlers
            if reason == "idle_timeout":
                self.lead.metaData["call_ended_by"] = "system"
            elif reason == "client_disconnected":
                self.lead.metaData["call_ended_by"] = "customer"
            else:
                # Shouldn't happen, but safe fallback
                self.lead.metaData["call_ended_by"] = "agent"
                logger.warning(f"Unexpected disconnect reason: {reason}")

        context = TemplateContext(self)
        await end_conversation(context, {})


async def telephony_bot(
    ws: WebSocket,
    aiohttp_session: Any,
    hangup_function: Optional[Callable],
    completion_function: Optional[Callable],
    provider: CallProvider,
) -> None:
    """Entry point for telephony-based agents (Twilio/Exotel)."""
    agent = Agent(
        transport_type=provider,
        ws=ws,
        aiohttp_session=aiohttp_session,
        hangup_function=hangup_function,
        completion_function=completion_function,
        provider=provider,
    )
    await agent.run()


async def daily_bot(
    runner_args: RunnerArguments,
    completion_function: Callable,
    aiohttp_session: Any,
) -> None:
    """Entry point for Daily-based agents (web/mobile frontends).

    Args:
        runner_args: RunnerArguments containing Daily room info and lead data
        completion_function: Callback function to handle call completion
        aiohttp_session: aiohttp session for HTTP requests
    """
    agent = Agent(
        transport_type=TRANSPORT_TYPE_DAILY,
        aiohttp_session=aiohttp_session,
        completion_function=completion_function,
    )
    try:
        await agent.run(runner_args)
    finally:
        if aiohttp_session:
            await aiohttp_session.close()
            logger.info("[DAILY_MODE] Closed aiohttp session")
