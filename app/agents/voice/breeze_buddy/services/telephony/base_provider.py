from abc import ABC, abstractmethod
from fastapi import WebSocket

from app.schemas import CallProvider


class VoiceCallProvider(ABC):
    """
    Abstract base class for voice call providers.
    """

    def __init__(self, config, aiohttp_session):
        self.config = config
        self.aiohttp_session = aiohttp_session
        self.completion_callback = None

    @abstractmethod
    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        """
        Handle the WebSocket connection for the voice provider.
        """

    @abstractmethod
    def make_call(self, customer_mobile_number: str, outbound_number: str):
        """
        Initiate a call.
        """

    def set_completion_callback(self, callback):
        """
        Set the callback function to be called when the call is completed.
        """
        self.completion_callback = callback
