import asyncio
import json
import time

from app.ai.voice.tts.elevenlabs import ElevenLabsConfig, build_elevenlabs_tts


class _Socket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def wait_closed(self):
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


async def test_teardown_does_not_wait_for_elevenlabs_to_close_the_socket():
    tts = build_elevenlabs_tts(
        ElevenLabsConfig(
            api_key="test",
            url="wss://api.in.residency.elevenlabs.io",
            voice_id="voice",
            model="eleven_flash_v2_5",
        )
    )
    socket = _Socket()
    tts._websocket = socket
    started = time.monotonic()

    await tts._disconnect_websocket()

    assert time.monotonic() - started < 0.5
    assert socket.sent == [{"close_socket": True}]
    assert socket.closed
    assert tts._websocket is None
