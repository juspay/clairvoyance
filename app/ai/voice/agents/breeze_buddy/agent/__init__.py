"""Voice agent for handling conversations via Daily or telephony transports."""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, cast

from fastapi import WebSocket
from opentelemetry import trace
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import (
    _create_telephony_transport,
    create_transport,
    parse_telephony_websocket,
)
from pipecat_flows import FlowManager

from app.ai.voice.agents.breeze_buddy.agent.approval import (
    RTVI_APPROVAL_DECISION,
    RTVI_APPROVAL_REQUEST,
    ApprovalManager,
)
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
from app.ai.voice.agents.breeze_buddy.agent.pipeline import (
    build_pipeline,
    create_pipeline_task,
    create_services,
    generate_conversation_id,
)
from app.ai.voice.agents.breeze_buddy.agent.transfer import apply_transfer
from app.ai.voice.agents.breeze_buddy.agent.transport import (
    TRANSPORT_TYPE_DAILY,
    get_transport_params,
)
from app.ai.voice.agents.breeze_buddy.agent.utils import (
    end_call_with_errors,
    send_initial_greeting,
    send_initial_greeting_daily,
)
from app.ai.voice.agents.breeze_buddy.chat.voice_bridge import WidgetVoiceBridge
from app.ai.voice.agents.breeze_buddy.guardrails.config import (
    load_guardrail_config,
)
from app.ai.voice.agents.breeze_buddy.guardrails.evaluator import (
    GuardrailCoordinator,
    GuardrailDecision,
    GuardrailInitializationError,
    GuardrailVerdict,
    build_guardrail_coordinator,
)
from app.ai.voice.agents.breeze_buddy.guardrails.focus import is_focus_enabled
from app.ai.voice.agents.breeze_buddy.guardrails.metrics import (
    GuardrailMetricsDirection,
    GuardrailSessionMetrics,
    resolve_session_metrics,
)
from app.ai.voice.agents.breeze_buddy.guardrails.results import (
    persist_guardrail_metrics,
)
from app.ai.voice.agents.breeze_buddy.guardrails.types import GuardrailsConfig
from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.ivr.selection import (
    BLOCK_MESSAGE_PLAY_SECONDS,
    _send_audio,
    get_template_id_from_call,
    prepare_block_audio,
)
from app.ai.voice.agents.breeze_buddy.ivr.walker import IvrWalker
from app.ai.voice.agents.breeze_buddy.managers.utils import (
    prepare_and_store_initial_greeting,
)
from app.ai.voice.agents.breeze_buddy.mcp import get_mcp_global_functions
from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import (
    create_root_span,
)
from app.ai.voice.agents.breeze_buddy.observers import ObserverManager, build_observers
from app.ai.voice.agents.breeze_buddy.processors import (
    KnowledgeRetrievalProcessor,
    MetricsCollectorProcessor,
    TranscriptCollectorProcessor,
)
from app.ai.voice.agents.breeze_buddy.processors.voice_ui_stream import (
    coerce_ui_action_text,
)
from app.ai.voice.agents.breeze_buddy.services.inbound_policy import (
    get_block_redirect,
)
from app.ai.voice.agents.breeze_buddy.services.knowledge_base import (
    fetch_full_kb_text_cached,
    resolve_kb_runtime,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.context import (
    TemplateContext,
    with_context,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    LEGACY_VOICE_TO_PROVIDER,
    ConfigurationModel,
    FlowMode,
    InterruptionConfig,
    TemplateModel,
    TTSConfig,
    TTSProvider,
)
from app.ai.voice.agents.breeze_buddy.template.vad import create_vad_analyzer
from app.ai.voice.agents.breeze_buddy.utils.agent_transfer import (
    PendingAgentTransfer,
    TransportRebuildContext,
)
from app.ai.voice.agents.breeze_buddy.utils.common import (
    track_error,
)
from app.ai.voice.agents.breeze_buddy.utils.transport.daily_keepalive import (
    force_teardown_daily_client,
    hold_daily_client,
)
from app.ai.voice.agents.breeze_buddy.utils.transport.nonclosing import (
    NonClosingWebSocket,
)
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import (
    close_websocket_safely,
)
from app.ai.voice.agents.breeze_buddy.utils.warm_transfer import set_transfer_flag
from app.ai.voice.llm.realtime.gemini.realtime import has_realtime_llm
from app.core.config.dynamic import BB_DAILY_AUDIO_OUT_10MS_CHUNKS
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
# Cap on a carousel/product-click `ui-action` message injected as a user turn
# (mirrors TTS_SPEAK_MAX_CHARS). See docs/widget/VOICE_AS_CHAT.md (A2).
UI_ACTION_MAX_CHARS = 2000


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
        self.llm_service: Any = None
        self.lead: Optional[LeadCallTracker] = None
        self.root_span: Any = None
        self.flow_manager: Optional[FlowManager] = None
        self.guardrail_coordinator: Optional[GuardrailCoordinator] = None
        self.guardrails = GuardrailsConfig()
        self.guardrail_session_metrics: Dict[str, GuardrailSessionMetrics] = {}
        # Opaque live-context marker -> original blocked caller text. The main
        # LLM sees only the marker; end_conversation restores the audit record.
        self.guardrail_transcript_redactions: Dict[str, str] = {}
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

        # Knowledge base runtime (resolved in run(); all fail-open)
        self.kb_runtime: Any = None  # Optional[ResolvedKbRuntime]
        self._kb_processor: Any = None  # Optional[KnowledgeRetrievalProcessor]
        self._kb_text_task: Optional[asyncio.Task] = None

        # RTVI processor for daily mode real-time events
        self._rtvi_processor: Any = None

        # Stream mode transcript collector (replaces LLMContext for transcription)
        self._transcript_collector: Optional[TranscriptCollectorProcessor] = None

        # Pipecat metrics for the CURRENT generation (build_pipeline replaces it
        # each generation; stays None for IVR mode, which builds no pipeline)
        self.metrics_collector: Optional[MetricsCollectorProcessor] = None

        # Real-time observers (side-LLMs for voicemail/hallucination detection)
        self._observer_manager: Optional[ObserverManager] = None

        # Error tracking
        self.errors: List[Dict[str, Any]] = []

        # Widget voice-as-chat bridge (stream mode + a bound chat_session).
        # Constructed in _register_event_handlers once self.task exists; drives
        # every finished user turn through the chat brain (run_chat_turn) and
        # speaks the assistant prose via TTS. None for telephony / non-widget
        # stream / agent-mode calls. See chat/voice_bridge.py + the
        # widget-voice-as-chat re-architecture plan.
        self._voice_bridge: Optional[WidgetVoiceBridge] = None

        # Reducer-built session state (cart_id/checkout_id/client facts),
        # the voice counterpart of ChatAgent.agent_state. Accumulated via
        # state_reducers during an AGENT-mode call by the global-function
        # wrapper. Empty {} for stream-mode widget voice (the chat brain owns
        # state there) and for telephony calls with no reducers.
        self.agent_state: Dict[str, Any] = {}

        # HITL approval channel — daily mode only, set alongside
        # _rtvi_processor. None on telephony bots (no approval surface) and
        # when RTVI is unavailable; the gate in template/approval.py treats
        # those cases differently (telephony on_no_channel vs daily deny).
        self.approval_manager: Optional[ApprovalManager] = None

        # ── Agent-to-agent transfer (generation loop) ──
        # Set by the connect_to_agent handler; consumed by run()'s loop.
        self.pending_transfer: Optional[PendingAgentTransfer] = None
        self.transfer_count: int = 0
        self.generation: int = 1
        # Transcript snapshots from completed generations, merged at final end.
        self.prior_generation_messages: List[Dict[str, Any]] = []
        # Same for pipecat turn metrics: each generation gets a fresh collector,
        # so the outgoing one is drained into here before it is discarded.
        self.prior_generation_metrics: List[Dict[str, Any]] = []
        # Handoff system messages to seed the NEXT generation's initial node.
        self._handoff_messages: List[Dict[str, str]] = []
        # Capture-once recipe for rebuilding the transport on each generation.
        # (See TransportRebuildContext for why these can't be re-derived.)
        self._rebuild = TransportRebuildContext()
        # Per-generation guard for flow init (see _handle_client_connected).
        self._flow_initialized: bool = False
        # Daily-only: the joined DailyTransportClient preserved across pipeline
        # generations (kept alive by hold_daily_client, which no-ops its
        # leave()/cleanup()), plus the restore() to undo that suppression. None
        # for telephony. See utils/transport/daily_keepalive.py.
        self._daily_client: Any = None
        self._daily_restore: Optional[Callable[[], None]] = None

    # ══════════════════════════════════════════════════════════════════════
    # Properties
    # ══════════════════════════════════════════════════════════════════════

    @property
    def is_daily_mode(self) -> bool:
        return self.transport_type == TRANSPORT_TYPE_DAILY

    @property
    def is_stream_mode(self) -> bool:
        return (
            self.lead is not None
            and self.lead.execution_mode == ExecutionMode.DAILY_STREAM
        )

    # ══════════════════════════════════════════════════════════════════════
    # Real-time events & idle handling
    # ══════════════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════════════
    # Transport setup (daily / telephony)
    # ══════════════════════════════════════════════════════════════════════

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
            if not self.is_stream_mode:
                self.guardrails = await load_guardrail_config(
                    str(self.template.id),
                    self.configurations,
                    supported_channels=list(self.template.supported_channels),
                )
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

        transport_params = get_transport_params(
            self.template,
            self.configurations,
            daily_audio_out_10ms_chunks=await BB_DAILY_AUDIO_OUT_10MS_CHUNKS(),
        )
        self.transport = await create_transport(runner_args, transport_params)

        # Keep-alive: preserve the joined DailyTransportClient across pipeline
        # generations so an agent-transfer rebuild never leaves the room / ejects
        # the browser client (Daily analog of NonClosingWebSocket). Neutralises
        # the client's leave()/cleanup(); the one real teardown happens at true
        # call end in run(). See utils/transport/daily_keepalive.py.
        daily_transport: Any = self.transport
        self._daily_client = daily_transport._client
        self._daily_restore = hold_daily_client(self._daily_client)

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
            self.guardrails = await load_guardrail_config(
                str(self.template.id),
                self.configurations,
                supported_channels=list(self.template.supported_channels),
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

        # Realtime LLMs use server-side turn detection plus the pipeline's
        # UserIdleController. Their user-turn events can arrive after speech
        # begins, so this separate wall-clock timer could expire mid-response
        # and trigger a false idle recovery or reconnect.
        if (
            self.greeting_source
            and self.configurations
            and not has_realtime_llm(self.configurations.llm_configurations)
        ):
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

        # Store for transport rebuilds on agent-to-agent transfer (generation >= 2).
        self._rebuild.telephony_transport_type = transport_type
        self._rebuild.telephony_call_data = call_data
        # Hand pipecat a proxy whose close() is a no-op so its teardown can't drop
        # the call; the Agent owns the real close. self.ws stays raw for
        # pre-pipeline error paths (close_websocket_safely).
        assert self.ws is not None
        self._rebuild.ws_proxy = NonClosingWebSocket(self.ws)

        # Create transport with the call data. Cast: the proxy forwards every
        # attribute so it quacks like a WebSocket, but isn't a subclass.
        self.transport = await _create_telephony_transport(
            cast(WebSocket, self._rebuild.ws_proxy), params, transport_type, call_data
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

    # ══════════════════════════════════════════════════════════════════════
    # Pipeline event handlers
    # ══════════════════════════════════════════════════════════════════════

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
            if self.approval_manager:
                self.approval_manager.deny_all("client_disconnected")
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
            if self.approval_manager:
                self.approval_manager.deny_all("idle_timeout")
            if self._rtvi_processor:
                await self._emit_rtvi_event(
                    "conversation-end", {"reason": "idle_timeout"}
                )
            await self._handle_unexpected_disconnect("idle_timeout")

        # Register RTVI-specific event handlers for daily mode. Client
        # messages are accepted whenever RTVI exists — widget voice runs as
        # daily AGENT mode (is_stream_mode=False), so registering only in
        # stream mode would make approval decisions undeliverable there.
        if self._rtvi_processor:

            @self._rtvi_processor.event_handler("on_client_ready")
            async def on_client_ready(rtvi):
                await rtvi.push_frame(
                    RTVIServerMessageFrame(data={"type": "bot-ready"})
                )
                # Re-emit pending approval requests so a reconnecting client
                # (page refresh within the Daily token TTL) repaints cards
                # instead of losing them until timeout.
                if self.approval_manager:
                    for payload in self.approval_manager.pending_requests():
                        await self._emit_rtvi_event(RTVI_APPROVAL_REQUEST, payload)

            @self._rtvi_processor.event_handler("on_client_message")
            async def on_client_message(rtvi, message):
                # tts-speak remains stream-mode-only (PipecatClient SDK).
                if message.type == "tts-speak" and self.is_stream_mode:
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
                elif message.type == RTVI_APPROVAL_DECISION:
                    data = message.data or {}
                    approval_id = data.get("approval_id")
                    approved = data.get("approved")
                    reason = data.get("reason")
                    if not isinstance(approval_id, str) or not isinstance(
                        approved, bool
                    ):
                        logger.warning(
                            f"[approval] Malformed decision message: {data!r}"
                        )
                        return
                    reason_str = reason if isinstance(reason, str) else None
                    if self._voice_bridge is not None:
                        # Stream-mode widget voice: the chat brain gated the call
                        # (Pattern B). Drive the resume turn through the bridge.
                        await self._voice_bridge.handle_approval_decision(
                            approval_id, approved, reason_str
                        )
                    elif self.approval_manager:
                        # Agent-mode voice (Pattern C): resolve the in-handler gate.
                        self.approval_manager.resolve(approval_id, approved, reason_str)
                    else:
                        logger.warning(
                            "[approval] Decision received but no approval "
                            "channel on this bot"
                        )
                elif message.type == "ui-action":
                    # Widget voice-as-chat: a carousel/product click arrives as
                    # a user turn. The backend emits no transcript echo (the
                    # widget renders the bubble optimistically). See
                    # docs/widget/VOICE_AS_CHAT.md (A2).
                    text = coerce_ui_action_text(message.data, UI_ACTION_MAX_CHARS)
                    if text is None:
                        logger.warning(f"[ui-action] empty/malformed: {message.data!r}")
                        return
                    if self._voice_bridge is not None:
                        # Stream mode: drive the click through the chat brain,
                        # exactly like a spoken turn.
                        logger.debug(f"[ui-action] bridge user turn: {text[:80]}")
                        await self._voice_bridge.handle_user_turn(text)
                    elif self.task:
                        # Agent mode: inject into the live LLM context
                        # (run_llm=True) — the same mid-call user-turn injection
                        # user_idle.py uses; pipecat handles barge-in natively.
                        logger.debug(f"[ui-action] inject user turn: {text[:80]}")
                        await self.task.queue_frame(
                            LLMMessagesAppendFrame(
                                [{"role": "user", "content": text}], run_llm=True
                            )
                        )

        # Register pipeline event handlers
        if self._context_aggregator:
            user_agg = self._context_aggregator.user()

            @user_agg.event_handler("on_user_turn_started")
            async def on_user_turn_started(aggregator, strategy):
                """Reset idle retry counter."""
                if self._user_idle_callback_handler:
                    if not self._user_spoke:
                        self._user_spoke = True
                        if self._post_greeting_task:
                            self._post_greeting_task.cancel()
                            self._post_greeting_task = None
                            logger.debug("Post-greeting timer cancelled - user spoke")
                    self._user_idle_callback_handler.reset_retry_count()

            # Subscribe observer manager to pipeline events
            if self._observer_manager:
                self._observer_manager.subscribe_to_pipeline(
                    self._context_aggregator, self.flow_manager
                )

        # Widget voice-as-chat bridge: stream mode + a bound chat_session.
        # Drive every finished user turn through the chat brain (run_chat_turn)
        # — the bridge speaks the assistant prose via TTS and emits RTVI
        # transcript / ui-op / turn-end events. on_user_turn_started cancels an
        # in-flight turn on barge-in (pipecat's broadcast_interruption already
        # flushed queued TTS natively). No idle handler is wired in stream mode,
        # so these registrations don't collide with the block above.
        widget_session_id = (
            (self.lead.metaData or {}).get("widget_session_id") if self.lead else None
        )
        if self.is_stream_mode and widget_session_id and self._context_aggregator:
            self._voice_bridge = WidgetVoiceBridge(
                session_id=str(widget_session_id),
                task=self.task,
                emit_rtvi=self._emit_rtvi_event,
            )
            bridge = self._voice_bridge
            bridge_user_aggregator = self._context_aggregator.user()

            @bridge_user_aggregator.event_handler("on_user_turn_stopped")
            async def _bridge_user_turn_stopped(aggregator, strategy, message):
                await bridge.handle_user_turn(getattr(message, "content", None))

            @bridge_user_aggregator.event_handler("on_user_turn_started")
            async def _bridge_user_turn_started(aggregator, strategy):
                await bridge.cancel_inflight()

            logger.info(
                "[STREAM] Widget voice-as-chat bridge wired for chat_session "
                f"{widget_session_id}"
            )

    # ══════════════════════════════════════════════════════════════════════
    # Client connected / flow initialization
    # ══════════════════════════════════════════════════════════════════════

    async def _drive_client_connected_after_start(self, task: Any) -> None:
        """Daily transfer (gen>1): re-establish audio capture + flow-init once ready.

        The reused, already-joined DailyTransportClient never re-fires
        _on_participant_joined for the rebuilt transport, so the new input
        transport never captures the existing participant's mic (dead STT) and
        on_client_connected (flow-init / the new agent's first turn) never fires.
        Wait for the StartFrame to reach the pipeline end
        (PipelineTask._pipeline_start_event) so queued frames aren't dropped, then
        replay pipecat's participant-joined handling for each participant already
        in the room — one call restores BOTH capture_participant_audio (at the
        correct in_sample_rate) and on_client_connected -> _handle_client_connected,
        exactly as a fresh gen-1 join. Idempotent via _flow_initialized.
        """
        start_event = getattr(task, "_pipeline_start_event", None)
        if start_event is not None:
            await start_event.wait()

        transport = self.transport
        on_join = getattr(transport, "_on_participant_joined", None)
        participants_fn = getattr(transport, "participants", None)
        replayed = False
        if on_join is not None and participants_fn is not None:
            for pid, pdata in (participants_fn() or {}).items():
                if pid == "local" or not isinstance(pdata, dict) or not pdata.get("id"):
                    continue
                try:
                    await on_join(pdata)
                    replayed = True
                except Exception as exc:
                    logger.warning(
                        f"[daily transfer] replay participant-joined failed "
                        f"for {pid}: {exc}"
                    )
        if not replayed:
            # No remote participant found — still drive flow-init directly.
            await self._handle_client_connected()

    async def _handle_client_connected(self) -> None:
        """Handle client connection and initialize flow."""
        if self.is_stream_mode:
            if self.lead and self.lead.metaData is None:
                self.lead.metaData = {}
            logger.info("[STREAM] Client connected — ready for STT/TTS")
            # Widget voice-as-chat: on the FIRST attachment (no prior user
            # turns) speak the persisted chat greeting via TTS. A reconnect
            # mid-conversation does not re-greet. The bridge gates on the chat
            # history and pushes audio only (no transcript echo — the widget
            # already shows the greeting bubble from its session load).
            if self._voice_bridge is not None:
                await self._voice_bridge.maybe_speak_greeting()
            return

        # Per-generation guard: on a transfer rebuild a fresh transport fires
        # on_client_connected again (and Daily may re-deliver participant
        # events); initialize the flow exactly once per generation.
        if self._flow_initialized:
            return
        self._flow_initialized = True

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
        # (Stream mode returned above — its greeting is spoken by the voice
        # bridge, not injected into an LLM context.)
        if self.is_daily_mode and not self.is_stream_mode and self.task:
            greeting_result = await send_initial_greeting_daily(
                task=self.task,
                lead=self.lead,
                template=self.template,
                errors=self.errors,
            )
            self.greeting_source = greeting_result.source
            self.greeting_text = greeting_result.text

            # Gemini Live + played greeting (Daily variant): the service was
            # built before the client connected, so apply the no-initial-
            # inference override now — must land before flow init below.
            self._suppress_realtime_initial_inference()

            # Mirror the telephony fallback for non-realtime pipelines.
            # Realtime LLMs rely on server-side turn detection and the
            # UserIdleController to avoid the timer race described above.
            if (
                self.greeting_source
                and self.configurations
                and not has_realtime_llm(self.configurations.llm_configurations)
            ):
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

        # Full-injection KB text: the fetch started at boot (run()); give it a
        # short grace window then fail open — the call proceeds without KB
        # context rather than delaying the greeting (mirrors greeting prep).
        kb_text: Optional[str] = None
        if self._kb_text_task is not None:
            try:
                kb_text = await asyncio.wait_for(
                    asyncio.shield(self._kb_text_task), timeout=2.0
                )
            except Exception as e:
                logger.warning(f"KB full-injection text unavailable at connect: {e}")

        initial_node_config = prepare_initial_node(
            flow_config=self.flow_config,
            lead_payload=lead_payload,
            configurations=self.configurations,
            has_greeting_source=bool(self.greeting_source),
            greeting_text=self.greeting_text,
            kb_text=kb_text,
            focus_enabled=is_focus_enabled(self.guardrails),
        )

        # Agent-to-agent transfer: seed the incoming generation's initial node
        # with the handoff note (and, in "full" mode, the prior transcript) so
        # the new agent knows it was transferred in. Cleared after use.
        if self._handoff_messages:
            initial_node_config["task_messages"] = list(self._handoff_messages) + list(
                initial_node_config.get("task_messages", [])
            )
            self._handoff_messages = []

        # Initialize node traversal tracking. Only reset on the first generation;
        # a transfer rebuild (generation >= 2) must PRESERVE prior generations'
        # nodes (the first template's node + its connect_to_agent call).
        if self.lead.metaData is None:
            self.lead.metaData = {}
        if self.generation == 1:
            self.lead.metaData["node_traversal"] = []
        else:
            self.lead.metaData.setdefault("node_traversal", [])

        # Record initial-node entry BEFORE flow_manager.initialize so that any
        # global function called during the first LLM turn (e.g. get_driver_info
        # on the initial node) finds an active node entry to record against.
        initial_node_name = self.flow_config["initial_node"]
        context = TemplateContext(self)
        context.record_node_entry(initial_node_name)

        # With Focus enabled, FlowManager's asynchronous context update leaves
        # a narrow window where an early greeting interruption could overtake
        # the policy. Seed the exact same initial messages directly first. The
        # disabled path intentionally retains the original FlowManager-only
        # initialization behavior.
        focus_enabled = is_focus_enabled(self.guardrails)
        if self.context is not None and focus_enabled:
            initial_messages = cast(
                list[LLMContextMessage],
                list(initial_node_config.get("role_messages", [])),
            )
            initial_messages.extend(
                cast(
                    list[LLMContextMessage],
                    list(initial_node_config.get("task_messages", [])),
                )
            )
            self.context.set_messages(initial_messages)
            logger.info(
                "Installed initial LLM context directly before FlowManager "
                f"initialization: messages={len(initial_messages)}"
            )

        await self.flow_manager.initialize(initial_node_config)
        logger.info(f"FlowManager initialized at node: {initial_node_name}")

    # ══════════════════════════════════════════════════════════════════════
    # Run loop & pipeline generation
    # ══════════════════════════════════════════════════════════════════════

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

    def _suppress_realtime_initial_inference(self) -> None:
        """Gemini Live + played greeting: don't generate on context init.

        flow.py always sends the flow-init LLMRunFrame for realtime LLMs
        (without it, pipecat 1.1.0's Gemini Live input gate never opens and
        the call goes deaf after the greeting). This companion override makes
        the context seed non-generating (``turn_complete=False``) so the
        model doesn't speak over the pre-played greeting — the greeting sits
        in history as an assistant message and the next user utterance gets
        a single history-aware response.

        Only Gemini Live has ``_inference_on_context_initialization``; other
        providers/services keep their default. No-op when no greeting played
        (LLM-speaks-first remains the trigger). Idempotent, and read by the
        service only when the first context frame arrives — so any call
        before flow init is in time.
        """
        if not self.greeting_source:
            return
        if not self.llm_service or not hasattr(
            self.llm_service, "_inference_on_context_initialization"
        ):
            return
        self.llm_service._inference_on_context_initialization = False
        logger.info(
            "Realtime initial inference suppressed (greeting was played): "
            "context will seed history without generating a response"
        )

    async def run(self, runner_args: Optional[RunnerArguments] = None) -> None:
        """Main entry point for running the agent.

        Args:
            runner_args: Required for Daily mode, contains room info and lead data
        """
        try:
            self._rebuild.runner_args = runner_args
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

            # ── IVR mode: pure DTMF state machine (no STT/LLM/pipeline) ─────────
            # Telephony-only. Runs the menu tree directly over the websocket and
            # returns before any pipeline is built — self.task/self.context stay
            # None, which the reused end_conversation finaliser already handles.
            if (
                not self.is_daily_mode
                and self.template
                and self.template.flow.get("mode") == FlowMode.IVR.value
            ):
                logger.info(
                    f"[IVR] flow.mode=ivr -> running DTMF walker for "
                    f"call {self.call_sid}"
                )
                await IvrWalker(self).run()
                return

            # One connection, N pipeline generations. Each generation is the
            # same cold-start build path; a connect_to_agent transfer ends the
            # current pipeline task (without hanging up) and loops back here.
            while True:
                await self._run_generation()
                if not self.pending_transfer or self.conversation_ended:
                    break
                transfer = self.pending_transfer
                self.pending_transfer = None
                await apply_transfer(self, transfer)

            # The Agent owns the ONE real teardown at true call end — per-generation
            # teardown is suppressed so transfers never drop the connection.
            # Telephony: close the raw ws (NonClosingWebSocket swallowed pipecat's
            # close). Daily: force the room leave + client release that
            # hold_daily_client neutralised.
            if not self.is_daily_mode and self.ws:
                await close_websocket_safely(
                    self.ws, code=1000, reason="Conversation ended"
                )
            elif self.is_daily_mode and self._daily_client is not None:
                await force_teardown_daily_client(self._daily_client)
        finally:
            clear_log_context()

    async def _run_generation(self) -> None:
        """Build and run one pipeline generation via the cold-start path.

        Blocks until the pipeline reaches a terminal frame — real call end, or a
        connect_to_agent transfer that queued an EndFrame via stop_when_done().
        run()'s loop then either finalizes (real end) or applies the transfer and
        re-invokes this method for the next generation.
        """
        # Stream mode skips LLM creation and runs build_pipeline with
        # mode="stream" (no LLM processor, no assistant aggregator, transcript
        # collector inserted, no user idle). All other wiring is identical.
        is_stream = self.is_stream_mode
        stt, llm, tts = await create_services(
            self.configurations, include_llm=not is_stream
        )
        if not is_stream:
            assert llm is not None, "LLM is required in agent mode"

        # Expose the LLM service for runtime overrides, and apply the
        # greeting one now — the telephony greeting was already sent during
        # transport setup, before this generation was built.
        self.llm_service = llm
        self._suppress_realtime_initial_inference()

        # Knowledge base runtime resolution (fail-open). Stream mode goes
        # through the chat brain, which has its own KB hooks; realtime LLMs
        # have no pre-LLM text hop (auto/auto_retrieve is rejected for them at
        # template save by ConfigurationModel's validator and re-checked at
        # call load by validate_template_compat). Re-resolved every generation:
        # an agent-transfer target may have a different KB (or none), so clear
        # the prior generation's processor + fetch task before resolving.
        self._kb_processor = None
        self._kb_text_task = None
        is_realtime_llm = stt is None and tts is None and llm is not None
        if not await self._initialize_guardrails(
            is_stream=is_stream,
            is_realtime_llm=is_realtime_llm,
        ):
            return
        if not is_stream:
            self.kb_runtime = await resolve_kb_runtime(self.configurations)
        if self.kb_runtime:
            if self.kb_runtime.mode == "auto_retrieve" and not is_realtime_llm:
                self._kb_processor = KnowledgeRetrievalProcessor(self.kb_runtime.config)
                logger.info("KB auto_retrieve enabled for this call")
            elif self.kb_runtime.mode == "full_injection":
                # Fetch concurrently with pipeline build / greeting prep;
                # awaited (with a short shield) in _handle_client_connected.
                self._kb_text_task = asyncio.create_task(
                    fetch_full_kb_text_cached(self.kb_runtime.config)
                )
                logger.info("KB full_injection enabled for this call")

        (
            pipeline,
            context,
            context_aggregator,
            user_idle_callback_handler,
            self.speech_gate,
            self._transcript_collector,
            self.metrics_collector,
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
            kb_processor=self._kb_processor,
            guardrail_coordinator=self.guardrail_coordinator,
            focus_enabled=is_focus_enabled(self.guardrails),
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
            pipeline,
            self.conversation_id,
            is_daily_mode=self.is_daily_mode,
        )

        if self.is_daily_mode and hasattr(self.task, "rtvi") and self.task.rtvi:
            self._rtvi_processor = self.task.rtvi
            # HITL approval channel (Pattern C — the in-handler gate that
            # blocks a voice global-function on a live RTVI card). This is
            # AGENT-mode only: stream-mode widget voice has no FlowManager /
            # global-function wrapper to ever reach the gate, and it gates
            # approvals through the chat brain instead (Pattern B). So only
            # non-widget daily agent-mode calls get an ApprovalManager.
            # RTVI requires ENABLE_BREEZE_BUDDY_DAILY_EVENTS=true; without it
            # approval_manager stays None and gated calls are denied.
            if not is_stream:
                self.approval_manager = ApprovalManager(emit=self._emit_rtvi_event)
                if self._user_idle_callback_handler:
                    # While an approval card is showing, the user is silently
                    # reading — idle prompts/end-call must not fire (idle
                    # re-inference can also spawn duplicate gated calls).
                    approval_manager = self.approval_manager
                    self._user_idle_callback_handler.suppress_when = (
                        approval_manager.has_pending
                    )

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
                        # Thread the bot so a gated MCP tool can reach the
                        # ApprovalManager and block in-process (Pattern C).
                        bot_instance=self,
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

        # ── Real-time observers ──────────────────────────────────
        observers_config = (
            self.configurations.observers if self.configurations else None
        )
        logger.info(
            f"Observer setup: "
            f"observers_count={len(observers_config) if observers_config else 0}, "
            f"is_stream={is_stream}"
        )
        if observers_config and not is_stream:
            try:
                observer_instances = await build_observers(
                    configs=observers_config,
                    template=self.template,
                    agent_context=self,
                    handler_map=self.flow_builder.handler_map,
                )
                if observer_instances:
                    self._observer_manager = ObserverManager(
                        observer_instances, context
                    )
                    logger.info(
                        f"Initialized {len(observer_instances)} "
                        f"real-time observer(s)"
                    )
            except Exception as e:
                logger.error(f"Failed to initialize observers: {e}")
                self._observer_manager = None

        self._register_event_handlers()

        # Daily transfer (gen>1): the reused, already-joined DailyTransportClient
        # never re-fires on_client_connected (its normal trigger), so drive
        # flow-init explicitly once the rebuilt pipeline is ready. gen-1 and
        # telephony still go through the on_client_connected event.
        if self.is_daily_mode and self.generation > 1:
            asyncio.create_task(self._drive_client_connected_after_start(self.task))

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
            # Per-generation observer teardown (each generation builds its own).
            if self._observer_manager:
                await self._observer_manager.stop()
                self._observer_manager = None
            self.guardrail_coordinator = None

    async def _initialize_guardrails(
        self, *, is_stream: bool, is_realtime_llm: bool
    ) -> bool:
        """Initialize custom guardrails, ending the call safely on failure."""
        self.guardrail_coordinator = None
        guardrails = self.guardrails
        if is_stream or guardrails is None:
            return True

        metrics: Optional[GuardrailSessionMetrics] = None
        metrics_key: Optional[str] = None
        config_id = guardrails.evaluation_config_id
        if config_id is not None and self.template is not None:
            revision = guardrails.configuration_revision or f"legacy:{config_id}"
            metrics_key = f"{config_id}:{revision}"
            metrics = resolve_session_metrics(
                guardrails,
                template_id=str(self.template.id),
                channel="VOICE",
                existing=self.guardrail_session_metrics.get(metrics_key),
            )
            if metrics is not None:
                self.guardrail_session_metrics[metrics_key] = metrics

        if is_realtime_llm or not guardrails.has_enabled_custom_guardrails():
            return True

        try:
            self.guardrail_coordinator = await build_guardrail_coordinator(
                guardrails,
                transcript_redactions=self.guardrail_transcript_redactions,
                metrics=metrics,
                initial_turn_number=(metrics.last_turn_number if metrics else 0),
            )
            return True
        except GuardrailInitializationError as exc:
            error_msg = str(exc)
            logger.error(error_msg, exc_info=True)
            track_error(self.errors, error_msg)
            self.conversation_ended = True
            if metrics is not None:
                direction = cast(
                    GuardrailMetricsDirection,
                    (
                        "input"
                        if guardrails.input is not None and guardrails.input.enabled
                        else "output"
                    ),
                )
                metrics.record(
                    direction,
                    GuardrailVerdict(
                        GuardrailDecision.BLOCK,
                        reason="guardrail evaluation unavailable",
                        evaluation_failed=True,
                    ),
                    metrics.last_turn_number + 1,
                )
                if self.lead is not None:
                    started_at = self.lead.call_initiated_time or self.lead.created_at
                    if started_at is not None:
                        await persist_guardrail_metrics(
                            metrics,
                            source_id=str(self.lead.id),
                            reseller_id=self.lead.reseller_id,
                            merchant_id=self.lead.merchant_id,
                            started_at=started_at,
                        )
                        if metrics_key is not None:
                            self.guardrail_session_metrics.pop(metrics_key, None)
            if self.completion_function and self.lead:
                await end_call_with_errors(
                    lead=self.lead,
                    errors=self.errors,
                    completion_function=self.completion_function,
                    transport_type=self.transport_type,
                    call_sid=self.call_sid,
                )
            return False

    # ══════════════════════════════════════════════════════════════════════
    # Cleanup
    # ══════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════
# Module entry points (telephony / daily)
# ══════════════════════════════════════════════════════════════════════════


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
