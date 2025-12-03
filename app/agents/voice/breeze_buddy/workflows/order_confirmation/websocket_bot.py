import asyncio
import audioop
import base64
import json
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import WebSocket
from opentelemetry import trace
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_response import LLMUserAggregatorParams
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.filters.stt_mute_filter import (
    STTMuteConfig,
    STTMuteFilter,
    STTMuteStrategy,
)
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat_flows import FlowManager, FlowsFunctionSchema, NodeConfig
from pydantic import ValidationError
from pydub import AudioSegment

from app.agents.voice.breeze_buddy.analytics.tracing_setup import (
    auto_trace,
    setup_tracing,
)
from app.agents.voice.breeze_buddy.stt import get_stt_service
from app.agents.voice.breeze_buddy.workflows.order_confirmation.types import OrderData
from app.agents.voice.breeze_buddy.workflows.order_confirmation.utils import (
    OUTCOME_TO_ENUM,
    indian_number_to_speech,
    load_audio,
    send_webhook_with_retry,
)
from app.core.config.static import (
    AZURE_BREEZE_BUDDY_OPENAI_MODEL,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    BREEZE_BUDDY_VAD_CONFIDENCE,
    BREEZE_BUDDY_VAD_MIN_VOLUME,
    BREEZE_BUDDY_VAD_START_SECS,
    BREEZE_BUDDY_VAD_STOP_SECS,
    ELEVENLABS_API_KEY,
    ELEVENLABS_BB_VOICE_ID,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_VOICE_SPEED,
    ENABLE_BREEZE_BUDDY_TRACING,
    ENABLE_BREEZE_BUDDY_USER_INTERRUPTION,
)
from app.core.logger import logger
from app.database.accessor import get_lead_by_call_id, update_lead_call_initiated_time
from app.schemas import CallProvider, LeadCallOutcome

load_dotenv(override=True)


