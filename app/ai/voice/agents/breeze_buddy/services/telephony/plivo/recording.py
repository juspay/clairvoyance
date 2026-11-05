"""
Plivo call recording: the ``<Record>`` element for the answer XML, and the
download of the finished recording.
"""

from html import escape as html_escape
from io import BytesIO
from typing import Optional
from urllib.parse import quote

import aiohttp

from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.account import (
    lead_plivo_account,
)
from app.core.config.static import (
    APP_BASE_URL,
    PLIVO_RECORDING_TIME_LIMIT,
)
from app.core.logger import logger
from app.core.security.ssrf import host_matches_allowlist
from app.core.transport.http_client import get_proxy_config
from app.database.accessor import get_lead_by_call_id

# Keys go only to Plivo's recording hosts (media.plivo.com and its regions,
# e.g. aps1.media.plivo.com) over https; any other URL is fetched without them.
_RECORDING_HOSTS = ["media.plivo.com"]


def _is_plivo_recording_url(recording_url: str) -> bool:
    """Is this recording on Plivo (keys needed), not our own copy?"""
    return recording_url.startswith("https://") and host_matches_allowlist(
        recording_url, _RECORDING_HOSTS
    )


def _recording_callback_url(call_uuid: str) -> str:
    """Where Plivo POSTs the finished session recording.

    ``call_uuid`` rides in the query string: the ``<Record>`` callback's
    documented params (RecordUrl, RecordingID, ...) do not promise CallUUID.
    """
    return (
        f"{APP_BASE_URL}/agent/voice/breeze-buddy/plivo/callback/details"
        f"?call_uuid={quote(call_uuid, safe='')}"
    )


def plivo_record_xml(call_uuid: str) -> str:
    """``<Record>`` element that records the whole call in the background.

    ``recordSession="true"`` returns immediately, so it must come BEFORE
    ``<Stream>`` (which holds the call via keepCallAlive). ``maxLength`` is
    set explicitly because the element defaults to 60s.
    """
    callback_url = html_escape(_recording_callback_url(call_uuid))
    return (
        f'<Record recordSession="true" recordChannelType="mono" '
        f'maxLength="{PLIVO_RECORDING_TIME_LIMIT}" '
        f'callbackUrl="{callback_url}" callbackMethod="POST"/>'
    )


async def download_call_recording(
    recording_url: str, call_sid: str
) -> Optional[BytesIO]:
    """
    Download a recording from Plivo directly into memory.

    Args:
        recording_url (str): The URL of the recording to download
        call_sid (str): The call SID; a recording on Plivo is fetched with
            its call's account

    Returns:
        Optional[BytesIO]: In-memory file object containing the recording, or None if download failed
    """
    try:
        auth = None
        if _is_plivo_recording_url(recording_url):
            lead = await get_lead_by_call_id(call_sid)
            if lead is None:
                raise LookupError(f"no lead for call {call_sid}")
            account = await lead_plivo_account(lead)
            auth = aiohttp.BasicAuth(account.auth_id, account.auth_token)

        # Get proxy configuration
        proxy_url = get_proxy_config()

        logger.info(f"Downloading Plivo recording from: {recording_url}")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                recording_url, auth=auth, proxy=proxy_url
            ) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to download Plivo recording. Status: {response.status}"
                    )
                    return None

                # Read the recording into memory
                audio_data = await response.read()
                audio_file = BytesIO(audio_data)

        logger.info(
            f"Successfully downloaded Plivo recording for call: {call_sid} ({len(audio_data)} bytes)"
        )
        return audio_file

    except Exception as e:
        logger.opt(exception=e).error(f"Error downloading Plivo recording: {e}")
        return None
