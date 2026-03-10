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
from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import (
    create_root_span,
)
from app.ai.voice.agents.breeze_buddy.services.inbound_policy import (
    evaluate_inbound_policy,
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
from app.core.config.static import APP_BASE_URL, ENABLE_BREEZE_BUDDY_TRACING
from app.core.logger import logger
from app.core.logger.context import (
    clear_log_context,
    set_log_context,
    update_log_context,
)
from app.database.accessor import (
    get_outbound_number_by_id,
    update_lead_call_initiated_time,
)
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_initiated_time_by_id,
)
from app.database.accessor.breeze_buddy.template import (
    get_template_by_id,
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

        self.lead = await update_lead_call_initiated_time(
            self.call_sid, call_initiated_time
        )
        if not self.lead:
            # Inbound call - extract template_id (handles IVR mode if enabled)
            template_id_from_query, error_reason, was_ivr = (
                await get_template_id_from_call(
                    ws=self.ws,
                    stream_sid=self.stream_sid,
                    call_sid=self.call_sid,
                    call_data=call_data,
                    provider=self.provider or "",
                )
            )

            # Check if there was an error (IVR failed or invalid template_id)
            if error_reason:
                # WebSocket already closed by get_template_id_from_call
                clear_log_context()
                return False

            # Extract URL query params for Plivo inbound (contains from_number, to_number)
            url_query_params = dict(self.ws.query_params) if self.ws else {}

            # Deferred rate limit check for IVR mode.
            # In IVR mode, rate limiting was skipped in the answer handler because the
            # caller hadn't selected a template yet. Now that we know the template,
            # evaluate the inbound call policy before creating a lead.
            if was_ivr and template_id_from_query:
                blocked = await self._evaluate_deferred_rate_limit(
                    template_id=template_id_from_query,
                    call_data=call_data,
                    url_query_params=url_query_params,
                )
                if blocked:
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

    async def _evaluate_deferred_rate_limit(
        self,
        template_id: str,
        call_data: Dict[str, Any],
        url_query_params: Dict[str, str],
    ) -> bool:
        """Evaluate rate limit for IVR calls after template selection.

        In IVR mode, rate limiting is deferred from the answer handler because
        the caller hasn't selected a template yet. This method runs the deferred
        check once the template is known.

        If the policy action is ``redirect`` and a redirect number is configured,
        the call is transferred to that number using the same mechanism as warm
        transfer (Redis flag + provider conference service). Otherwise, a block
        message is played and the WebSocket is closed.

        Args:
            template_id: The selected template ID from IVR.
            call_data: Call data from the telephony provider.
            url_query_params: URL query params (for Plivo inbound).

        Returns:
            True if the call was blocked/redirected (caller should not proceed),
            False if the call is allowed.
        """
        try:
            template = await get_template_by_id(template_id)
            if not template:
                logger.warning(
                    f"[IVR-RateLimit] Template {template_id} not found, allowing call"
                )
                return False

            policy = (
                template.configurations.inbound_call_policy
                if template.configurations
                else None
            )
            if not policy:
                return False

            merchant_id = template.reseller_id or ""
            transfer_number = (
                template.configurations.transfer_number
                if template.configurations
                else None
            )

            # Extract caller number from call_data
            start_data = call_data.get("start", {})
            custom_params = call_data.get("custom_parameters") or start_data.get(
                "custom_parameters", {}
            )
            from_number = (
                start_data.get("from")
                or call_data.get("from")
                or custom_params.get("from_number")
                or url_query_params.get("from_number", "unknown")
            )

            policy_result = await evaluate_inbound_policy(
                policy=policy,
                merchant_id=merchant_id,
                template_id=template_id,
                caller_number=from_number,
                transfer_number=transfer_number,
                skip_rate_limit=False,
            )

            if not policy_result.allowed:
                logger.info(
                    f"[IVR-RateLimit] Call blocked after IVR selection: "
                    f"reason={policy_result.reason}, action={policy_result.action}, "
                    f"call_sid={self.call_sid}"
                )

                # Attempt call transfer for redirect actions (same as warm transfer)
                if policy_result.action == "redirect" and policy_result.redirect_number:
                    transferred = await self._transfer_rate_limited_call(
                        template=template,
                        redirect_number=policy_result.redirect_number,
                        from_number=from_number,
                        block_message=policy_result.block_message,
                    )
                    if transferred:
                        return True
                    # Fall through to block if transfer failed

                # Block: play message and close WebSocket
                block_msg = (
                    policy_result.block_message
                    or "We are unable to take your call right now. Goodbye."
                )
                await self._play_message_and_close(block_msg, policy_result.reason)
                return True

            return False

        except Exception as e:
            # Fail-open: if rate limit check fails, allow the call through
            logger.error(
                f"[IVR-RateLimit] Deferred rate limit check failed (allowing call): {e}",
                exc_info=True,
            )
            return False

    async def _transfer_rate_limited_call(
        self,
        template: TemplateModel,
        redirect_number: str,
        from_number: str,
        block_message: Optional[str] = None,
    ) -> bool:
        """Transfer a rate-limited call using the warm transfer mechanism.

        Sets a Redis transfer flag and delegates to the provider's conference
        service, identical to how ``connect_to_live_agent`` works.

        Args:
            template: The selected template (for outbound_number_id, reseller_id).
            redirect_number: The phone number to redirect the call to.
            from_number: The caller's phone number.
            block_message: Optional message to play before transfer (not used
                currently — provider handles bridging immediately).

        Returns:
            True if the transfer was initiated successfully, False otherwise.
        """
        if not self.telephony_service or not hasattr(
            self.telephony_service, "conference_service"
        ):
            logger.warning(
                f"[IVR-RateLimit] No telephony/conference service available for "
                f"redirect on call {self.call_sid}, falling back to block"
            )
            return False

        if not self.call_sid:
            logger.warning("[IVR-RateLimit] No call_sid available, cannot redirect")
            return False

        if not template.outbound_number_id:
            logger.warning(
                f"[IVR-RateLimit] No outbound_number_id on template {template.id}, "
                f"cannot redirect call {self.call_sid}"
            )
            return False

        outbound_number_record = await get_outbound_number_by_id(
            template.outbound_number_id
        )
        if not outbound_number_record:
            logger.warning(
                f"[IVR-RateLimit] Outbound number not found for "
                f"id={template.outbound_number_id}, cannot redirect"
            )
            return False

        outbound_number = outbound_number_record.number
        conference_name = f"ratelimit-redirect-{self.call_sid}"

        logger.info(
            f"[IVR-RateLimit] Redirecting call {self.call_sid} to {redirect_number} "
            f"via {conference_name}"
        )

        # Set Redis transfer flag (same pattern as warm transfer)
        await set_transfer_flag(
            call_sid=self.call_sid,
            reseller_id=template.reseller_id or "",
            merchant_identifier=template.merchant_identifier or "",
            transfer_number=redirect_number,
            customer_phone_number=from_number,
        )

        # Build status callback URL for conference events
        provider_name = (self.provider or "").lower()
        status_callback_url = (
            f"{APP_BASE_URL}/agent/voice/breeze-buddy/"
            f"{provider_name}/callback/transfer/conference-end"
        )

        try:
            conference_result = (
                await self.telephony_service.conference_service.handle_transfer(
                    conference_name=conference_name,
                    agent_phone_number=redirect_number,
                    customer_call_sid=self.call_sid,
                    outbound_number=outbound_number,
                    callback=None,
                    status_callback_url=status_callback_url,
                    customer_phone_number=from_number,
                )
            )

            if conference_result.get("success"):
                logger.info(
                    f"[IVR-RateLimit] Transfer initiated successfully for "
                    f"call {self.call_sid} → {redirect_number}"
                )
                # Close WebSocket — provider now handles bridging
                if self.ws:
                    await close_websocket_safely(
                        self.ws, code=1000, reason="Rate limit redirect"
                    )
                return True
            else:
                logger.warning(
                    f"[IVR-RateLimit] Transfer failed: "
                    f"{conference_result.get('reason')}, falling back to block"
                )
                return False

        except Exception as e:
            logger.error(
                f"[IVR-RateLimit] Transfer exception for call {self.call_sid}: {e}",
                exc_info=True,
            )
            return False

    async def _play_message_and_close(
        self, message: str, reason: Optional[str] = None
    ) -> None:
        """Play a TTS message over the WebSocket and close the connection."""
        from app.ai.voice.agents.breeze_buddy.agent.ivr import (
            _convert_audio_for_provider,
            _generate_tts_audio_mulaw,
            _send_audio,
        )

        provider = self.provider or ""
        audio = await _generate_tts_audio_mulaw(message)
        if audio and self.ws and self.stream_sid:
            provider_audio = _convert_audio_for_provider(audio, provider)
            await _send_audio(self.ws, self.stream_sid, provider_audio, provider)
            # Wait for audio to finish playing before closing
            await asyncio.sleep(4)

        if self.ws:
            await close_websocket_safely(
                self.ws,
                code=4000,
                reason=f"Call blocked: {reason or 'rate_limited'}",
            )

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
            stt, llm, tts = await create_services(self.configurations)
            (
                pipeline,
                self.context,
                context_aggregator,
                user_idle_callback_handler,
                self.speech_gate,
            ) = await build_pipeline(
                self.transport,
                stt,
                llm,
                tts,
                self.vad_analyzer,
                self.configurations,
                on_user_idle_timeout=self._handle_user_idle_timeout,
            )

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
