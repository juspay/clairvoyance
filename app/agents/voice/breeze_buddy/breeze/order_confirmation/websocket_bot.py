import json
import os
import asyncio
from dotenv import load_dotenv
from twilio.http.http_client import TwilioHttpClient
from fastapi import WebSocket
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transcriptions.language import Language
from pipecat.transports.network.fastapi_websocket import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.services.google.tts import GoogleTTSService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.azure.llm import AzureLLMService
from pipecat_flows import NodeConfig, FlowsFunctionSchema, FlowManager
from twilio.rest import Client
from app.core.config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_MODEL,
    GOOGLE_CREDENTIALS_JSON,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_VOICE_SPEED,
)

load_dotenv(override=True)

class CustomTwilioFrameSerializer(TwilioFrameSerializer):
    async def _hang_up_call(self):
        logger.info("Skipping automatic hang-up from serializer.")
        pass

class OrderConfirmationBot:
    def __init__(self, ws: WebSocket, aiohttp_session):
        self.ws = ws
        self.aiohttp_session = aiohttp_session
        self.task: PipelineTask = None
        self.outcome = "unknown"
        self.context: OpenAILLMContext = None
        self.reporting_webhook_url = None
        self.twilio_client = Client(
            TWILIO_ACCOUNT_SID,
            TWILIO_AUTH_TOKEN,
            http_client=TwilioHttpClient(),
        )

    async def run(self):
        logger.info("Starting WebSocket bot")
        await self.ws.accept()

        start_data = self.ws.iter_text()
        await start_data.__anext__()
        call_data = json.loads(await start_data.__anext__())
        logger.info(f"Received call data: {call_data}")

        stream_sid = call_data["start"]["streamSid"]
        self.call_sid = call_data["start"]["callSid"]
        custom_parameters = call_data["start"]["customParameters"]

        order_id = custom_parameters.get("order_id", "N/A")
        customer_name = custom_parameters.get("customer_name", "Valued Customer")
        shop_name = custom_parameters.get("shop_name", "the shop")
        total_price = custom_parameters.get("total_price", "0")
        try:
            price_num = float(total_price)
            price_int = round(price_num)
            price_words = self._indian_number_to_speech(price_int)
        except (ValueError, TypeError):
            price_words = "the total price"
            logger.warning(f"Could not parse total_price: {total_price}")
        order_data_str = custom_parameters.get("order_data", "{}")
        order_data = json.loads(order_data_str)
        self.reporting_webhook_url = custom_parameters.get("reporting_webhook_url")
        logger.info(f"Parsed order_data: {order_data}")

        summary_parts = [
            f"{item.get('quantity', '')} {item.get('product_name', '')}"
            for item in order_data.get("items", [])
            if isinstance(item, dict)
        ]
        self.order_summary = ", ".join(summary_parts) or "your items"

        logger.info(
            f"Connected to Twilio call: CallSid={self.call_sid}, StreamSid={stream_sid}"
        )
        logger.info(
            f"Order Details: ID-{order_id}, Customer-{customer_name}, Summary-{self.order_summary}, Price-₹{total_price}"
        )

        serializer = CustomTwilioFrameSerializer(
            stream_sid=stream_sid,
            call_sid=self.call_sid,
            account_sid=TWILIO_ACCOUNT_SID,
            auth_token=TWILIO_AUTH_TOKEN,
        )

        transport = FastAPIWebsocketTransport(
            websocket=self.ws,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                vad_analyzer=SileroVADAnalyzer(),
                serializer=serializer,
            ),
        )

        stt = GoogleSTTService(
            params=GoogleSTTService.InputParams(
                languages=[Language.EN_US, Language.EN_IN],
                enable_interim_results=False,
            ),
            credentials=GOOGLE_CREDENTIALS_JSON,
        )
        llm = AzureLLMService(
            api_key=AZURE_OPENAI_API_KEY,
            endpoint=AZURE_OPENAI_ENDPOINT,
            model=AZURE_OPENAI_MODEL,
        )
        tts = ElevenLabsTTSService(
            api_key=ELEVENLABS_API_KEY,
            voice_id=ELEVENLABS_VOICE_ID,
            model_id=ELEVENLABS_MODEL_ID,
            params=ElevenLabsTTSService.InputParams(
                speed=ELEVENLABS_VOICE_SPEED, language=Language.EN_IN
            ),
        )

        self.system_prompt = self._get_system_prompt(
            shop_name, customer_name, order_id, self.order_summary, price_words
        )
        messages = [{"role": "system", "content": self.system_prompt}]

        self.context = OpenAILLMContext(messages)
        context_aggregator = llm.create_context_aggregator(self.context)

        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                context_aggregator.user(),
                llm,
                tts,
                transport.output(),
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

        flow_manager = FlowManager(
            task=self.task,
            llm=llm,
            context_aggregator=context_aggregator,
            transport=transport,
        )

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Client connected: {client}")
            await flow_manager.initialize(self._create_initial_node())

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info(f"Client disconnected: {client}")
            await self.task.cancel()

        runner = PipelineRunner()
        await runner.run(self.task)

    def _indian_number_to_speech(self, number: int) -> str:
        if number < 100:
            return f"{number} rupees"

        parts = []
        num_str = str(number)
        n = len(num_str)

        # Process last 3 digits (hundreds)
        if n >= 3:
            last_three = int(num_str[-3:])
            if last_three:
                parts.append(f"{last_three}")

        # Process thousands
        if n > 3:
            thousand = int(num_str[-5:-3]) if n >= 5 else int(num_str[-4:-3])
            if thousand:
                parts.insert(0, f"{thousand} thousand")

        # Process lakhs
        if n > 5:
            lakh = int(num_str[-7:-5]) if n >= 7 else int(num_str[-6:-5])
            if lakh:
                parts.insert(0, f"{lakh} lakh")

        # Process crores
        if n > 7:
            crore = int(num_str[:-7])
            if crore:
                parts.insert(0, f"{crore} crore")

        # Adjust hundreds format for last part
        if parts and int(parts[-1]) >= 100:
            h = int(parts[-1])
            h_part = f"{h // 100} hundred"
            rest = h % 100
            if rest:
                h_part += f" {rest}"
            parts[-1] = h_part

        return ' '.join(parts) + " rupees"

    def _get_system_prompt(
        self, shop_name, customer_name, order_id, order_summary, total_price_words
    ):
        return f"""
            You are Rhea, a friendly and natural-sounding voice assistant from {shop_name}. Your goal is to confirm a Cash on Delivery (COD) order with the customer.

            Before starting the conversation, identify whether the customer name "{customer_name}" is typically male or female in India. Based on this:
            - If the name is clearly male, address the customer as "Sir".
            - If clearly female, address them as "Madam".
            - If unsure, use their name instead of Sir/Madam in the conversation.

            Start the call by introducing yourself and stating the purpose of the call:
            "Hi {customer_name}, I'm Rhea calling from {shop_name} to confirm the order you placed. Is this a good time to talk, Sir/Madam/Name?"

            Your main job is to verify the following order details:
            - Items: {order_summary}
            - Total Price: {total_price_words}

            Speak in a warm, casual, and human-like tone. Avoid robotic language.
            Instead of: "Please confirm the following details."
            Say: "I have your order here, just wanted to quickly confirm the details with you, Sir/Madam/Name."

            You must use the following functions:
            - `confirm_order()`: If the customer confirms all the details.
            - `cancel_order()`: If the customer wants to cancel the order.
            - `user_busy()`: If the user says they are busy or it's not a good time to talk.
            - `ask_for_clarification()`: If the customer's response is unclear.

            If the user asks about things you don't know (e.g., other products, delivery times), gently guide them back to the confirmation.
            Example:
            User: "Do you have this in blue?"
            You: "I can't check that for you right now, but I can confirm your current order for {order_summary}, Sir/Madam/Name. Should I go ahead with that?"

            Your only role is to confirm or cancel this specific order.

            Always use "Sir", "Madam", or the customer's name depending on the gender identification as described above.
        """

    async def _end_conversation_handler(self, flow_manager, args):
        logger.info("Ending conversation.")
        try:
            # Send webhook with transcription history
            if self.context:
                history = self.context.messages
                transcription = []
                for msg in history:
                    if isinstance(msg, dict) and "role" in msg and "content" in msg and isinstance(msg["content"], str):
                        transcription.append(
                            {"role": msg["role"], "content": msg["content"]}
                        )
                summary_data = {
                    "call_sid": self.call_sid,
                    "transcription": transcription,
                    "outcome": self.outcome,
                }
                if self.reporting_webhook_url:
                    try:
                        async with self.aiohttp_session.post(
                            self.reporting_webhook_url, json=summary_data
                        ) as response:
                            if response.status == 200:
                                logger.info("Successfully sent call summary webhook.")
                            else:
                                logger.error(
                                    f"Failed to send call summary webhook. Status: {response.status}"
                                )
                    except Exception as e:
                        logger.error(f"Error sending webhook: {e}")

            self.twilio_client.calls(self.call_sid).update(status="completed")
            logger.info(f"Twilio call {self.call_sid} hung up successfully.")
        except Exception as e:
            logger.error(f"Failed to hang up Twilio call {self.call_sid}: {str(e)}")
        finally:
            await self.task.cancel()

    def _create_confirmation_node(self) -> NodeConfig:
        return NodeConfig(
            name="order_confirmation_and_end",
            task_messages=[
                {
                    "role": "system",
                    "content": f"The order is confirmed. Say: 'Thank you for confirming your order. Your order for {self.order_summary} will be delivered soon. Have a great day!'",
                }
            ],
            post_actions=[
                {"type": "function", "handler": self._end_conversation_handler}
            ],
        )

    def _create_cancellation_node(self) -> NodeConfig:
        return NodeConfig(
            name="order_cancellation_and_end",
            task_messages=[
                {
                    "role": "system",
                    "content": "The order is cancelled. Say: 'I understand you don't want to proceed with this order. Your order has been cancelled. Thank you for your time.'",
                }
            ],
            post_actions=[
                {"type": "function", "handler": self._end_conversation_handler}
            ],
        )

    async def _confirm_order_handler(self, flow_manager):
        logger.info("Order confirmed. Transitioning to confirmation node.")
        self.outcome = "confirmed"
        return {}, self._create_confirmation_node()

    async def _deny_order_handler(self, flow_manager):
        logger.info("Order denied. Transitioning to cancellation node.")
        self.outcome = "cancelled"
        return {}, self._create_cancellation_node()

    def _create_busy_node(self) -> NodeConfig:
        return NodeConfig(
            name="user_busy_and_end",
            task_messages=[
                {
                    "role": "system",
                    "content": "The user is busy. Say: 'I understand. I will call you back later. Thank you for your time.'",
                }
            ],
            post_actions=[
                {"type": "function", "handler": self._end_conversation_handler}
            ],
        )

    async def _user_busy_handler(self, flow_manager):
        logger.info("User is busy. Transitioning to busy node.")
        self.outcome = "busy"
        return {}, self._create_busy_node()

    def _create_initial_node(self) -> NodeConfig:
        return NodeConfig(
            name="initial",
            task_messages=[{"role": "system", "content": self.system_prompt}],
            functions=[
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
                )
            ],
        )


async def main(ws: WebSocket, aiohttp_session):
    bot = OrderConfirmationBot(ws, aiohttp_session)
    await bot.run()
