"""
Where each telephony provider serves call recordings, and the host we keep our
own copies on.

A recording URL arrives on an unauthenticated provider callback. It is stored
on the lead, linked from the console, and fetched by the server with the
provider's credentials, so only a URL on the provider's own media host is
stored, sent credentials or fetched. The hosts are the *_RECORDING_HOSTS
static config keys (app/core/config/static.py); a provider moving its media
elsewhere shows up as "Refusing recording URL" errors, and the fix is that
provider's key.
"""

from typing import List
from urllib.parse import urlsplit

from app.core.config.static import (
    EXOTEL_RECORDING_HOSTS,
    PLIVO_RECORDING_HOSTS,
    SELF_HOSTED_RECORDING_HOSTS,
    TWILIO_RECORDING_HOSTS,
    VOBIZ_API_BASE_URL,
    VOBIZ_RECORDING_HOSTS,
)
from app.core.security.ssrf import host_matches_allowlist

# Besides VOBIZ_RECORDING_HOSTS over https, Vobiz's REST host on its own
# scheme: the local harness points VOBIZ_API_BASE_URL at an http stand-in.
_VOBIZ_API_HOST = urlsplit(VOBIZ_API_BASE_URL).hostname
_VOBIZ_API_SCHEME = urlsplit(VOBIZ_API_BASE_URL).scheme


def _is_plain(url: str) -> bool:
    """No userinfo, backslash or whitespace, so a browser and urlsplit agree on
    the host (a browser reads ``https://evil.com\\@media.vobiz.ai`` as
    evil.com; urlsplit as media.vobiz.ai)."""
    if "\\" in url or any(c.isspace() or ord(c) < 0x20 for c in url):
        return False
    try:
        return "@" not in urlsplit(url).netloc
    except ValueError:
        return False


def _https_on(url: str, hosts: List[str]) -> bool:
    return urlsplit(url).scheme == "https" and host_matches_allowlist(url, hosts)


def is_provider_recording_url(provider: str, url: str) -> bool:
    """Is ``url`` a recording on ``provider``'s own media host?"""
    if not _is_plain(url):
        return False
    provider = provider.lower()
    if provider == "vobiz":
        parts = urlsplit(url)
        if parts.hostname == _VOBIZ_API_HOST and parts.scheme == _VOBIZ_API_SCHEME:
            return True
    hosts = {
        "twilio": TWILIO_RECORDING_HOSTS,
        "plivo": PLIVO_RECORDING_HOSTS,
        "exotel": EXOTEL_RECORDING_HOSTS,
        "vobiz": VOBIZ_RECORDING_HOSTS,
    }.get(provider)
    return hosts is not None and _https_on(url, hosts)


def is_self_hosted_recording_url(url: str) -> bool:
    """Is ``url`` a recording on our own host?"""
    return _is_plain(url) and _https_on(url, SELF_HOSTED_RECORDING_HOSTS)
