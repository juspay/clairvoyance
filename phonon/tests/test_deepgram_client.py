"""Phonon through a standard Deepgram client (the official SDK).

Clairvoyance uses its own NemotronSTTService, but Phonon's protocol follows
Deepgram's live format. This test keeps that claim honest: the official SDK
connects, sends media / KeepAlive / Finalize / CloseStream, and parses every
message Phonon sends into its typed models.
"""

import asyncio

from deepgram import AsyncDeepgramClient, DeepgramClientEnvironment
from deepgram.listen.v1.types import (
    ListenV1CloseStream,
    ListenV1Finalize,
    ListenV1KeepAlive,
    ListenV1Metadata,
    ListenV1Results,
)

RATE = 8000
FRAME = RATE // 10 * 2


async def _call(base: str) -> list:
    client = AsyncDeepgramClient(
        api_key="unused-until-pr-13",
        environment=DeepgramClientEnvironment(
            base=base.replace("ws://", "http://"), production=base, agent=base
        ),
    )
    received: list = []
    async with client.listen.v1.connect(
        model="nova-3-general",
        language="en",
        encoding="linear16",
        sample_rate=str(RATE),
        channels="1",
        interim_results="true",
    ) as connection:
        for _ in range(12):  # 1.2 s
            await connection.send_media(b"\0" * FRAME)
        await connection.send_keep_alive(ListenV1KeepAlive(type="KeepAlive"))
        await connection.send_finalize(ListenV1Finalize(type="Finalize"))
        async for message in connection:
            received.append(message)
            if isinstance(message, ListenV1Results) and message.is_final:
                break
        await connection.send_close_stream(ListenV1CloseStream(type="CloseStream"))
        async for message in connection:  # Metadata, then the socket closes
            received.append(message)
    return received


def test_the_deepgram_sdk_parses_everything_phonon_sends(server):
    received = asyncio.run(asyncio.wait_for(_call(server.base_url), 20))

    results = [m for m in received if isinstance(m, ListenV1Results)]
    partials = [r for r in results if not r.is_final]
    final = results[-1]
    # The fields a Deepgram-style client reads from each Results message:
    assert [p.channel.alternatives[0].transcript.count("c") for p in partials] == [1, 2]
    assert final.is_final and final.speech_final and final.from_finalize
    assert final.channel.alternatives[0].transcript.endswith("c2")
    assert (final.start, final.duration) == (0.0, 1.2)
    assert isinstance(received[-1], ListenV1Metadata)