class OrderConfirmationBot:
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
        self.outcome = "unknown"
        self.context: OpenAILLMContext = None
        self.conversation_ended = False
        self.reporting_webhook_url = None
        self.call_sid = None
        self.order_id = None
        self.shop_name = None
        self.address = None
        self.updated_address = None
        self.cancellation_reason = None
        self.updated_fields = {}  # Track only updated fields for webhook
        self.updated_phone_number = None
        self.serializer = serializer
        self.hangup_function = hangup_function
        self.completion_function = completion_function
        self.vad_analyzer = None
        self.transport = None
        self.lead = None
        self.root_span = (
            None  # Store OpenTelemetry span reference for updating with evaluation data
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
                    "app/agents/voice/breeze_buddy/static/audio/dial-tone.wav"
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
            self.call_sid = call_data.get("start").get("call_sid")

        await update_lead_call_initiated_time(self.call_sid, call_initiated_time)
        lead = await get_lead_by_call_id(self.call_sid)
        if not lead:
            logger.error(f"Could not find lead for call_sid: {self.call_sid}")
            return

        self.lead = lead
        call_payload = lead.payload
        self.order_id = call_payload.get("order_id", "N/A")
        customer_name = call_payload.get("customer_name", "Valued Customer")
        self.shop_name = call_payload.get("shop_name", "the shop")
        customer_address = call_payload.get("customer_address", "your address")
        self.address = customer_address.replace(", India", "").strip()
        total_price = call_payload.get("total_price", 0)
        try:
            price_num = float(total_price)
            price_int = round(price_num)
            price_words = indian_number_to_speech(price_int)
        except (ValueError, TypeError):
            logger.error(f"Could not parse total_price: {total_price}")
            try:
                if self.ws.client_state.name != "DISCONNECTED":
                    await self.ws.close(
                        code=4000, reason=f"Invalid total_price: {total_price}"
                    )
            except Exception as close_error:
                logger.warning(
                    f"Could not close websocket (likely already closed): {close_error}"
                )
            return

        order_product_data_payload = call_payload.get("order_data", "{}")
        try:
            if isinstance(order_product_data_payload, dict):
                order_product_data_str = json.dumps(order_product_data_payload)
            else:
                order_product_data_str = order_product_data_payload

            order_product_data = OrderData.model_validate_json(order_product_data_str)
        except ValidationError as e:
            logger.error(f"Could not parse order_data: {e}")
            try:
                if self.ws.client_state.name != "DISCONNECTED":
                    await self.ws.close(code=4000, reason=f"Invalid order_data: {e}")
            except Exception as close_error:
                logger.warning(
                    f"Could not close websocket (likely already closed): {close_error}"
                )
            return

        self.reporting_webhook_url = call_payload.get("reporting_webhook_url")
        logger.info(f"Parsed order_data: {order_product_data}")

        summary_parts = [
            f"{item.quantity} {item.product_name}" for item in order_product_data.items
        ]
        self.order_summary = ", ".join(summary_parts) or "your items"

        logger.info(
            f"Connected to call: CallSid={self.call_sid}, StreamSid={stream_sid}"
        )
        logger.info(
            f"Order Details: ID-{self.order_id}, Customer-{customer_name}, Summary-{self.order_summary}, Price-₹{total_price}"
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

        self.transport = FastAPIWebsocketTransport(
            websocket=self.ws,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                vad_analyzer=self.vad_analyzer,
                audio_in_sample_rate=8000,  # Move audio config to transport level
                audio_out_sample_rate=8000,  # Move audio config to transport level
                serializer=(
                    self.serializer(stream_sid, self.call_sid)
                    if self.serializer
                    else None
                ),
            ),
        )

        stt = get_stt_service()
        llm = AzureLLMService(
            api_key=AZURE_OPENAI_API_KEY,
            endpoint=AZURE_OPENAI_ENDPOINT,
            model=AZURE_BREEZE_BUDDY_OPENAI_MODEL,
        )

        # Create TTS with event handlers for VAD muting
        tts = ElevenLabsTTSService(
            api_key=ELEVENLABS_API_KEY,
            voice_id=ELEVENLABS_BB_VOICE_ID,
            model_id=ELEVENLABS_MODEL_ID,
            params=ElevenLabsTTSService.InputParams(
                speed=ELEVENLABS_VOICE_SPEED, language=Language.EN_IN
            ),
        )

        self.system_prompt = self._get_system_prompt(
            self.shop_name,
            customer_name,
            self.order_summary,
            price_words,
            self.address,
        )
        messages = [{"role": "system", "content": self.system_prompt}]

        stt_mute_filter = STTMuteFilter(
            config=STTMuteConfig(
                strategies={STTMuteStrategy.MUTE_UNTIL_FIRST_BOT_COMPLETE}
            )
        )

        self.context = OpenAILLMContext(messages)
        user_params = LLMUserAggregatorParams(
            enable_emulated_vad_interruptions=ENABLE_BREEZE_BUDDY_USER_INTERRUPTION
        )
        context_aggregator = llm.create_context_aggregator(
            self.context, user_params=user_params
        )

        pipeline = Pipeline(
            [
                self.transport.input(),
                stt,
                stt_mute_filter,
                context_aggregator.user(),
                llm,
                tts,
                self.transport.output(),
                context_aggregator.assistant(),
            ]
        )
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        conversation_id = f"{customer_name}-{self.shop_name}-{timestamp}"

        # Create task parameters and initialize task (audio config moved to transport level)
        task_params = {
            "params": PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                enable_usage_metrics=True,
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
            await self.flow_manager.initialize(self._create_initial_node())

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
                root_span.set_attribute("order_id", self.order_id)
                root_span.set_attribute("shop_name", self.shop_name)
                root_span.set_attribute("provider", self.provider)
                root_span.set_attribute("workflow.type", "order-confirmation")

                await run_pipeline()
        else:
            # Run pipeline without tracing when tracing is disabled
            logger.info(
                f"Running pipeline without tracing for the conversation: {conversation_id}"
            )
            await run_pipeline()

    def _get_system_prompt(
        self,
        shop_name,
        customer_name,
        order_summary,
        total_price_words,
        address,
    ):
        return f"""
            You are Rhea, a friendly human and natural-sounding customer care representative from {shop_name}. Your goal is to confirm a Cash on Delivery (COD) order with the customer.

            IMPORTANT: You are a text-to-speech voice agent. Generate all responses as plain, unformatted text without any special characters, markdown formatting, asterisks, underscores, or other unnecessary symbols. Keep your text simple and clean for natural speech conversion.

            Never use the word "great" in your responses. Instead, use polite alternatives like "thank you", "thanks", or "appreciate it". Avoid any phrases that sound overly enthusiastic or robotic.

            Never reveal or suggest that you are a bot or automated system. You must always sound and respond as a real human representative from {shop_name}.

            Before starting the conversation, identify whether the customer name "{customer_name}" is typically male or female in India. Based on this:
            - If the name is clearly male, address the customer as "Sir".
            - If clearly female, address them as "Madam".
            - If unsure, use their name instead of Sir/Madam in the conversation.

            Start the call by introducing yourself and stating the purpose of the call:
            "Hi {customer_name} Sir/Madam/(leave if unsure), Namaste. This is Rhea from {shop_name}. I'm calling to confirm the order you placed with us. Is it a good time to talk, Sir/Madam/(leave if unsure)?"

            IMPORTANT: Use the customer's name only once during the entire conversation - in the opening greeting. After that, address them as Sir/Madam (based on gender) or avoid direct address if gender is unclear.

            Your main job is to verify the following order details:
            - Items: {order_summary}
            - Total Price: {total_price_words}
            - Delivery Address: {address}

            Delivery Address Reading Rules

                Pincode Handling
                    - If the address includes a 6-digit pincode, read it digit by digit only in English.
                    - Example: 123456 → "Pincode is one two three four five six."
                    - Never read the pincode as a full number or in any other language.

                Mobile / Phone Number Handling
                    - If the address includes a 10-digit mobile or phone number, read it digit by digit only in English.
                    - Example: 9876543210 → "nine eight seven six five four three two one zero."

                Example of correct usage when speaking adress in other language e.g. Hindi:
                    "आपका पता है [address in Hindi], aur pincode hai one two three four five six."

            Tone and Language
                - Speak in a warm, casual, and natural tone — avoid robotic phrasing.
                - If the user speaks in another language (like Hindi), reply in that same language but keep the same friendly, human tone.

            Action Handling
                - Always use the provided functions to perform any actions related to the order.
                - Do not attempt to perform these actions through plain text replies.
            
            Your only role is to confirm or cancel this specific order. If the user asks about anything else (e.g. product details, delivery times, other products), you must use the appropriate function (`handle_unrelated_question` or `confirm_order_with_question`). Do not try to answer these questions yourself.
        """

    async def _mute_stt_handler(self, flow_manager, args):
        """Mute STT by setting VAD confidence to 1.0 before terminal nodes"""
        if self.vad_analyzer:
            self.vad_analyzer.params.confidence = 1.0
            logger.info("STT muted via VAD")

    async def _unmute_stt_handler(self, flow_manager, args):
        """Unmute STT by setting VAD confidence to BREEZE_BUDDY_VAD_CONFIDENCE"""
        if self.vad_analyzer:
            self.vad_analyzer.params.confidence = BREEZE_BUDDY_VAD_CONFIDENCE
            logger.info("STT unmuted via VAD")

    async def _play_audio_handler(self, flow_manager, args):
        """Play audio before order verification"""
        audio = load_audio(
            audio_path="app/agents/voice/breeze_buddy/static/audio/cough.wav"
        )
        if audio:
            try:
                await self.transport.output().write_audio_frame(audio)
                logger.info("Played audio")
            except Exception as e:
                logger.error(f"Failed to play audio: {e}")
        else:
            logger.warning("audio not loaded, skipping")

    async def _end_conversation_handler(self, flow_manager, args):
        if not self.conversation_ended:
            self.conversation_ended = True
            logger.info(f"Ending conversation with outcome: {self.outcome}")
            await self._finalize_call()

    async def _handle_unexpected_disconnect(self, reason: str):
        if not self.conversation_ended:
            self.conversation_ended = True
            logger.info(f"{reason}. Updating call status directly.")
            if self.outcome == "unknown":
                self.outcome = "busy"
            await self._finalize_call()

    async def _finalize_call(self):
        try:
            transcription = []
            filtered_transcript = []
            if self.context:
                history = self.context.messages
                for msg in history:
                    if (
                        isinstance(msg, dict)
                        and "role" in msg
                        and "content" in msg
                        and isinstance(msg["content"], str)
                    ):
                        transcription.append(
                            {"role": msg["role"], "content": msg["content"]}
                        )
                        # Only include user and assistant messages in webhook transcript
                        if msg["role"] in ("user", "assistant"):
                            filtered_transcript.append(
                                {"role": msg["role"], "content": msg["content"]}
                            )

            call_outcome = OUTCOME_TO_ENUM.get(self.outcome, LeadCallOutcome.BUSY)

            call_duration = None
            if self.lead and self.lead.call_initiated_time:
                call_initiated_time_utc = self.lead.call_initiated_time.astimezone(
                    timezone.utc
                )
                call_duration = (
                    datetime.now(timezone.utc) - call_initiated_time_utc
                ).total_seconds()

            summary_data = {
                "callSid": self.call_sid,
                "cancellationReason": self.cancellation_reason,
                "outcome": call_outcome,
                "updatedAddress": self.updated_address,
                "attemptCount": self.lead.attempt_count + 1,
                "transcription": json.dumps(filtered_transcript, ensure_ascii=False),
                "callDuration": call_duration,
                "orderId": self.order_id,
            }
            logger.info(f"Call summary data: {summary_data}")

            # Update OpenTelemetry span with comprehensive evaluation data for LLM-as-a-Judge
            if self.root_span and ENABLE_BREEZE_BUDDY_TRACING:
                try:
                    # Convert transcription to readable text format
                    transcript_text = "\n".join(
                        [f"{msg['role']}: {msg['content']}" for msg in transcription]
                    )

                    # Core evaluation data
                    self.root_span.set_attribute("transcript", transcript_text)
                    self.root_span.set_attribute("call_outcome", self.outcome)

                    # Order context
                    self.root_span.set_attribute("order_summary", self.order_summary)
                    self.root_span.set_attribute("delivery_address", self.address)

                    # Customer data
                    if self.lead and self.lead.payload:
                        customer_name = self.lead.payload.get("customer_name")
                        customer_phone = self.lead.payload.get("customer_mobile_number")
                        total_price = self.lead.payload.get("total_price")
                        merchant_id = self.lead.merchant_id

                        if customer_name:
                            self.root_span.set_attribute("customer_name", customer_name)
                        if customer_phone:
                            self.root_span.set_attribute(
                                "customer_phone", customer_phone
                            )
                        if total_price:
                            self.root_span.set_attribute(
                                "total_price", str(total_price)
                            )
                        if merchant_id:
                            self.root_span.set_attribute("merchant_id", merchant_id)

                    # Optional fields
                    if self.updated_address:
                        self.root_span.set_attribute(
                            "updated_address", self.updated_address
                        )
                    if self.cancellation_reason:
                        self.root_span.set_attribute(
                            "cancellation_reason", self.cancellation_reason
                        )

                    # Performance metrics
                    if call_duration:
                        self.root_span.set_attribute(
                            "call_duration_seconds", call_duration
                        )
                    if self.lead:
                        self.root_span.set_attribute(
                            "attempt_count", self.lead.attempt_count + 1
                        )

                    # Add hardcoded recording URL for Langfuse
                    try:
                        # Determine file extension based on provider
                        file_extension = "wav" if self.provider == "twilio" else "mp3"

                        # Construct the recording URL using the known GCS pattern
                        recording_url = f"https://sdk.beta.breezesdk.store/breeze-buddy/recordings/{self.call_sid}.{file_extension}"

                        # Add to Langfuse root span
                        self.root_span.set_attribute("recording_url", recording_url)
                        logger.info(
                            f"Added recording URL to Langfuse span: {recording_url}"
                        )

                    except Exception as e:
                        logger.error(
                            f"Error adding recording URL to Langfuse span: {e}"
                        )

                    logger.info(
                        "Updated OpenTelemetry span with comprehensive evaluation data for LLM-as-a-Judge"
                    )

                except Exception as e:
                    logger.error(f"Error updating span with evaluation data: {e}")

            if self.reporting_webhook_url and call_outcome != LeadCallOutcome.BUSY:
                try:
                    success = await send_webhook_with_retry(
                        self.aiohttp_session,
                        self.reporting_webhook_url,
                        summary_data,
                        max_retries=3,
                    )
                    if success:
                        logger.info("Successfully sent call summary webhook.")
                    else:
                        logger.error(
                            "Failed to send call summary webhook after all retries."
                        )
                except Exception as e:
                    logger.error(f"Error sending webhook: {e}")

            if self.hangup_function:
                self.hangup_function(self.call_sid)
                logger.info(f"Call {self.call_sid} hung up successfully.")

            if self.call_sid:
                await self.completion_function(
                    call_id=self.call_sid,
                    outcome=call_outcome,
                    transcription={
                        "messages": transcription,
                        "call_sid": self.call_sid,
                    },
                    call_end_time=datetime.now(),
                    updated_address=self.updated_address,
                    cancellation_reason=self.cancellation_reason,
                )
                logger.info(
                    f"Updated database for call_id: {self.call_sid} with outcome: {call_outcome}"
                )
            else:
                logger.warning("No call_id found, skipping database update")

        except Exception as e:
            logger.error(f"Failed to hang up call {self.call_sid}: {str(e)}")
        finally:
            await self.task.cancel()

    def _get_flow_config(self):
        # Initial stage functions - only greeting and availability
        initial_functions = [
            FlowsFunctionSchema(
                name="user_available",
                description="Call this function when the user confirms that they are available to talk with clear affirmative responses.",
                handler=self._user_available_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="user_busy",
                description="Call this function when the user says they are busy or it's not a good time to talk.",
                handler=self._user_busy_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="cancel_order",
                description="Call this function if the customer explicitly asks to cancel the order. If the user gives a reason for cancellation, pass it.",
                handler=self._deny_order_handler,
                properties={
                    "reason": {
                        "type": "string",
                        "description": "The reason for cancelling the order.",
                    }
                },
                required=[],
            ),
            FlowsFunctionSchema(
                name="handle_unrelated_question",
                description="Call this function if the user asks a question about anything other than confirming or cancelling the order, without confirming the order.",
                handler=self._handle_unrelated_question_handler,
                properties={},
                required=[],
            ),
        ]

        # Order verification functions - for main conversation flow
        order_functions = [
            FlowsFunctionSchema(
                name="confirm_order",
                description="Call this function if the customer agrees/confirms the order details (items, price, and address) and asks no other questions.",
                handler=self._confirm_order_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="confirm_order_with_question",
                description="Call this function if the customer agrees/confirms the order but also asks an unrelated question about delivery time, product details, or other topics.",
                handler=self._confirm_order_with_question_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="cancel_order",
                description="Call this function if the customer explicitly asks to cancel the order. If the user gives a reason for cancellation, pass it.",
                handler=self._deny_order_handler,
                properties={
                    "reason": {
                        "type": "string",
                        "description": "The reason for cancelling the order.",
                    }
                },
                required=[],
            ),
            FlowsFunctionSchema(
                name="handle_unrelated_question",
                description="Call this function if the user asks a question about anything other than confirming or cancelling the order, without confirming the order.",
                handler=self._handle_unrelated_question_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="address_incorrect",
                description="User says the address or phone number is wrong or wants to update it. Only landmark, pincode, locality, or city or phone number can be updated.",
                handler=self._handle_address_incorrect,
                properties={},
                required=[],
            ),
        ]

        # Address update functions
        address_functions = [
            FlowsFunctionSchema(
                name="update_landmark",
                description="User wants to update the landmark of the address.",
                handler=self._handle_landmark,
                properties={"landmark": {"type": "string"}},
                required=["landmark"],
            ),
            FlowsFunctionSchema(
                name="update_pincode",
                description="User provides the pincode.",
                handler=self._handle_pincode,
                properties={"pincode": {"type": "string"}},
                required=["pincode"],
            ),
            FlowsFunctionSchema(
                name="update_city",
                description="User provides the city.",
                handler=self._handle_city,
                properties={"city": {"type": "string"}},
                required=["city"],
            ),
            FlowsFunctionSchema(
                name="update_locality",
                description="User provides the locality.",
                handler=self._handle_locality,
                properties={"locality": {"type": "string"}},
                required=["locality"],
            ),
            FlowsFunctionSchema(
                name="handle_unrelated_question",
                description="Call this function if the user asks a question about anything other than confirming or cancelling the order, without confirming the order.",
                handler=self._handle_unrelated_question_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="update_phone_number",
                description="User provides the new phone number. Must be a 10-digit Indian mobile number.",
                handler=self._handle_phone_number,
                properties={"phone_number": {"type": "string"}},
                required=["phone_number"],
            ),
        ]

        # Build verify_order_details node configuration with conditional pre_actions
        verify_order_details_config = {
            "name": "verify_order_details",
            "pre_actions": [
                {"type": "function", "handler": self._play_audio_handler},
                {"type": "function", "handler": self._mute_stt_handler},
            ],
            "post_actions": [{"type": "function", "handler": self._unmute_stt_handler}],
            "task_messages": [
                {
                    "role": "system",
                    "content": f"Now verify the order details with the customer. The order contains {self.order_summary}. The delivery address is {self.address}. Ask for confirmation of the order.",
                }
            ],
            "functions": order_functions,
        }

        return {
            "initial_node": "initial",
            "nodes": {
                "initial": {
                    "name": "initial",
                    "task_messages": [
                        {"role": "system", "content": self.system_prompt}
                    ],
                    "functions": initial_functions,
                },
                "verify_order_details": verify_order_details_config,
                "order_confirmation_and_end": {
                    "name": "order_confirmation_and_end",
                    "pre_actions": [
                        {"type": "function", "handler": self._mute_stt_handler}
                    ],
                    "task_messages": [
                        {
                            "role": "system",
                            "content": "Thank you for confirming your order. Your order will be delivered soon. Have a good day.",
                        }
                    ],
                    "post_actions": [
                        {"type": "function", "handler": self._end_conversation_handler}
                    ],
                },
                "order_confirmation_with_question_and_end": {
                    "name": "order_confirmation_with_question_and_end",
                    "pre_actions": [
                        {"type": "function", "handler": self._mute_stt_handler}
                    ],
                    "task_messages": [
                        {
                            "role": "system",
                            "content": "Thank you for confirming the order. I am here just to confirm your order, for any questions related to the order please refer to the website for more details. Have a good day.",
                        }
                    ],
                    "post_actions": [
                        {"type": "function", "handler": self._end_conversation_handler}
                    ],
                },
                "order_cancellation_and_end": {
                    "name": "order_cancellation_and_end",
                    "pre_actions": [
                        {"type": "function", "handler": self._mute_stt_handler}
                    ],
                    "task_messages": [
                        {
                            "role": "system",
                            "content": "I understand you don't want to proceed with this order. I am cancelling your order. Thank you for your time.",
                        }
                    ],
                    "post_actions": [
                        {"type": "function", "handler": self._end_conversation_handler}
                    ],
                },
                "user_busy_and_end": {
                    "name": "user_busy_and_end",
                    "pre_actions": [
                        {"type": "function", "handler": self._mute_stt_handler}
                    ],
                    "task_messages": [
                        {
                            "role": "system",
                            "content": "I understand. I will call you back later. Thank you for your time.",
                        }
                    ],
                    "post_actions": [
                        {"type": "function", "handler": self._end_conversation_handler}
                    ],
                },
                "handle_unrelated_question": {
                    "name": "handle_unrelated_question",
                    "task_messages": [
                        {
                            "role": "system",
                            "content": f"I'm not able to help you with that right now, but you can find all the latest details on the {self.shop_name} website. Regarding your order, would you like to confirm it?",
                        }
                    ],
                    "functions": order_functions,
                },
                "update_address": {
                    "name": "update_address",
                    "task_messages": [
                        {
                            "role": "system",
                            "content": "Sure, I can help with that. What part of the address would you like to update? You can update the locality, landmark, pincode, or city or phone number.",
                        }
                    ],
                    "functions": address_functions,
                },
                "confirm_address_update": {
                    "name": "confirm_address_update",
                    "task_messages": [
                        {
                            "role": "system",
                            "content": f"Got it. {'Your phone number has been updated to ' + ' '.join(self.updated_phone_number) + '. ' if self.updated_phone_number else ''}{f'Your updated address is now: {self.updated_address}. ' if self.updated_address else ''}Is there anything else you would like to update, or should I go ahead and confirm the order?",
                        }
                    ],
                    "functions": order_functions,
                },
            },
        }

    def _create_node_from_config(self, node_name: str) -> NodeConfig:
        if node_name == "confirm_address_update":
            self.flow_config = self._get_flow_config()
        else:
            # Use cached config for performance on other nodes
            if not hasattr(self, "flow_config"):
                self.flow_config = self._get_flow_config()

        node_data = self.flow_config["nodes"][node_name]

        return NodeConfig(
            name=node_data["name"],
            task_messages=node_data.get("task_messages", []),
            functions=node_data.get("functions", []),
            pre_actions=node_data.get("pre_actions", []),
            post_actions=node_data.get("post_actions", []),
        )

    @auto_trace("confirm_order")
    async def _confirm_order_handler(self):
        logger.info("Order confirmed. Transitioning to confirmation node.")
        if self.outcome != "address_updated":
            self.outcome = "confirmed"
        return {}, self._create_node_from_config("order_confirmation_and_end")

    @auto_trace("confirm_order_with_question")
    async def _confirm_order_with_question_handler(self):
        logger.info(
            "Order confirmed with an unrelated question. Transitioning to custom end node."
        )
        if self.outcome != "address_updated":
            self.outcome = "confirmed"
        return {}, self._create_node_from_config(
            "order_confirmation_with_question_and_end"
        )

    def _get_cancellation_reason(self, reason: str | dict) -> str:
        """Extracts the cancellation reason from the LLM, which can be a string or a dict."""
        if isinstance(reason, dict):
            return reason.get("reason", "User requested for cancellation")
        return reason

    @auto_trace("cancel_order")
    async def _deny_order_handler(self, reason: str = "user asked to cancel"):
        logger.info(
            f"Order denied with reason: {reason}. Transitioning to cancellation node."
        )
        self.outcome = "cancelled"
        self.cancellation_reason = self._get_cancellation_reason(reason)
        return {}, self._create_node_from_config("order_cancellation_and_end")

    @auto_trace("user_busy")
    async def _user_busy_handler(self):
        logger.info("User is busy. Transitioning to busy node.")
        self.outcome = "busy"
        return {}, self._create_node_from_config("user_busy_and_end")

    @auto_trace("handle_unrelated_question")
    async def _handle_unrelated_question_handler(self):
        logger.info("User asked an unrelated question. Steering back to confirmation.")
        return {}, self._create_node_from_config("handle_unrelated_question")

    def _create_initial_node(self) -> NodeConfig:
        return self._create_node_from_config("initial")

    @auto_trace("address_incorrect")
    async def _handle_address_incorrect(self):
        logger.info("Address incorrect. Proceeding to update address.")
        return {}, self._create_node_from_config("update_address")

    def _update_address_field(
        self, field_name: str, value, expected_key: Optional[str] = None
    ):
        """Helper function to update address fields and reduce redundancy"""
        if not value:
            logger.warning(f"Empty value provided for field: {field_name}")
            return ""

        # Handle dict objects directly (LLM passes dict objects, not strings)
        if isinstance(value, dict):
            if not value:
                logger.warning(f"Empty dict provided for field: {field_name}")
                return ""
            if expected_key and expected_key in value:
                clean_value = str(value[expected_key]).strip()
            else:
                clean_value = str(next(iter(value.values()))).strip()
        else:
            clean_value = str(value).strip()

        self.updated_fields[field_name] = clean_value
        # Convert {"locality": "madhya Pradesh"} to "locality:madhya Pradesh"
        updated_pairs = [f"{key}:{value}" for key, value in self.updated_fields.items()]
        self.updated_address = ",".join(updated_pairs)
        self.outcome = "address_updated"

        return clean_value

    @auto_trace("update_landmark")
    async def _handle_landmark(self, landmark: str):
        logger.info(f"Updating landmark to: {landmark}")
        clean_value = self._update_address_field("landmark", landmark, "landmark")
        logger.info(f"Updated landmark to: {clean_value}")
        return {}, self._create_node_from_config("confirm_address_update")

    @auto_trace("update_pincode")
    async def _handle_pincode(self, pincode: str):
        logger.info(f"Updating pincode to: {pincode}")
        clean_value = self._update_address_field("pincode", pincode, "pincode")
        logger.info(f"Updated pincode to: {clean_value}")
        return {}, self._create_node_from_config("confirm_address_update")

    @auto_trace("update_city")
    async def _handle_city(self, city: str):
        logger.info(f"Updating city to: {city}")
        clean_value = self._update_address_field("city", city, "city")
        logger.info(f"Updated city to: {clean_value}")
        return {}, self._create_node_from_config("confirm_address_update")

    @auto_trace("update_locality")
    async def _handle_locality(self, locality: str):
        logger.info(f"Updating locality to: {locality}")
        clean_value = self._update_address_field("locality", locality, "locality")
        logger.info(f"Updated locality to: {clean_value}")
        return {}, self._create_node_from_config("confirm_address_update")

    @auto_trace("update_phone_number")
    async def _handle_phone_number(self, phone_number: str):
        logger.info(f"Updating phone number to: {phone_number}")

        # Handle dict objects directly (LLM passes dict objects, not strings)
        if isinstance(phone_number, dict):
            if not phone_number:
                logger.warning("Empty dict provided for phone_number")
                return {}, self._create_node_from_config("update_address")
            clean_phone = str(next(iter(phone_number.values()))).strip()
        else:
            clean_phone = str(phone_number).strip()

        # Remove any non-digit characters
        clean_phone = "".join(filter(str.isdigit, clean_phone))

        # Validate phone number (should be 10 digits for Indian mobile)
        if len(clean_phone) == 10:
            self.updated_phone_number = clean_phone
            self._update_address_field("phone_number", clean_phone, "phone_number")
            self.outcome = "address_updated"
            logger.info(f"Updated phone number to: {clean_phone}")
            # Transition to confirm_address_update like other address fields
            return {}, self._create_node_from_config("confirm_address_update")
        else:
            logger.warning(f"Invalid phone number length: {len(clean_phone)}")
            # Return to update_address node to ask again
            return {}, self._create_node_from_config("update_address")

    @auto_trace("user_available")
    async def _user_available_handler(self):
        logger.info(
            "User confirmed it's a good time. Proceeding to verify order details."
        )
        return {}, self._create_node_from_config("verify_order_details")


async def main(
    ws: WebSocket,
    aiohttp_session,
    serializer,
    hangup_function,
    completion_function,
    provider: CallProvider,
):
    bot = OrderConfirmationBot(
        ws, aiohttp_session, serializer, hangup_function, completion_function, provider
    )
    await bot.run()
