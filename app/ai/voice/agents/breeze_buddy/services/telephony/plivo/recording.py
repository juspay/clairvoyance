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
from app.ai.voice.agents.breeze_buddy.services.telephony.recording_download import (
    download_recording_via_ssrf,
)
from app.core.config.static import APP_BASE_URL, PLIVO_RECORDING_TIME_LIMIT
from app.database.accessor import get_lead_by_call_id

# Keys go only to Plivo's recording hosts (media.plivo.com and its regions,
# e.g. aps1.media.plivo.com) over https; any other URL is fetched without them.
_RECORDING_HOSTS = ["media.plivo.com"]


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
    """Download a Plivo recording into memory; keys go only to its call's account."""

    async def _auth() -> aiohttp.BasicAuth:
        lead = await get_lead_by_call_id(call_sid)
        if lead is None:
            raise LookupError(f"no lead for call {call_sid}")
        account = await lead_plivo_account(lead)
        return aiohttp.BasicAuth(account.auth_id, account.auth_token)

    return await download_recording_via_ssrf(
        recording_url,
        call_sid,
        provider="Plivo",
        recording_hosts=_RECORDING_HOSTS,
        make_auth=_auth,
    )
