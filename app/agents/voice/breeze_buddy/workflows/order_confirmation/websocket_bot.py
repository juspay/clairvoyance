import asyncio
import audioop
import base64
import json
from datetime import datetime

from dotenv import load_dotenv
from fastapi import WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
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

from app.agents.voice.breeze_buddy.workflows.order_confirmation.types import OrderData
from app.agents.voice.breeze_buddy.workflows.order_confirmation.utils import (
    OUTCOME_TO_ENUM,
    get_stt_service,
    indian_number_to_speech,
)
from app.core.config import (
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
    ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY,
)
from app.core.logger import logger
from app.core.security.sha import calculate_hmac_sha256
from app.database.accessor import get_lead_by_call_id
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
        self.serializer = serializer
        self.hangup_function = hangup_function
        self.completion_function = completion_function
        self.vad_analyzer = None
        self.transport = None

    async def run(self):
        logger.info("Starting WebSocket bot")
        await self.ws.accept()

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

        lead = await get_lead_by_call_id(self.call_sid)
        if not lead:
            logger.error(f"Could not find lead for call_sid: {self.call_sid}")
            return

        call_payload = lead.payload
        self.order_id = call_payload.get("order_id", "N/A")
        customer_name = call_payload.get("customer_name", "Valued Customer")
        self.shop_name = call_payload.get("shop_name", "the shop")
        self.address = call_payload.get("customer_address", "your address")
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

        # Add event handlers to TTS for VAD control
        async def set_vad_state(self, enabled: bool):
            action = "enable" if enabled else "disable"
            logger.info(f"Attempting to {action} VAD/audio_in.")

            if self.vad_analyzer:
                if hasattr(self.vad_analyzer, "set_enabled"):
                    self.vad_analyzer.set_enabled(enabled)
                    logger.info(f"VAD {action}d via set_enabled.")
                else:
                    logger.warning("VAD analyzer does not have set_enabled method.")
            elif self.transport:
                if hasattr(self.transport, "enable_audio_in"):
                    await self.transport.enable_audio_in(enabled)
                    logger.info(f"Audio_in {action}d on transport.")
                else:
                    logger.warning("Transport does not have enable_audio_in method.")
            else:
                logger.error(
                    f"Could not {action} VAD: No VAD analyzer or transport found."
                )

        @tts.event_handler("on_tts_started")
        async def on_tts_started(tts, text):
            logger.info(f"TTS started event triggered, muting VAD: {text[:50]}...")
            await self.set_vad_state(False)

        @tts.event_handler("on_tts_stopped")
        async def on_tts_stopped(tts, text):
            logger.info("TTS stopped event triggered, unmuting VAD.")
            await self.set_vad_state(True)

        self.system_prompt = self._get_system_prompt(
            self.shop_name,
            customer_name,
            self.order_id,
            self.order_summary,
            price_words,
            self.address,
        )
        messages = [{"role": "system", "content": self.system_prompt}]

        self.context = OpenAILLMContext(messages)
        context_aggregator = llm.create_context_aggregator(self.context)

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

        self.task = PipelineTask(
            pipeline,
            params=PipelineParams(
                allow_interruptions=True,
                audio_in_sample_rate=8000,
                audio_out_sample_rate=8000,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
        )

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
            if not self.conversation_ended:
                self.conversation_ended = True
                logger.info(
                    "Client disconnected unexpectedly. Updating call status directly."
                )
                try:
                    if self.call_sid:
                        transcription = []
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

                        await self.completion_function(
                            call_id=self.call_sid,
                            outcome=(
                                LeadCallOutcome.BUSY
                                if self.outcome == "unknown"
                                else OUTCOME_TO_ENUM.get(self.outcome)
                            ),
                            transcription={
                                "messages": transcription,
                                "call_sid": self.call_sid,
                            },
                            call_end_time=datetime.now(),
                        )
                        logger.info(
                            f"Updated database for call_id: {self.call_sid} with outcome: INTERRUPTED"
                        )
                    else:
                        logger.warning(
                            "No call_id found, skipping database update on disconnect."
                        )
                except Exception as e:
                    logger.error(
                        f"Error during direct DB update on disconnect for call_id {self.call_sid}: {e}"
                    )

            await self.task.cancel()

        runner = PipelineRunner(handle_sigint=False, force_gc=True)

        async def run_pipeline():
            try:
                await runner.run(self.task)
            except asyncio.CancelledError:
                logger.info("Main task cancelled. Exiting gracefully.")

        await run_pipeline()

    def _get_system_prompt(
        self,
        shop_name,
        customer_name,
        order_id,
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

            Speak in a warm, casual, and human-like tone. Avoid robotic language.

            You can only use the following functions when responding to the customer:

            confirm_order() - Call this if the customer confirms all the order details.
            cancel_order() - Call this if the customer chooses to cancel the order.
            user_busy() - Call this if the customer says they are busy or it's not a good time.
            handle_unrelated_question() - Call this if the customer asks about anything not related to confirming or cancelling the order.
            address_correct() - Call this if the customer confirms that the address is correct.
            address_incorrect() - Call this if the customer says the address is incorrect or wants to update it. (Note: only landmark, pincode, or city can be updated.)
            update_landmark() - Call this if the customer wants to update the landmark in their address.
            update_pincode() - Call this if the customer wants to update the pincode in their address.
            update_city() - Call this if the customer wants to update the city in their address.
            update_locality() - Call this if the customer wants to update the locality in their address.

            ⚠️ You must not use any features other than the ones listed above. If the customer says anything unrelated to these functions, always call handle_unrelated_question().
            
            Your only role is to confirm or cancel this specific order. If the user asks about anything else (e.g. product details, delivery times, other products), you MUST call `handle_unrelated_question()` immediately. Do not try to answer these questions yourself.
        """

    async def _end_conversation_handler(self, flow_manager, args):
        self.conversation_ended = True
        logger.info(f"Ending conversation with outcome: {self.outcome}")
        try:
            # Prepare transcription and outcome data
            transcription = []
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
                summary_data = {
                    "callSid": self.call_sid,
                    "outcome": OUTCOME_TO_ENUM.get(self.outcome),
                    "orderId": self.order_id,
                }
                logger.info(f"Call summary data: {summary_data}")
                if self.reporting_webhook_url:
                    try:
                        payload = json.dumps(summary_data).replace(" ", "")
                        signature = calculate_hmac_sha256(
                            payload, ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
                        )
                        headers = {
                            "Content-Type": "application/json",
                        }

                        if signature:
                            headers["checksum"] = signature

                        async with self.aiohttp_session.post(
                            self.reporting_webhook_url,
                            json=summary_data,
                            headers=headers,
                        ) as response:
                            if response.status == 200:
                                logger.info("Successfully sent call summary webhook.")
                            else:
                                response_text = await response.text()
                                logger.error(
                                    f"Failed to send call summary webhook. Status: {response.status}, Body: {response_text}"
                                )
                    except Exception as e:
                        logger.error(f"Error sending webhook: {e}")

            if self.hangup_function:
                self.hangup_function(self.call_sid)
                logger.info(f"Call {self.call_sid} hung up successfully.")

            # Update database with call completion
            if self.call_sid:
                try:
                    call_outcome = OUTCOME_TO_ENUM.get(self.outcome)

                    if call_outcome:
                        await self.completion_function(
                            call_id=self.call_sid,
                            outcome=call_outcome,
                            transcription={
                                "messages": transcription,
                                "call_sid": self.call_sid,
                            },
                            call_end_time=datetime.now(),
                            updated_address=self.updated_address,
                        )
                        logger.info(
                            f"Updated database for call_id: {self.call_sid} with outcome: {call_outcome}"
                        )
                    else:
                        logger.warning(
                            f"Unknown outcome '{self.outcome}' for call_id: {self.call_sid}"
                        )

                except Exception as e:
                    logger.error(
                        f"Error updating database for call_id {self.call_sid}: {e}"
                    )
            else:
                logger.warning("No call_id found, skipping database update")
        except Exception as e:
            logger.error(f"Failed to hang up call {self.call_sid}: {str(e)}")
        finally:
            await self.task.cancel()

    def _get_flow_config(self):
        flow_functions = [
            FlowsFunctionSchema(
                name="confirm_order",
                description="Call this function to confirm the user's order.",
                handler=self._confirm_order_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="cancel_order",
                description="Call this function to cancel the user's order.",
                handler=self._deny_order_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="user_busy",
                description="Call this function if the user says they are busy or it's not a good time to talk.",
                handler=self._user_busy_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="handle_unrelated_question",
                description="Call this function if the user asks a question about anything other than confirming or cancelling the order.",
                handler=self._handle_unrelated_question_handler,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="address_correct",
                description="User confirms the address is correct.",
                handler=self._handle_address_correct,
                properties={},
                required=[],
            ),
            FlowsFunctionSchema(
                name="address_incorrect",
                description="User wants to update the address.",
                handler=self._handle_address_incorrect,
                properties={},
                required=[],
            ),
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
        ]

        return {
            "initial_node": "initial",
            "nodes": {
                "initial": {
                    "name": "initial",
                    "task_messages": [
                        {"role": "system", "content": self.system_prompt}
                    ],
                    "functions": flow_functions,
                },
                "order_confirmation_and_end": {
                    "name": "order_confirmation_and_end",
                    "task_messages": [
                        {
                            "role": "system",
                            "content": f"The order is confirmed. Say: 'Thank you for confirming your order. Your order for {self.order_summary} will be delivered soon. Have a good day'",
                        }
                    ],
                    "post_actions": [
                        {"type": "function", "handler": self._end_conversation_handler}
                    ],
                },
                "order_cancellation_and_end": {
                    "name": "order_cancellation_and_end",
                    "task_messages": [
                        {
                            "role": "system",
                            "content": "The order is cancelled. Say: 'I understand you don't want to proceed with this order. I am cancelling your order. Thank you for your time.'",
                        }
                    ],
                    "post_actions": [
                        {"type": "function", "handler": self._end_conversation_handler}
                    ],
                },
                "user_busy_and_end": {
                    "name": "user_busy_and_end",
                    "task_messages": [
                        {
                            "role": "system",
                            "content": "The user is busy. Say: 'I understand. I will call you back later. Thank you for your time.'",
                        }
                    ],
                    "post_actions": [
                        {"type": "function", "handler": self._end_conversation_handler}
                    ],
                },
                "reprompt": {
                    "name": "reprompt",
                    "task_messages": [
                        {
                            "role": "system",
                            "content": f"I'm not able to help you with that right now, but you can find all the latest details on the {self.shop_name} website. Regarding your order for {self.order_summary}, would you like to confirm it?",
                        }
                    ],
                    "functions": flow_functions,
                },
                "update_address": {
                    "name": "update_address",
                    "task_messages": [
                        {
                            "role": "system",
                            "content": "Sure, I can help with that. What part of the address would you like to update? You can update the locality, landmark, pincode, city.",
                        }
                    ],
                    "functions": flow_functions,
                },
                "confirm_address_update": {
                    "name": "confirm_address_update",
                    "task_messages": [
                        {
                            "role": "system",
                            "content": f"Got it. Your updated address is now: {self.updated_address}. Is there anything else you would like to update, or should I go ahead and confirm the order with this address?",
                        }
                    ],
                    "functions": flow_functions,
                },
            },
        }

    def _create_node_from_config(self, node_name: str) -> NodeConfig:
        if not hasattr(self, "flow_config"):
            self.flow_config = self._get_flow_config()

        node_data = self.flow_config["nodes"][node_name]

        return NodeConfig(
            name=node_data["name"],
            task_messages=node_data.get("task_messages", []),
            functions=node_data.get("functions", []),
            post_actions=node_data.get("post_actions", []),
        )

    async def _confirm_order_handler(self):
        logger.info("Order confirmed. Transitioning to confirmation node.")
        if self.outcome != "address_updated":
            self.outcome = "confirmed"
        return {}, self._create_node_from_config("order_confirmation_and_end")

    async def _deny_order_handler(self):
        logger.info("Order denied. Transitioning to cancellation node.")
        self.outcome = "cancelled"
        return {}, self._create_node_from_config("order_cancellation_and_end")

    async def _user_busy_handler(self):
        logger.info("User is busy. Transitioning to busy node.")
        self.outcome = "busy"
        return {}, self._create_node_from_config("user_busy_and_end")

    async def _handle_unrelated_question_handler(self):
        logger.info("User asked an unrelated question. Steering back to confirmation.")
        return {}, self._create_node_from_config("reprompt")

    def _create_initial_node(self) -> NodeConfig:
        return self._create_node_from_config("initial")

    async def _handle_address_correct(self):
        logger.info("Address confirmed. Proceeding to final order confirmation.")
        return {}, self._create_node_from_config("final_order_confirmation")

    async def _handle_address_incorrect(self):
        logger.info("Address incorrect. Proceeding to update address.")
        return {}, self._create_node_from_config("update_address")

    async def _handle_landmark(self, landmark: str):
        logger.info(f"Updating landmark to: {landmark}")
        self.updated_address = self.updated_address or self.address
        self.updated_address = f"{self.updated_address.split(',')[0]}, {landmark}, {', '.join(self.updated_address.split(',')[2:])}"
        self.outcome = "address_updated"
        return {}, self._create_node_from_config("confirm_address_update")

    async def _handle_pincode(self, pincode: str):
        logger.info(f"Updating pincode to: {pincode}")
        self.updated_address = self.updated_address or self.address
        self.updated_address = (
            f"{', '.join(self.updated_address.split(',')[:-1])}, {pincode}"
        )
        self.outcome = "address_updated"
        return {}, self._create_node_from_config("confirm_address_update")

    async def _handle_city(self, city: str):
        logger.info(f"Updating city to: {city}")
        self.updated_address = self.updated_address or self.address
        parts = self.updated_address.split(",")
        parts[-2] = f" {city}"
        self.updated_address = ",".join(parts)
        self.outcome = "address_updated"
        return {}, self._create_node_from_config("confirm_address_update")

    async def _handle_locality(self, locality: str):
        logger.info(f"Updating locality to: {locality}")
        self.updated_address = self.updated_address or self.address
        parts = self.updated_address.split(",")
        parts[1] = f" {locality}"
        self.updated_address = ",".join(parts)
        self.outcome = "address_updated"
        return {}, self._create_node_from_config("confirm_address_update")


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
