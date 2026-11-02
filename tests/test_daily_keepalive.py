from pipecat.utils.shared import acquires, releases

from app.ai.voice.agents.breeze_buddy.utils.transport.daily_keepalive import (
    force_teardown_daily_client,
    hold_daily_client,
)


class _Client:
    def __init__(self):
        self.released = False

    @acquires("client")
    async def setup(self):
        pass

    @releases("client")
    async def cleanup(self):
        self.released = True

    async def leave(self):
        pass

    async def _leave(self):
        pass


async def test_teardown_releases_a_client_that_input_and_output_both_own():
    client = _Client()
    await client.setup()
    await client.setup()
    hold_daily_client(client)

    await client.cleanup()
    assert not client.released

    await force_teardown_daily_client(client)
    assert client.released
