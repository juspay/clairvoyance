"""Voice agent for handling conversations via Daily or telephony transports."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import WebSocket
from opentelemetry import trace
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.service_switcher import ManuallySwitchServiceFrame
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
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
)
from app.ai.voice.agents.breeze_buddy.agent.vad import create_vad_analyzer
from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import (
    create_root_span,
)
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
    ConfigurationModel,
    TemplateModel,
    TTSVoiceName,
)
from app.ai.voice.agents.breeze_buddy.utils.common import (
    create_background_sound_mixer,
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
from app.database.accessor import update_lead_call_initiated_time
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_initiated_time_by_id,
)
from app.schemas import CallProvider
from app.schemas.breeze_buddy.core import LeadCallTracker
from app.services.slack import slack_alert

DEFAULT_OUTCOME = "BUSY"


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

        # User idle handling
        self._user_idle_callback_handler: Any = None
        self._context_aggregator: Any = None

        # Post-greeting idle detection
        self._post_greeting_task: Optional[asyncio.Task] = None
        self._user_spoke: bool = False

        # Transcription gate processor (always present in pipeline)
        self.speech_gate: Any = None

        # STT provider tracking (for mid-call error attribution)
        self.stt_provider: str = "soniox"

        # Mid-call STT fallback (ServiceSwitcher)
        self.fallback_stt: Optional[Any] = None
        self.stt_switched: bool = False

        # Error tracking
        self.errors: List[Dict[str, Any]] = []

    @property
    def is_daily_mode(self) -> bool:
        return self.transport_type == TRANSPORT_TYPE_DAILY

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

    async def _send_mid_call_stt_alert(self, processor: str, error_msg: str) -> None:
        """Send Slack alert for mid-call STT failure and gracefully end the call.

        This is fire-and-forget: if the Slack call fails, the call still terminates.
        """
        call_sid = self.call_sid or "unknown"
        lead_id = str(self.lead.id) if self.lead else "unknown"
        try:
            await slack_alert.send(
                title="🚨 Mid-Call STT Failure (Breeze Buddy)",
                fields=[
                    {"name": "Provider", "value": self.stt_provider},
                    {"name": "Processor", "value": str(processor)},
                    {"name": "Call SID", "value": call_sid},
                    {"name": "Lead ID", "value": lead_id},
                ],
                sections=[
                    {"title": "Error Details", "text": f"```{error_msg[:500]}```"},
                ],
                fallback_text=f"Mid-call STT failure: {self.stt_provider} — call {call_sid}",
            )
        except Exception as alert_err:
            logger.warning(f"Failed to send mid-call STT Slack alert: {alert_err}")

    async def _send_stt_fallback_activated_alert(
        self, failed_provider: str, processor: str, error_msg: str
    ) -> None:
        """Send Slack alert when mid-call STT fallback is activated.

        Notifies that the call is continuing with the backup provider.
        """
        call_sid = self.call_sid or "unknown"
        lead_id = str(self.lead.id) if self.lead else "unknown"
        try:
            await slack_alert.send(
                title="⚠️ STT Fallback Activated (Breeze Buddy)",
                fields=[
                    {"name": "Failed Provider", "value": failed_provider},
                    {"name": "Switched To", "value": "deepgram"},
                    {"name": "Processor", "value": str(processor)},
                    {"name": "Call SID", "value": call_sid},
                    {"name": "Lead ID", "value": lead_id},
                ],
                sections=[
                    {
                        "title": "Status",
                        "text": f"Call is continuing with Deepgram fallback.\n```{error_msg[:500]}```",
                    },
                ],
                fallback_text=f"STT fallback activated: {failed_provider} → deepgram — call {call_sid}",
            )
        except Exception as alert_err:
            logger.warning(
                f"Failed to send STT fallback activation Slack alert: {alert_err}"
            )

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

        logger.info(
            f"Starting Daily bot for lead_id: {lead_id}, call_sid: {self.call_sid}"
        )

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

        self.vad_analyzer, self.default_vad_params = await create_vad_analyzer(
            is_daily_mode=True
        )

        # Daily transport does not support audio_out_mixer, so we pass None
        # Note: VAD is configured in the aggregator (via UserTurnStrategies), not the transport
        transport_params = get_transport_params(None, self.configurations)
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
        if not self.lead:
            # Extract URL query params for Plivo inbound (contains from_number, to_number)
            url_query_params = dict(self.ws.query_params) if self.ws else {}
            from_number = call_data.get("from") or url_query_params.get(
                "from_number", ""
            )

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

        # Create background sound mixer if configured in template
        audio_out_mixer = create_background_sound_mixer(self.template)

        # Get transport params using the detected transport type
        # Note: VAD is configured in the aggregator (via UserTurnStrategies), not the transport
        transport_params = get_transport_params(audio_out_mixer, self.configurations)
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
            """Capture TTS/STT/LLM pipeline failures.

            For STT errors: if a fallback service is available and hasn't
            been used yet, hot-swap to it via ServiceSwitcher. If the
            fallback also fails (or none is available), end the call.
            """
            processor = getattr(error, "processor", "unknown")
            error_msg = getattr(error, "error", str(error))
            detailed_msg = f"[PIPELINE] {processor}: {error_msg}"
            logger.info(f"[PIPELINE_ERROR] {detailed_msg}")
            track_error(self.errors, detailed_msg)

            # Detect STT-specific failures by processor name
            processor_name = str(processor).lower()
            stt_keywords = ("soniox", "deepgram", "stt", "speech", "transcri")
            if any(kw in processor_name for kw in stt_keywords):
                failed_provider = self.stt_provider

                # --- Attempt mid-call fallback via ServiceSwitcher ---
                if self.fallback_stt and not self.stt_switched and self.task:
                    logger.warning(
                        f"Mid-call STT failure (provider={failed_provider}, "
                        f"processor={processor}). Switching to Deepgram fallback."
                    )
                    # Push switch frame to ServiceSwitcher
                    await self.task.queue_frame(
                        ManuallySwitchServiceFrame(service=self.fallback_stt)
                    )
                    self.stt_provider = "deepgram"
                    self.stt_switched = True

                    # Fire-and-forget Slack alert (fallback activated)
                    asyncio.create_task(
                        self._send_stt_fallback_activated_alert(
                            failed_provider, str(processor), str(error_msg)
                        )
                    )
                    # Mark lead metadata for analytics
                    if self.lead:
                        if self.lead.metaData is None:
                            self.lead.metaData = {}
                        self.lead.metaData["stt_fallback_triggered"] = True
                        self.lead.metaData["stt_original_provider"] = failed_provider
                        self.lead.metaData["stt_provider"] = "deepgram"
                    return  # Call continues with Deepgram

                # --- Guard: ignore stale errors from the old provider ---
                # After switching, Soniox may still emit queued errors
                # (e.g. from a reconnect attempt that was in-flight).
                # Only treat as "both failed" if the error comes from Deepgram.
                if self.stt_switched and "deepgram" not in processor_name:
                    logger.info(
                        f"Ignoring stale STT error from old provider "
                        f"(processor={processor}) — already switched to Deepgram."
                    )
                    return

                # --- No fallback available or already exhausted ---
                logger.error(
                    f"Mid-call STT failure (provider={failed_provider}, "
                    f"processor={processor}). "
                    + (
                        "Both providers failed."
                        if self.stt_switched
                        else "No fallback available."
                    )
                    + " Ending call gracefully."
                )
                # Fire-and-forget Slack alert (total failure)
                asyncio.create_task(
                    self._send_mid_call_stt_alert(str(processor), str(error_msg))
                )
                # Mark lead metadata for post-call analytics
                if self.lead:
                    if self.lead.metaData is None:
                        self.lead.metaData = {}
                    self.lead.metaData["call_ended_by"] = "system"
                    self.lead.metaData["call_end_reason"] = (
                        "stt_both_providers_failed"
                        if self.stt_switched
                        else "stt_mid_call_failure"
                    )
                    self.lead.metaData["stt_provider"] = self.stt_provider
                # Gracefully end the conversation
                context = TemplateContext(self)
                await end_conversation(context, {})

        @self.transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Client connected: {client}")
            await self._handle_client_connected()

        @self.transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info(f"Client disconnected: {client}")
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
            await self._handle_unexpected_disconnect("idle_timeout")

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
            greeting_text=self.greeting_text,
        )

        # Initialize node traversal tracking
        if self.lead.metaData is None:
            self.lead.metaData = {}
        self.lead.metaData["node_traversal"] = []

        await self.flow_manager.initialize(initial_node_config)

        # Record initial node entry after FlowManager is initialized
        initial_node_name = self.flow_config["initial_node"]
        context = TemplateContext(self)
        context.record_node_entry(initial_node_name)
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
        try:
            # Setup transport based on mode
            if self.is_daily_mode:
                if not runner_args:
                    logger.error("runner_args is required for Daily mode")
                    return
                await self._setup_daily_transport(runner_args)
            else:
                if not await self._setup_telephony_transport():
                    return

            # Override TTS voice name if LLM-based selection was done at lead push time
            if self.lead and self.lead.payload:
                payload_voice = self.lead.payload.get("tts_voice_name")
                if payload_voice and self.configurations:
                    try:
                        voice_enum = TTSVoiceName(payload_voice)
                        logger.info(
                            f"Overriding TTS voice from payload: {voice_enum.value}"
                        )
                        self.configurations.tts_voice_name = voice_enum
                    except ValueError:
                        logger.warning(
                            f"Invalid TTS voice '{payload_voice}' in payload, keeping existing config"
                        )

            # Create services and pipeline
            # VAD analyzer is passed to build_pipeline where it's configured inside the
            # LLMUserAggregator. This enables UserTurnStrategies (VAD + Transcription fallback).
            stt_result, llm, tts = await create_services(self.configurations)
            self.stt_provider = stt_result.provider
            if stt_result.is_fallback:
                logger.warning(
                    f"Call starting with fallback STT provider: {stt_result.provider}"
                )
            (
                pipeline,
                self.context,
                context_aggregator,
                user_idle_callback_handler,
                self.speech_gate,
                fallback_stt_ref,
            ) = await build_pipeline(
                self.transport,
                stt_result.service,
                llm,
                tts,
                self.vad_analyzer,
                self.configurations,
                on_user_idle_timeout=self._handle_user_idle_timeout,
                stt_provider=stt_result.provider,
                fallback_stt=stt_result.fallback_service,
            )
            self.fallback_stt = fallback_stt_ref

            # Store callback handler for resetting retry count on user activity
            self._user_idle_callback_handler = user_idle_callback_handler

            # Store context aggregator for user turn event registration
            self._context_aggregator = context_aggregator

            # Generate conversation ID and update context
            lead_payload = self.lead.payload if self.lead else None
            self.conversation_id = generate_conversation_id(lead_payload)
            update_log_context(conversation_id=self.conversation_id)

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
        finally:
            # Safety net: always clear log context when run() exits, regardless of how.
            # This handles crashes, cancellations, and any exit path missed above.
            # Double-clearing (if end_conversation already cleared) is harmless.
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
                # Shouldn't happen, but safe fallback
                self.lead.metaData["call_ended_by"] = "agent"
                logger.warning(f"Unexpected disconnect reason: {reason}")

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
