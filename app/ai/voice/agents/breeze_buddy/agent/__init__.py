"""Voice agent for handling conversations via Daily or telephony transports."""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import WebSocket
from opentelemetry import trace
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
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
    prepare_resume_node,
    setup_flow_manager,
)
from app.ai.voice.agents.breeze_buddy.agent.inbound import (
    create_lead_from_template_id,
    handle_inbound_call,
)
from app.ai.voice.agents.breeze_buddy.agent.ivr import (
    BLOCK_MESSAGE_PLAY_SECONDS,
    _send_audio,
    get_template_id_from_call,
    prepare_block_audio,
)
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
    send_initial_greeting_daily,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.managers.utils import (
    prepare_and_store_initial_greeting,
)
from app.ai.voice.agents.breeze_buddy.mcp import get_mcp_global_functions
from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import (
    create_root_span,
)
from app.ai.voice.agents.breeze_buddy.processors import TranscriptCollectorProcessor
from app.ai.voice.agents.breeze_buddy.services.inbound_policy import (
    get_block_redirect,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.template import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.context import with_context
from app.ai.voice.agents.breeze_buddy.template.types import (
    LEGACY_VOICE_TO_PROVIDER,
    ConfigurationModel,
    InterruptionConfig,
    TemplateModel,
    TTSConfig,
    TTSProvider,
)
from app.ai.voice.agents.breeze_buddy.template.vad import create_vad_analyzer
from app.ai.voice.agents.breeze_buddy.utils.common import (
    track_error,
)
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import (
    close_websocket_safely,
)
from app.ai.voice.agents.breeze_buddy.utils.warm_transfer import set_transfer_flag
from app.core.config.static import ENABLE_BREEZE_BUDDY_TRACING
from app.core.logger import logger
from app.core.logger.context import (
    clear_log_context,
    set_log_context,
    update_log_context,
)
from app.database.accessor import get_lead_by_call_id, update_lead_call_initiated_time
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_initiated_time_by_id,
    update_lead_template,
)
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.schemas import CallProvider
from app.schemas.breeze_buddy.core import ExecutionMode, LeadCallTracker

DEFAULT_OUTCOME = "BUSY"
TTS_SPEAK_MAX_CHARS = 2000


