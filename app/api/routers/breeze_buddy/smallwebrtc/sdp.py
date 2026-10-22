"""SDP answer repair for ESP32 clients behind a NAT'd server.

pipecat's esp32 munging FILTERS the answer's ``a=candidate`` lines down to
the ones that mention the public host the device dialled. On a VM/pod that
only knows its private address (GCP 1:1 NAT, k8s), aiortc never produced a
candidate with the public IP, so the filter deletes every candidate and the
device receives an answer it cannot dial (ICE never starts). The device-side
workaround was to invent the candidate from the dialled IP and the ``m=audio``
port; this does the same thing once, on the server, where it belongs.
"""

import ipaddress
import re
from typing import Optional

_CANDIDATE_RE = re.compile(r"^a=candidate:", re.M)
_MEDIA_PORT_RE = re.compile(r"^m=audio (\d+) ", re.M)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def media_port(sdp: str) -> Optional[int]:
    """The port on the ``m=audio`` line, or None."""
    match = _MEDIA_PORT_RE.search(sdp)
    return int(match.group(1)) if match else None


def ensure_host_candidate(sdp: str, host: Optional[str]) -> str:
    """Return ``sdp`` with one host candidate for ``host`` when it has none.

    Leaves the answer untouched when it already carries a candidate, when
    ``host`` is not an IP literal (a candidate needs an address, not a
    name), or when no ``m=audio`` port can be read. Under 1:1 NAT the public
    IP plus the negotiated port is exactly where the device's packets land.
    """
    if not host or not _is_ip(host) or _CANDIDATE_RE.search(sdp):
        return sdp
    port = media_port(sdp)
    if port is None or port == 0:
        return sdp
    newline = "\r\n" if "\r\n" in sdp else "\n"
    body = sdp.rstrip("\r\n")
    candidate = f"a=candidate:1 1 udp 2130706431 {host} {port} typ host"
    return f"{body}{newline}{candidate}{newline}a=end-of-candidates{newline}"
