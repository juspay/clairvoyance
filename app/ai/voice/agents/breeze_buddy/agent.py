import asyncio
import audioop
import base64
import json
from datetime import datetime, timezone

from fastapi import WebSocket
from opentelemetry import trace
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.observers.loggers.llm_log_observer import LLMLogObserver
from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
from pipecat.observers.loggers.transcription_log_observer import (
    TranscriptionLogObserver,
)
from pipecat.observers.loggers.user_bot_latency_log_observer import (
    UserBotLatencyLogObserver,
)
from pipecat.observers.turn_tracking_observer import TurnTrackingObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_response import LLMUserAggregatorParams
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.azure.llm import AzureLLMService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat_flows import FlowManager, NodeConfig
from pydub import AudioSegment

from app.ai.voice.agents.breeze_buddy.analytics.tracing_setup import (
    setup_tracing,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.stt import get_stt_service
from app.ai.voice.agents.breeze_buddy.template import (
    FlowConfigBuilder,
    TemplateContext,
    with_context,
)
from app.ai.voice.agents.breeze_buddy.template.loader import FlowConfigLoader
from app.ai.voice.agents.breeze_buddy.tts import get_tts_service
from app.ai.voice.agents.breeze_buddy.utils.common import create_background_sound_mixer
from app.ai.voice.agents.breeze_buddy.utils.language_utils.prompt_injections import (
    inject_language_rules,
)
from app.core.config.dynamic import (
    BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS,
    BREEZE_BUDDY_AZURE_TEMPERATURE,
    BREEZE_BUDDY_LLM_AGGREGATION_TIMEOUT,
)
from app.core.config.static import (
    AZURE_BREEZE_BUDDY_OPENAI_MODEL,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    BREEZE_BUDDY_VAD_CONFIDENCE,
    BREEZE_BUDDY_VAD_MIN_VOLUME,
    BREEZE_BUDDY_VAD_START_SECS,
    BREEZE_BUDDY_VAD_STOP_SECS,
    ENABLE_BREEZE_BUDDY_TRACING,
    ENABLE_BREEZE_BUDDY_USER_INTERRUPTION,
    ENVIRONMENT,
)
from app.core.logger import logger
from app.database.accessor import get_lead_by_call_id, update_lead_call_initiated_time
from app.database.accessor.breeze_buddy.template import get_template_by_merchant
from app.schemas import CallProvider


class Agent:
    def __init__(
        self,
        ws: WebSocket,
        aiohttp_session,
        serializer,
        hangup_function,
        completion_function,
        provider: str,
    ):
        self.ws = ws
        self.aiohttp_session = aiohttp_session
        self.provider = provider
        self.task: PipelineTask = None
        self.context: OpenAILLMContext = None
        self.conversation_ended = False
        self.reporting_webhook_url = None
        self.call_sid = None
        self.serializer = serializer
        self.hangup_function = hangup_function
        self.completion_function = completion_function
        self.vad_analyzer = None
        self.transport = None
        self.lead = None
        self.root_span = (
            None  # Store OpenTelemetry span reference for updating with evaluation data
        )

        # Dynamic template components (initialized later)
        self.flow_loader = None
        self.flow_builder = None
        self.template_config = None
        self.system_prompt = None
        self.flow_config = None
        self.end_conversation_callbacks = []
        self.expected_callback_response_schema = None

        # Language config (initialized during run)
        self.language_name = "English"
        self.language_prompt_enabled = (
            False  # Whether to inject language instruction into prompts
        )

    async def run(self):
        logger.info("Starting WebSocket bot")
        await self.ws.accept()
        call_initiated_time = datetime.now(timezone.utc)

        try:
            start_data = self.ws.iter_text()
            await start_data.__anext__()
            call_data_str = await start_data.__anext__()
            call_data = json.loads(call_data_str)
            logger.info(f"Received call data: {call_data}")
        except StopAsyncIteration:
            logger.warning("WebSocket connection closed before receiving call data")
            return
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse call data JSON: {e}")
            try:
                if self.ws.client_state.name != "DISCONNECTED":
                    await self.ws.close(code=4000, reason="Invalid JSON data")
            except Exception as close_error:
                logger.warning(
                    f"Could not close websocket (likely already closed): {close_error}"
                )
            return

        if self.provider == CallProvider.TWILIO:
            stream_sid = call_data["start"]["streamSid"]
            self.call_sid = call_data["start"]["callSid"]

            try:
                logger.info("Preparing to send initial audio message.")
                wav_file_path = (
                    "app/ai/voice/agents/breeze_buddy/static/audio/dial-tone.wav"
                )

                # Load and convert audio
                audio = AudioSegment.from_wav(wav_file_path)
                audio = audio.set_frame_rate(8000).set_channels(1).set_sample_width(2)
                pcm_data = audio.raw_data
                mulaw_data = audioop.lin2ulaw(pcm_data, 2)
                payload = base64.b64encode(mulaw_data).decode("utf-8")

                # Create and send media message
                media_message = {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": payload},
                }
                await self.ws.send_text(json.dumps(media_message))
                logger.info(
                    f"Successfully sent initial media message for streamSid: {stream_sid}"
                )

            except Exception as e:
                logger.error(f"Failed to send initial media message: {e}")

        else:  # Exotel
            stream_sid = call_data.get("stream_sid")
            start_data = call_data.get("start", {})
            self.call_sid = start_data.get("call_sid")

        await update_lead_call_initiated_time(self.call_sid, call_initiated_time)
        lead = await get_lead_by_call_id(self.call_sid)
        if not lead:
            logger.error(f"Could not find lead for call_sid: {self.call_sid}")
            return

        self.lead = lead
        call_payload = lead.payload
        self.reporting_webhook_url = call_payload.get("reporting_webhook_url")

        logger.info(
            f"Connected to call: CallSid={self.call_sid}, StreamSid={stream_sid}"
        )
        logger.info(f"Call payload: {call_payload}")

        self.flow_loader = FlowConfigLoader()
        self.flow_builder = FlowConfigBuilder()

        # Apply context to all handlers before passing them to the builder
        for handler_name, handler_func in self.flow_builder.handler_map.items():
            self.flow_builder.handler_map[handler_name] = with_context(self)(
                handler_func
            )

        # Load template configuration from database
        merchant_id = self.lead.merchant_id if self.lead else "default"

        # First, load the template to get the expected_payload_schema

        template = await get_template_by_merchant(
            merchant_id=merchant_id,
            shop_identifier=self.lead.shop_identifier if self.lead else None,
            name=self.lead.template,
        )

        if not template:
            logger.error(
                f"Template not found for merchant={merchant_id}, template={self.lead.template}"
            )
            try:
                if self.ws.client_state.name != "DISCONNECTED":
                    await self.ws.close(code=4000, reason="Template not found")
            except Exception as close_error:
                logger.warning(
                    f"Could not close websocket (likely already closed): {close_error}"
                )
            return

        if not template.expected_payload_schema:
            logger.error(
                f"Template {self.lead.template} has no expected_payload_schema defined"
            )
            try:
                if self.ws.client_state.name != "DISCONNECTED":
                    await self.ws.close(code=4000, reason="Template schema not defined")
            except Exception as close_error:
                logger.warning(
                    f"Could not close websocket (likely already closed): {close_error}"
                )
            return

        # Build template_vars dynamically based on expected_payload_schema
        self.template_vars = {}
        for field_name in template.expected_payload_schema.keys():
            if field_name in call_payload:
                self.template_vars[field_name] = call_payload[field_name]
            else:
                logger.warning(f"Field '{field_name}' from schema not found in payload")
                self.template_vars[field_name] = ""

        # Add language_name to template_vars for prompt customization
        # Language name is pre-computed in handlers.py and sent directly in payload
        self.language_name = call_payload.get("language_name", "English")
        logger.info(f"Using language from payload: {self.language_name}")
        self.template_vars["primary_language"] = self.language_name

        logger.info(
            f"Dynamically built template_vars from schema: {list(self.template_vars.keys())}"
        )

        self.template_config = await self.flow_loader.load_template(
            merchant_id=merchant_id,
            template=self.lead.template,
            template_vars=self.template_vars,
            shop_identifier=self.lead.shop_identifier,
        )

        # Create VAD analyzer and store reference for muting
        self.vad_analyzer = SileroVADAnalyzer(
            sample_rate=16000,
            params=VADParams(
                confidence=BREEZE_BUDDY_VAD_CONFIDENCE,
                start_secs=BREEZE_BUDDY_VAD_START_SECS,
                stop_secs=BREEZE_BUDDY_VAD_STOP_SECS,
                min_volume=BREEZE_BUDDY_VAD_MIN_VOLUME,
            ),
        )

        # Create audio mixer for background sound from template configuration
        audio_out_mixer = create_background_sound_mixer(template)

        self.transport = FastAPIWebsocketTransport(
            websocket=self.ws,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                vad_analyzer=self.vad_analyzer,
                audio_out_mixer=audio_out_mixer,
                serializer=(
                    self.serializer(stream_sid, self.call_sid)
                    if self.serializer
                    else None
                ),
            ),
        )

        # Use stt_language from template if available
        stt_language = (
            template.configurations.stt_language
            if template and template.configurations
            else None
        )
        if stt_language:
            logger.info(f"Using STT language from template: {stt_language}")

        # Check if language-aware prompting is enabled
        # Enable if stt_language is set OR payload_based_language_selection is enabled
        payload_based_language_selection = (
            template.configurations.payload_based_language_selection
            if template and template.configurations
            else False
        )
        if stt_language or payload_based_language_selection:
            self.language_prompt_enabled = True
            logger.info(
                f"Language prompt enabled: will instruct LLM to speak in {self.language_name}"
            )

        stt = await get_stt_service(language_hints=stt_language)
        llm = AzureLLMService(
            api_key=AZURE_OPENAI_API_KEY,
            endpoint=AZURE_OPENAI_ENDPOINT,
            model=AZURE_BREEZE_BUDDY_OPENAI_MODEL,
            max_completion_tokens=await BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS(),
            temperature=await BREEZE_BUDDY_AZURE_TEMPERATURE(),
        )

        # Create TTS with event handlers for VAD muting
        # Use tts_voice_name from template if available
        tts_voice_name = (
            template.configurations.tts_voice_name
            if template and template.configurations
            else None
        )
        if tts_voice_name:
            logger.info(f"Using TTS voice from template: {tts_voice_name}")
        tts = await get_tts_service(voice_name=tts_voice_name)

        self.context = OpenAILLMContext()
        user_params = LLMUserAggregatorParams(
            aggregation_timeout=await BREEZE_BUDDY_LLM_AGGREGATION_TIMEOUT(),
            enable_emulated_vad_interruptions=ENABLE_BREEZE_BUDDY_USER_INTERRUPTION,
        )
        context_aggregator = llm.create_context_aggregator(
            self.context, user_params=user_params
        )

        pipeline = Pipeline(
            [
                self.transport.input(),
                stt,
                context_aggregator.user(),
                llm,
                tts,
                self.transport.output(),
                context_aggregator.assistant(),
            ]
        )
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        customer_name = self.template_vars.get("customer_name", "unknown")
        shop_name = self.template_vars.get("shop_name", "unknown")
        conversation_id = f"{customer_name}-{shop_name}-{timestamp}"

        # Only add observers in dev environment
        observers = []
        if ENVIRONMENT.lower() in ["dev", "development"]:
            observers = [
                MetricsLogObserver(),  # All metrics (STT, LLM, TTS, TTFB, etc.)
                LLMLogObserver(),  # LLM activity details
                TranscriptionLogObserver(),  # STT transcriptions
                UserBotLatencyLogObserver(),  # Response timing
                TurnTrackingObserver(),  # Conversation flow
            ]
            logger.info(
                "Development environment detected - enabling pipeline observers"
            )
        else:
            logger.info(f"Environment: {ENVIRONMENT} - pipeline observers disabled")

        # Create task parameters and initialize task
        task_params = {
            "params": PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                enable_usage_metrics=True,
                observers=observers,
            ),
        }

        # Only add tracing parameters when tracing is enabled
        if ENABLE_BREEZE_BUDDY_TRACING:
            setup_tracing("breeze-buddy")
            task_params["conversation_id"] = conversation_id
            task_params["enable_tracing"] = True

        self.task = PipelineTask(pipeline, **task_params)

        self.flow_manager = FlowManager(
            task=self.task,
            llm=llm,
            context_aggregator=context_aggregator,
            transport=self.transport,
        )

        @self.transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Client connected: {client}")
            self.flow_config = self.flow_builder.build_flow_config(self.template_config)
            self.end_conversation_callbacks = self.flow_config.get(
                "end_conversation_callbacks", []
            )
            self.expected_callback_response_schema = self.flow_config.get(
                "expected_callback_response_schema", None
            )
            logger.info(
                f"Built flow config with {len(self.flow_config['nodes'])} nodes, initial: {self.flow_config['initial_node']}, "
                f"end_conversation_callbacks: {self.end_conversation_callbacks}"
            )

            # Get the initial node config
            initial_node_name = self.flow_config["initial_node"]
            initial_node_config = self.flow_config["nodes"][initial_node_name]

            # Get role_messages and inject language instruction if enabled
            role_messages = initial_node_config.get("role_messages", [])
            role_messages = inject_language_rules(
                role_messages,
                self.template_vars.get("primary_language", self.language_name),
                self.language_prompt_enabled,
            )

            initial_node_config = NodeConfig(
                name=initial_node_config["name"],
                task_messages=initial_node_config["task_messages"],
                role_messages=role_messages,
                functions=initial_node_config["functions"],
                pre_actions=initial_node_config["pre_actions"],
                post_actions=initial_node_config["post_actions"],
            )

            # Initialize the flow with the prepared initial node
            await self.flow_manager.initialize(initial_node_config)
            logger.info(
                f"FlowManager initialized with initial node: {initial_node_name}"
            )

        @self.transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info(f"Client disconnected: {client}")
            await self._handle_unexpected_disconnect("Client disconnected unexpectedly")

        @self.task.event_handler("on_idle_timeout")
        async def on_idle_timeout(task):
            logger.info("Idle timeout detected.")
            await self._handle_unexpected_disconnect("Idle timeout")

        runner = PipelineRunner(handle_sigint=False, force_gc=True)

        async def run_pipeline():
            try:
                await runner.run(self.task)
            except asyncio.CancelledError:
                logger.info("Main task cancelled. Exiting gracefully.")

        if ENABLE_BREEZE_BUDDY_TRACING:
            tracer = trace.get_tracer(__name__)

            with tracer.start_as_current_span(conversation_id) as root_span:
                # Store root span reference for updating with evaluation data later
                self.root_span = root_span

                logger.info(
                    f"Starting Langfuse trace for Breeze Buddy conversation: {conversation_id}"
                )

                root_span.set_attribute("conversation.id", conversation_id)
                root_span.set_attribute("conversation.type", "phone-call")
                root_span.set_attribute("user.name", customer_name)
                root_span.set_attribute("service.name", "breeze-buddy")
                root_span.set_attribute("call_sid", self.call_sid)
                root_span.set_attribute("order_id", self.lead.request_id or "unknown")
                root_span.set_attribute("shop_name", shop_name)
                root_span.set_attribute("provider", self.provider)
                root_span.set_attribute("template.type", self.lead.template)

                await run_pipeline()
        else:
            # Run pipeline without tracing when tracing is disabled
            logger.info(
                f"Running pipeline without tracing for the conversation: {conversation_id}"
            )
            await run_pipeline()

    async def _handle_unexpected_disconnect(self, reason: str):
        if not self.conversation_ended:
            logger.info(f"{reason}. Updating call status directly.")
            if self.lead.outcome is None:
                self.lead.outcome = "BUSY"

            if self.lead and self.lead.metaData is None:
                self.lead.metaData = {}

            context = TemplateContext(self)
            await end_conversation(context, {})


async def main(
    ws: WebSocket,
    aiohttp_session,
    serializer,
    hangup_function,
    completion_function,
    provider: CallProvider,
):
    bot = Agent(
        ws, aiohttp_session, serializer, hangup_function, completion_function, provider
    )
    await bot.run()
