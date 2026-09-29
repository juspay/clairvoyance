"""
Vobiz media-stream serializer.

Vobiz's bidirectional <Stream> speaks Plivo's WebSocket protocol for 8 kHz
μ-law (start / media in; playAudio / clearAudio out), so pipecat's
PlivoFrameSerializer handles every frame. A dtmf event, if Vobiz sends one,
is read the same way (it is not on Vobiz's canonical stream-events page).
Only the REST hang-up differs: Vobiz's host, and X-Auth-ID / X-Auth-Token
headers instead of Basic auth.

Subclassing pipecat's own serializer keeps us on its contract across
upgrades (``setup()`` changed signature inside 1.x), which the community
``pipecat-vobiz`` package would not.
"""

import re
from typing import cast

import aiohttp
from pipecat.serializers.plivo import PlivoFrameSerializer

from app.core.config.static import VOBIZ_API_BASE_URL
from app.core.logger import logger
from app.core.transport.http_client import get_proxy_config

# The hang-up runs inside the output transport's stop(); a black-holed
# request must not hold pipeline teardown for aiohttp's 300 s default.
_HANGUP_TIMEOUT_SECONDS = 10

# The call id comes from the unauthenticated websocket start event and is
# spliced into an authenticated URL; yarl collapses "..", so anything but a
# UUID-shaped id could aim the DELETE at another Vobiz resource.
_CALL_ID_PATTERN = re.compile(r"[A-Za-z0-9-]+")


class VobizFrameSerializer(PlivoFrameSerializer):
    """PlivoFrameSerializer whose auto hang-up calls Vobiz's REST API."""

    async def _hang_up_call(self):
        # PlivoFrameSerializer.__init__ guarantees these whenever auto_hang_up
        # is on, which is the only path that reaches this method.
        auth_id = cast(str, self._auth_id)
        auth_token = cast(str, self._auth_token)
        call_id = cast(str, self._call_id)
        if not _CALL_ID_PATTERN.fullmatch(call_id):
            logger.warning(
                "Refusing to hang up Vobiz call with an unexpected call id: "
                f"{call_id!r}"
            )
            return
        endpoint = f"{VOBIZ_API_BASE_URL}/Account/{auth_id}/Call/{call_id}/"
        headers = {"X-Auth-ID": auth_id, "X-Auth-Token": auth_token}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_HANGUP_TIMEOUT_SECONDS)
            ) as session:
                # Never follow a redirect: aiohttp strips only Authorization on
                # a cross-origin hop, so X-Auth-* would reach the other host.
                # A 3xx lands in the failure branch below.
                async with session.delete(
                    endpoint,
                    headers=headers,
                    proxy=get_proxy_config(),
                    allow_redirects=False,
                ) as response:
                    if response.status == 204:
                        logger.debug(f"Successfully terminated Vobiz call {call_id}")
                    elif response.status == 404:
                        logger.debug(f"Vobiz call {call_id} already terminated")
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Failed to terminate Vobiz call {call_id}: "
                            f"Status {response.status}, Response: {error_text}"
                        )
        except Exception as e:
            logger.error(f"Failed to hang up Vobiz call {call_id}: {e!r}")
