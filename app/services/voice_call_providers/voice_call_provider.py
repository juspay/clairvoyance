from abc import ABC, abstractmethod
from typing import Optional
from fastapi import WebSocket
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData


class VoiceCallProvider(ABC):
    """
    Abstract base class for voice call providers.
    """

    def __init__(self, config, aiohttp_session):
        self.config = config
        self.aiohttp_session = aiohttp_session

    @abstractmethod
    async def handle_websocket(self, websocket: WebSocket):
        """
        Handle the WebSocket connection for the voice provider.
        """
        pass

    @abstractmethod
    def make_call(self, order: BreezeOrderData):
        """
        Initiate a call.
        """
        pass