class Agent:
    """Voice agent that handles conversations via Daily or telephony transports."""

    def __init__(
        self,
        transport_type: str,
        ws: Optional[WebSocket] = None,
        aiohttp_session: Any = None,
        completion_function: Optional[Callable] = None,
        provider: Optional[str] = None,
        telephony_service: Optional[VoiceCallProvider] = None,
    ):
        # Transport configuration
        self.transport_type = transport_type
        self.ws = ws
        self.aiohttp_session = aiohttp_session
        self.provider = provider
        self.completion_function = completion_function
        self.telephony_service = telephony_service

        # Runtime state
        self.task: Optional[PipelineTask] = None
        self.context: Optional[LLMContext] = None
        self.conversation_ended = False
        self.call_sid: Optional[str] = None
        self.stream_sid: Optional[str] = None
        self.vad_analyzer: Optional[SileroVADAnalyzer] = None
        self.transport: Any = None
        self.room_url: Optional[str] = None  # Daily room URL (Daily mode only)
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
        self.greeting_text: Optional[str] = (
            None  # Resolved greeting text for LLM context
        )
        self.default_vad_params: Optional[VADParams] = None
        self.default_interruption_config: Optional[InterruptionConfig] = None

        # User idle handling
        self._user_idle_callback_handler: Any = None
        self._context_aggregator: Any = None

        # Post-greeting idle detection
        self._post_greeting_task: Optional[asyncio.Task] = None
        self._user_spoke: bool = False

        # Transcription gate processor (always present in pipeline)
        self.speech_gate: Any = None

        # RTVI processor for daily mode real-time events
        self._rtvi_processor: Any = None

        # Stream mode transcript collector (replaces LLMContext for transcription)
        self._transcript_collector: Optional[TranscriptCollectorProcessor] = None

        # Error tracking
        self.errors: List[Dict[str, Any]] = []

        # Widget-mode resume seed (CHAT_MODE.md §14). Populated from
        # lead.metaData in _setup_*_transport when this voice call is a
        # transient attachment to an in-progress chat_session. When set,
        # the agent skips greeting playback and starts the FlowManager
        # at start_node with prior_history pre-loaded into LLM context.
        self._widget_resume_seed: Optional[Dict[str, Any]] = None

    @property
    def is_daily_mode(self) -> bool:
        return self.transport_type == TRANSPORT_TYPE_DAILY

    @property
    def is_stream_mode(self) -> bool:
        return (
            self.lead is not None
            and self.lead.execution_mode == ExecutionMode.DAILY_STREAM
        )

    async def _emit_rtvi_event(
        self, event_type: str, payload: Optional[Dict[str, Any]] = None
    ) -> None:
        """Emit a custom RTVI event to the connected client (daily mode only)."""
        if not self._rtvi_processor:
            return
        try:
            data: Dict[str, Any] = {
                "type": event_type,
                "timestamp": int(time.time() * 1000),
            }
            if payload:
                data["payload"] = payload
            await self._rtvi_processor.push_frame(RTVIServerMessageFrame(data=data))
        except Exception as e:
            logger.warning(f"Failed to emit RTVI event '{event_type}': {e}")

    async def _handle_user_idle_timeout(self, idle_retry_count: int) -> None:
        """Handle user idle timeout by ending call with BUSY outcome.

        This is passed as a callback to the user idle processor to trigger
        the full end_conversation flow which collects transcription, errors,
        and other metadata.

        Args:
            idle_retry_count: Number of idle retries that occurred
        """
        if self.conversation_ended:
            logger.debug(
                "Conversation already ended, skipping _handle_user_idle_timeout"
            )
            return

        # Don't set conversation_ended here - let end_conversation handler do it
        # This prevents end_conversation from skipping finalization

        if self.lead:
            self.lead.outcome = "BUSY"
            if self.lead.metaData is None:
                self.lead.metaData = {}
            self.lead.metaData["call_ended_by"] = "system"
            self.lead.metaData["call_end_reason"] = "user_idle_timeout"
            self.lead.metaData["idle_retry_count"] = idle_retry_count

            if self._transcript_collector:
                self.lead.metaData["transcription"] = (
                    self._transcript_collector.get_transcription()
                )

        logger.info(
            f"Ending call as BUSY due to user idle timeout "
            f"(retries: {idle_retry_count})"
        )

        context = TemplateContext(self)
        await end_conversation(context, {})

    async def _handle_post_greeting_idle(self, user_idle_config) -> None:
        """Handle post-greeting idle detection before user speaks for the first time.

        This runs as an asyncio task that waits for the idle timeout and triggers
        the first idle prompt if the user hasn't spoken yet.
        """
        try:
            # Wait for greeting to finish (~5s) + idle timeout before checking
            initial_delay = 5.0  # Estimated greeting duration
            await asyncio.sleep(initial_delay + user_idle_config.timeout)

            # If user never spoke, trigger first idle prompt
            if not self._user_spoke and self.task:
                logger.info("Post-greeting idle detected. Triggering first prompt.")
                await self.task.queue_frames(
                    [
                        LLMMessagesAppendFrame(
                            [
                                {
                                    "role": "system",
                                    "content": user_idle_config.idle_message,
                                }
                            ],
                            run_llm=True,
                        )
                    ]
                )
        except asyncio.CancelledError:
            logger.debug("Post-greeting idle timer cancelled.")
            return

    async def _setup_daily_transport(self, runner_args: RunnerArguments) -> None:
        """Initialize transport for Daily mode."""
        if not runner_args or not runner_args.body:
            raise ValueError("runner_args with body is required for Daily mode")

        lead_id = runner_args.body.get("lead_id")
        if not lead_id:
            raise ValueError("lead_id is required in runner_args.body for Daily mode")

        # Set lead_id context immediately - all subsequent logs will include it
        set_log_context(lead_id=str(lead_id))

        call_initiated_time = datetime.now(timezone.utc)
        self.lead = await update_lead_call_initiated_time_by_id(
            lead_id, call_initiated_time
        )
        if not self.lead:
            raise ValueError(f"Lead not found for lead_id: {lead_id}")

        # Update context with call_sid (lead_id already set above)
        self.call_sid = (
            self.lead.call_id or f"daily-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        update_log_context(call_sid=self.call_sid)

        # Widget-mode resume seed: see CHAT_MODE.md §14. When this voice
        # call is a transient attachment to an in-progress chat_session,
        # the unified widget router stuffs the seed into lead.metaData.
        # We capture it here so the greeting / initial-node code paths
        # below can branch on it cleanly.
        meta = self.lead.metaData or {}
        widget_session_id = meta.get("widget_session_id")
        if widget_session_id:
            self._widget_resume_seed = {
                "widget_session_id": str(widget_session_id),
                "start_node": meta.get("start_node"),
                "prior_history": list(meta.get("prior_history") or []),
                "seed_message_count": int(meta.get("seed_message_count", 0) or 0),
            }
            logger.info(
                f"Widget voice resume: chat_session={widget_session_id} "
                f"start_node={self._widget_resume_seed['start_node']!r} "
                f"prior_msgs={len(self._widget_resume_seed['prior_history'])}"
            )

        logger.info(
            f"Starting Daily bot for lead_id: {lead_id}, call_sid: {self.call_sid}"
        )

        # Stream mode skips flow builder — no LLM/template nodes needed
        if not self.is_stream_mode:
            self.flow_builder = FlowConfigBuilder()
            for handler_name, handler_func in self.flow_builder.handler_map.items():
                self.flow_builder.handler_map[handler_name] = with_context(self)(
                    handler_func
                )

        try:
            (
                self.template,
                self.configurations,
                self.template_vars,
            ) = await load_template_config(self.lead)
        except ValueError as e:
            logger.error(f"Failed to load template config for Daily mode: {e}")
            raise

        # Synthesize and cache the initial greeting in Redis so it can be
        # played out on client-connect. Idempotent: if the dispatch worker
        # pre-synthesized the audio at dispatch time (outbound), this is a
        # Redis hit and
        # skips TTS. Bounded by a short timeout so a hung TTS does not block
        # the room from accepting the client. Stream mode skips this — no
        # LLM/template playback in passthrough mode.
        if not self.is_stream_mode:
            try:
                await asyncio.wait_for(
                    prepare_and_store_initial_greeting(
                        lead_id=self.lead.id,
                        payload=self.lead.payload or {},
                        template=self.template,
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Daily greeting synthesis timed out for lead {self.lead.id}; "
                    "client will hear no greeting (LLM may speak first instead)"
                )
            except Exception as e:
                logger.warning(
                    f"Daily greeting synthesis failed for lead {self.lead.id}: {e}; "
                    "client will hear no greeting (LLM may speak first instead)"
                )

        self.vad_analyzer, self.default_vad_params = await create_vad_analyzer(
            is_daily_mode=True,
            template=self.template,
        )

        transport_params = get_transport_params(self.template, self.configurations)
        self.transport = await create_transport(runner_args, transport_params)
        self.room_url = getattr(runner_args, "room_url", None)

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

        # Set call_sid context early for log isolation
        set_log_context(call_sid=self.call_sid)

        logger.info(
            f"Parsed WebSocket: transport_type={transport_type}, "
            f"call_sid: {self.call_sid}, stream_sid: {self.stream_sid}"
        )

        # Check for Exotel block-redirect (set at answer-time when inbound
        # policy blocks with REDIRECT action). Play message, set transfer
        # flag, close WS. Exotel applet handles the redirect via /dial-up.
        if await self._handle_block_redirect(transport_type):
            clear_log_context()
            return False

        self.lead = await update_lead_call_initiated_time(
            self.call_sid, call_initiated_time
        )

        # Extract URL query params for Plivo inbound (contains from_number, to_number)
        url_query_params = dict(self.ws.query_params) if self.ws else {}
        from_number = call_data.get("from") or url_query_params.get("from_number", "")

        if not self.lead:
            # Inbound call - extract template_id (handles IVR mode if enabled)
            (
                template_id_from_query,
                error_reason,
                _was_ivr,
            ) = await get_template_id_from_call(
                ws=self.ws,
                stream_sid=self.stream_sid,
                call_sid=self.call_sid,
                call_data=call_data,
                provider=self.provider or "",
                telephony_service=self.telephony_service,
                from_number=from_number,
            )

            # Check if there was an error (IVR failed or invalid template_id)
            if error_reason:
                # WebSocket already closed by get_template_id_from_call
                clear_log_context()
                return False

            # Handle inbound call - create lead on-the-fly
            if template_id_from_query:
                # Template ID from IVR selection or query param
                self.lead, error_reason = await create_lead_from_template_id(
                    template_id=template_id_from_query,
                    call_sid=self.call_sid,
                    call_data=call_data,
                    call_initiated_time=call_initiated_time,
                    url_query_params=url_query_params,
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
                    clear_log_context()
                    return False
            else:
                # Standard inbound handling (no IVR, no template_id in params)
                self.lead, error_reason = await handle_inbound_call(
                    call_sid=self.call_sid,
                    call_data=call_data,
                    call_initiated_time=call_initiated_time,
                    provider=self.provider or "",
                    url_query_params=url_query_params,
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
                    clear_log_context()
                    return False
        else:
            # Lead was already created in the answer handler (e.g. for inbound calls).
            # If this is IVR mode, we still need to run template selection and update
            # the lead if a different template was chosen.
            ivr_mode = url_query_params.get("ivr_mode") == "true"
            if ivr_mode:
                (
                    template_id_from_query,
                    error_reason,
                    _was_ivr,
                ) = await get_template_id_from_call(
                    ws=self.ws,
                    stream_sid=self.stream_sid,
                    call_sid=self.call_sid,
                    call_data=call_data,
                    provider=self.provider or "",
                    telephony_service=self.telephony_service,
                    from_number=from_number,
                )
                if error_reason:
                    clear_log_context()
                    return False
                if (
                    template_id_from_query
                    and self.lead.template_id != template_id_from_query
                ):
                    template = await get_template_by_id(template_id_from_query)
                    if template:
                        updated_lead = await update_lead_template(
                            lead_id=self.lead.id,
                            template=template.name,
                            template_id=str(template.id),
                        )
                        if updated_lead:
                            self.lead = updated_lead
                        else:
                            logger.warning(
                                f"Failed to update lead template to "
                                f"{template_id_from_query} for lead {self.lead.id}"
                            )

        # Update context with lead_id (call_sid already set above)
        update_log_context(lead_id=str(self.lead.id))

        try:
            (
                self.template,
                self.configurations,
                self.template_vars,
            ) = await load_template_config(self.lead)
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
            clear_log_context()
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
            clear_log_context()
            return False

        self.flow_builder = FlowConfigBuilder()
        for handler_name, handler_func in self.flow_builder.handler_map.items():
            self.flow_builder.handler_map[handler_name] = with_context(self)(
                handler_func
            )

        # Safe access for required attributes after lead check above
        if not self.ws or not self.stream_sid or not self.lead or not self.template:
            logger.error("Missing required attributes after setup")
            clear_log_context()
            return False

        # Inbound calls have no pre-call window to synthesize the greeting
        # (lead is created on-the-fly here), so do it before send_initial_greeting
        # to avoid the dial-tone fallback. Idempotent for outbound — the
        # dispatch worker has already populated the cache, so this is a
        # Redis hit and no-op.
        # Bounded by a short timeout: if TTS hangs, the WS is already accepted
        # and the customer would hear dead air. On timeout/error, fall through
        # to send_initial_greeting which plays the dial-tone fallback.
        try:
            await asyncio.wait_for(
                prepare_and_store_initial_greeting(
                    lead_id=self.lead.id,
                    payload=self.lead.payload or {},
                    template=self.template,
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Greeting synthesis timed out for lead {self.lead.id}; "
                "falling back to dial tone"
            )
        except Exception as e:
            logger.warning(
                f"Greeting synthesis failed for lead {self.lead.id}: {e}; "
                "falling back to dial tone"
            )

        greeting_result = await send_initial_greeting(
            ws=self.ws,
            stream_sid=self.stream_sid,
            lead=self.lead,
            template=self.template,
            provider=self.provider or "",
            errors=self.errors,
        )
        self.greeting_source = greeting_result.source
        self.greeting_text = greeting_result.text

        # Start post-greeting idle timer if greeting was sent
        if self.greeting_source and self.configurations:
            user_idle_config = getattr(
                self.configurations, "user_idle_configuration", None
            )
            if user_idle_config and user_idle_config.enabled:
                self._post_greeting_task = asyncio.create_task(
                    self._handle_post_greeting_idle(user_idle_config)
                )

        self.vad_analyzer, self.default_vad_params = await create_vad_analyzer(
            is_daily_mode=False,
            template=self.template,
        )

        # Get transport params using the detected transport type
        transport_params = get_transport_params(self.template, self.configurations)
        params = transport_params[transport_type]()

        # Create transport with the call data
        self.transport = await _create_telephony_transport(
            self.ws, params, transport_type, call_data
        )

        logger.info(f"Created transport: {self.transport.__class__.__name__}")
        return True

    async def _handle_block_redirect(self, transport_type: str) -> bool:
        """Handle Exotel block-redirect set at answer-time.

        When inbound policy blocks a call with REDIRECT action on Exotel,
        the answer handler accepts the call but stores redirect info in Redis.
        This method detects that, plays the block message, sets the transfer
        flag (so Exotel /dial-up callback returns the redirect number), and
        closes the WebSocket.

        Returns True if the call was handled (caller should return False from setup).
        """
        if not self.call_sid:
            return False

        redirect_info = await get_block_redirect(self.call_sid)
        if not redirect_info:
            return False

        redirect_number = redirect_info.get("redirect_number")
        block_message = redirect_info.get("message")
        reseller_id = redirect_info.get("reseller_id")
        merchant_id = redirect_info.get("merchant_id")

        logger.info(
            f"[BLOCK_REDIRECT] Call {self.call_sid} blocked with redirect to {redirect_number}"
        )

        # Play block message if available (with caching)
        if block_message and self.ws and self.stream_sid:
            try:
                audio = await prepare_block_audio(block_message, self.provider or "")
                if audio:
                    await _send_audio(
                        self.ws, self.stream_sid, audio, self.provider or ""
                    )
                    await asyncio.sleep(BLOCK_MESSAGE_PLAY_SECONDS)
            except Exception as e:
                logger.warning(f"[BLOCK_REDIRECT] Failed to play block message: {e}")

        # Set transfer flag so Exotel /dial-up callback returns the redirect number
        if redirect_number:
            await set_transfer_flag(
                call_sid=self.call_sid,
                reseller_id=reseller_id or "",
                merchant_id=merchant_id or "",
                transfer_number=redirect_number,
            )

        # Close WebSocket — Exotel applet detects stream end and calls /dial-up
        if self.ws:
            await close_websocket_safely(self.ws, code=1000, reason="Block redirect")
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
            if self._rtvi_processor:
                await self._emit_rtvi_event(
                    "pipeline-error",
                    {"processor": str(processor), "error": error_msg},
                )

        @self.transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Client connected: {client}")
            if self._rtvi_processor:
                await self._emit_rtvi_event("conversation-start")
            await self._handle_client_connected()

        @self.transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info(f"Client disconnected: {client}")
            if self._rtvi_processor:
                await self._emit_rtvi_event(
                    "conversation-end", {"reason": "client_disconnected"}
                )
            # Cancel post-greeting idle task if still running
            if self._post_greeting_task and not self._post_greeting_task.done():
                self._post_greeting_task.cancel()
                self._post_greeting_task = None
                logger.info(
                    "Cancelling the post greeting task due to client disconnect"
                )
            await self._handle_unexpected_disconnect("client_disconnected")

        @self.task.event_handler("on_idle_timeout")
        async def on_idle_timeout(task):
            logger.info("Idle timeout detected.")
            if self._rtvi_processor:
                await self._emit_rtvi_event(
                    "conversation-end", {"reason": "idle_timeout"}
                )
            await self._handle_unexpected_disconnect("idle_timeout")

        # Register RTVI-specific event handlers for daily mode
        if self._rtvi_processor:

            @self._rtvi_processor.event_handler("on_client_ready")
            async def on_client_ready(rtvi):
                await rtvi.push_frame(
                    RTVIServerMessageFrame(data={"type": "bot-ready"})
                )

        # Stream mode: accept tts-speak via RTVI client-message (PipecatClient SDK)
        if self.is_stream_mode and self._rtvi_processor:

            @self._rtvi_processor.event_handler("on_client_message")
            async def on_client_message(rtvi, message):
                if message.type == "tts-speak":
                    data = message.data or {}
                    text = data.get("text", "")
                    if not isinstance(text, str) or not text:
                        return
                    if len(text) > TTS_SPEAK_MAX_CHARS:
                        logger.warning(
                            f"[STREAM] tts-speak text exceeds {TTS_SPEAK_MAX_CHARS} chars "
                            f"({len(text)}), truncating"
                        )
                        text = text[:TTS_SPEAK_MAX_CHARS]
                    if self.task:
                        logger.debug(f"[STREAM] TTS speak: {text[:80]}")
                        await self.task.queue_frame(TTSSpeakFrame(text=text))

        # Register user turn started event to reset idle retry counter
        if self._context_aggregator and self._user_idle_callback_handler:
            user_aggregator = self._context_aggregator.user()

            @user_aggregator.event_handler("on_user_turn_started")
            async def on_user_turn_started(aggregator, strategy):
                """Reset idle retry counter when user starts speaking."""
                # Detect first user speech and cancel post-greeting timer
                if not self._user_spoke:
                    self._user_spoke = True
                    if self._post_greeting_task:
                        self._post_greeting_task.cancel()
                        self._post_greeting_task = None
                        logger.debug("Post-greeting timer cancelled - user spoke")
                self._user_idle_callback_handler.reset_retry_count()

    async def _handle_client_connected(self) -> None:
        """Handle client connection and initialize flow."""
        if self.is_stream_mode:
            if self.lead and self.lead.metaData is None:
                self.lead.metaData = {}
            logger.info("[STREAM] Client connected — ready for STT/TTS")
            return

        if (
            not self.flow_builder
            or not self.template
            or not self.lead
            or not self.flow_manager
        ):
            logger.error("Required attributes not initialized for client connection")
            return

        # Daily mode plays the pre-synthesized greeting through the pipeline
        # transport on client-connect (telephony plays it out-of-band during
        # _setup_telephony_transport, before the pipeline starts). Setting
        # greeting_source/text here makes prepare_initial_node inject the
        # greeting into the LLM context as an assistant message and switch
        # respond_immediately=False — same downstream behavior as telephony.
        # DAILY_STREAM also uses a Daily transport but is client-driven STT/
        # TTS-only (no LLM, no template playback) — explicitly skip greeting
        # injection there so we don't push audio into a passthrough pipeline.
        # Widget-mode resume: skip the greeting too. Prior chat history is
        # already in the seed; re-greeting would repeat what the user
        # already saw and feels broken (CHAT_MODE.md §14).
        if (
            self.is_daily_mode
            and not self.is_stream_mode
            and not self._widget_resume_seed
            and self.task
        ):
            greeting_result = await send_initial_greeting_daily(
                task=self.task,
                lead=self.lead,
                template=self.template,
                errors=self.errors,
            )
            self.greeting_source = greeting_result.source
            self.greeting_text = greeting_result.text

            # Mirror telephony: start post-greeting idle timer so the bot
            # re-engages if the user stays silent after the greeting.
            if self.greeting_source and self.configurations:
                user_idle_config = getattr(
                    self.configurations, "user_idle_configuration", None
                )
                if user_idle_config and user_idle_config.enabled:
                    self._post_greeting_task = asyncio.create_task(
                        self._handle_post_greeting_idle(user_idle_config)
                    )

        (
            self.flow_config,
            self.end_conversation_callbacks,
            self.expected_callback_response_schema,
        ) = build_flow_config(self.flow_builder, self.template)

        lead_payload = self.lead.payload or {}

        # Widget-mode resume: start at the chat's current_node with the
        # chat's history pre-seeded. See prepare_resume_node for why
        # the history goes inside task_messages (FlowManager always
        # RESETs context on first node init — we have to put the seed
        # inside the same frame to survive).
        if self._widget_resume_seed:
            initial_node_config = prepare_resume_node(
                flow_config=self.flow_config,
                lead_payload=lead_payload,
                configurations=self.configurations,
                start_node_name=self._widget_resume_seed.get("start_node"),
                prior_history=self._widget_resume_seed.get("prior_history") or [],
            )
        else:
            initial_node_config = prepare_initial_node(
                flow_config=self.flow_config,
                lead_payload=lead_payload,
                configurations=self.configurations,
                has_greeting_source=bool(self.greeting_source),
                greeting_text=self.greeting_text,
            )

        # Initialize node traversal tracking
        if self.lead.metaData is None:
            self.lead.metaData = {}
        self.lead.metaData["node_traversal"] = []

        # Record initial-node entry BEFORE flow_manager.initialize so that any
        # global function called during the first LLM turn (e.g. get_driver_info
        # on the initial node) finds an active node entry to record against.
        # For widget resume we use the resume node name (which falls back to
        # initial_node if start_node is missing — see prepare_resume_node).
        if self._widget_resume_seed:
            initial_node_name = (
                self._widget_resume_seed.get("start_node")
                or self.flow_config["initial_node"]
            )
            if initial_node_name not in self.flow_config["nodes"]:
                initial_node_name = self.flow_config["initial_node"]
        else:
            initial_node_name = self.flow_config["initial_node"]
        context = TemplateContext(self)
        context.record_node_entry(initial_node_name)

        await self.flow_manager.initialize(initial_node_config)
        logger.info(
            f"FlowManager initialized at node: {initial_node_name}"
            + (
                f" (widget resume from chat_session "
                f"{self._widget_resume_seed['widget_session_id']})"
                if self._widget_resume_seed
                else ""
            )
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
            evaluator_config=(
                self.configurations.evaluator_config if self.configurations else None
            ),
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
        try:
            # Setup transport based on mode
            if self.is_daily_mode:
                if not runner_args:
                    logger.error("runner_args is required for Daily mode")
                    return
                await self._setup_daily_transport(runner_args)
            else:
                if not await self._setup_telephony_transport():
                    if self.completion_function and self.call_sid:

                        lead = await get_lead_by_call_id(self.call_sid)
                        # If lead is None (not found), or it doesn't have an outcome,
                        # or the outcome is not a BLOCKED_ outcome, then it's an early hangup.
                        if not lead or not lead.outcome:
                            await self.completion_function(
                                call_id=self.call_sid,
                                outcome="EARLY_HANGUP",
                                call_end_time=datetime.now(timezone.utc),
                            )
                    return

            # Override TTS provider if LLM-based selection was done at lead push time.
            # resolve_voice_config() will pick per-provider settings from
            # tts_configuration_overrides if available, else Redis defaults.
            if self.lead and self.lead.payload:
                payload_provider = self.lead.payload.get(
                    "tts_provider"
                ) or LEGACY_VOICE_TO_PROVIDER.get(
                    (self.lead.payload.get("tts_voice_name") or "").lower()
                )
                if payload_provider and self.configurations:
                    try:
                        provider_enum = TTSProvider(payload_provider)
                        logger.info(
                            f"Overriding TTS provider from payload: {provider_enum.value}"
                        )
                        existing = self.configurations.tts_configuration
                        if existing and existing.provider == provider_enum:
                            # Same provider — keep existing template settings
                            pass
                        else:
                            # Different provider — create minimal config;
                            # resolve_voice_config will fill from overrides/defaults
                            self.configurations.tts_configuration = TTSConfig(
                                provider=provider_enum
                            )
                    except ValueError:
                        logger.warning(
                            f"Invalid TTS provider '{payload_provider}' in payload, keeping existing config"
                        )

            # Build services and pipeline. Stream mode skips LLM creation and
            # runs build_pipeline with mode="stream" (no LLM processor, no
            # assistant aggregator, transcript collector inserted, no user idle).
            # All other wiring is identical.
            is_stream = self.is_stream_mode
            stt, llm, tts = await create_services(
                self.configurations, include_llm=not is_stream
            )
            if not is_stream:
                assert llm is not None, "LLM is required in agent mode"

            (
                pipeline,
                context,
                context_aggregator,
                user_idle_callback_handler,
                self.speech_gate,
                self._transcript_collector,
            ) = await build_pipeline(
                self.transport,
                stt,
                llm,
                tts,
                self.vad_analyzer,
                self.configurations,
                on_user_idle_timeout=(
                    None if is_stream else self._handle_user_idle_timeout
                ),
                mode="stream" if is_stream else "agent",
            )
            self._context_aggregator = context_aggregator

            # Stream mode deliberately leaves self.context=None so end_conversation
            # falls back to the transcript collector (captures both user turns AND
            # client-driven bot TTS text). The aggregator writes user turns into
            # the LLMContext, but nothing writes bot TTS text — using the context
            # would lose the bot side of the transcript.
            if not is_stream:
                self.context = context
                self._user_idle_callback_handler = user_idle_callback_handler
                self.default_interruption_config = (
                    getattr(self.configurations, "interruption", None)
                    or InterruptionConfig()
                )

            lead_payload = self.lead.payload if self.lead else None
            self.conversation_id = generate_conversation_id(lead_payload)
            update_log_context(conversation_id=self.conversation_id)

            self.task = await create_pipeline_task(
                pipeline, self.conversation_id, is_daily_mode=self.is_daily_mode
            )

            if self.is_daily_mode and hasattr(self.task, "rtvi") and self.task.rtvi:
                self._rtvi_processor = self.task.rtvi

            # Flow manager is agent-mode only (stream mode has no LLM to drive
            # node transitions or function calls).
            if not is_stream:
                if not self.template:
                    logger.error("Template is not set, cannot setup flow manager")
                    return

                # Fetch MCP tools if configured in template configuration
                mcp_global_functions = []
                mcp_config = self.configurations.mcp if self.configurations else None
                if mcp_config and mcp_config.servers:
                    try:
                        mcp_global_functions = await get_mcp_global_functions(
                            mcp_config=mcp_config,
                            template_vars=self.template_vars,
                        )
                    except Exception as e:
                        logger.error(
                            f"[BUDDY_MCP] Failed to load MCP tools, continuing without them: {e}"
                        )

                assert llm is not None  # narrowed: non-stream path always has LLM
                self.flow_manager = setup_flow_manager(
                    task=self.task,
                    llm=llm,
                    context_aggregator=context_aggregator,
                    transport=self.transport,
                    flow_builder=self.flow_builder,
                    template=self.template,
                    bot_instance=self,
                    mcp_global_functions=mcp_global_functions,
                )

            self._register_event_handlers()

            runner = PipelineRunner(handle_sigint=False, force_gc=True)
            log_prefix = "[STREAM] " if is_stream else ""
            try:
                if ENABLE_BREEZE_BUDDY_TRACING:
                    await self._run_with_tracing(runner)
                else:
                    logger.info(
                        f"{log_prefix}Running pipeline for conversation: {self.conversation_id}"
                    )
                    await runner.run(self.task)
            except asyncio.CancelledError:
                logger.info(f"{log_prefix}Pipeline task cancelled. Exiting gracefully.")
        finally:
            clear_log_context()

    async def _handle_unexpected_disconnect(self, reason: str) -> None:
        """Handle unexpected disconnection and cleanup."""
        if self.conversation_ended:
            return

        logger.info(f"{reason}. Updating call status.")

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
                self.lead.metaData["call_ended_by"] = "agent"
                logger.warning(f"Unexpected disconnect reason: {reason}")

            # Stream mode: populate transcription from collector before
            # end_conversation runs (no LLMContext to pull from)
            if self._transcript_collector:
                self.lead.metaData["transcription"] = (
                    self._transcript_collector.get_transcription()
                )

        context = TemplateContext(self)
        await end_conversation(context, {})


async def telephony_bot(
    ws: WebSocket,
    aiohttp_session: Any,
    completion_function: Optional[Callable],
    provider: CallProvider,
    telephony_service: Optional[VoiceCallProvider] = None,
) -> None:
    """Entry point for telephony-based agents (Twilio/Exotel)."""
    agent = Agent(
        transport_type=provider,
        ws=ws,
        aiohttp_session=aiohttp_session,
        completion_function=completion_function,
        provider=provider,
        telephony_service=telephony_service,
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
