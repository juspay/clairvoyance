"""
Exotel recording download functionality
"""

from io import BytesIO
from typing import Optional

import aiohttp

from app.core.config.static import (
    EXOTEL_API_KEY,
    EXOTEL_API_TOKEN,
    EXOTEL_SUBDOMAIN,
    RECORDING_STORAGE_HOST,
)
from app.core.logger import logger
from app.core.network import fetch_bytes_from_allowed_host, host_matches_allowlist

# Only ever send Exotel creds to Exotel hosts (configured subdomain + exotel.com).
_EXOTEL_HOST_SUFFIXES = tuple(
    {
        EXOTEL_SUBDOMAIN.split("//")[-1].split("/")[0].strip() or "api.exotel.com",
        "exotel.com",
    }
)

# A recording that has been copied to our own storage is fetched back through
# this same function, with the lead's recording_url now pointing there rather
# than at the provider. So the storage host is reachable too — but the
# provider's credentials are not for it, and only ride when the URL really is
# the provider's.
_REACHABLE_HOST_SUFFIXES = _EXOTEL_HOST_SUFFIXES + (RECORDING_STORAGE_HOST,)


async def download_call_recording(
    recording_url: str, call_sid: str
) -> Optional[BytesIO]:
    """
    Download a recording from Exotel directly into memory.

    Args:
        recording_url (str): The URL of the recording to download
        call_sid (str): The call SID for logging purposes

    Returns:
        Optional[BytesIO]: In-memory file object containing the recording, or None if download failed
    """
    try:
        # Exotel recordings require basic authentication
        auth = (
            aiohttp.BasicAuth(EXOTEL_API_KEY, EXOTEL_API_TOKEN)
            if host_matches_allowlist(recording_url, list(_EXOTEL_HOST_SUFFIXES))
            else None
        )

        audio_data = await fetch_bytes_from_allowed_host(
            recording_url, allowed_host_suffixes=_REACHABLE_HOST_SUFFIXES, auth=auth
        )
        if audio_data is None:
            return None
        audio_file = BytesIO(audio_data)

        logger.info(
            f"Successfully downloaded Exotel recording for call: {call_sid} "
            f"({len(audio_data)} bytes)"
        )
        return audio_file

    except Exception as e:
        logger.error(f"Error downloading Exotel recording: {e}", exc_info=True)
        return None
