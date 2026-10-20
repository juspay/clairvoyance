"""The merchant_http door: a base URL and, optionally, one auth header.

The handshake makes no provider call. What it does instead is refuse early:
the base URL crosses the egress guard HERE, at onboarding, so a merchant
who types an internal address hears it in the console rather than parking
every run of every plan that names the connector. The guard runs again on
every request at fire time (DNS answers change); this is the cheap check,
not the authoritative one.

The secret lives in the `credentials` table and nowhere else: the
installation row keeps the door's STABLE name (its ``external_account_id``
— for a merchant API that name is what a plan addresses, e.g. ``payments``)
and the base URL as data under ``door_data['base_url']``, so editing a URL
re-onboards the same name onto the same row and no published plan changes.
This module only declares WHICH keys the bundle holds (``AUTH_HEADER`` /
``AUTH_VALUE``); the shared onboarding sync writes that bundle into the
vault table and points ``credential_id`` at it.
A request for a merchant whose door carries no credential goes out with no
auth header, which is honest: some endpoints are signed by network, not by
header.
"""

import re as _re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

from app.core.security.ssrf import SSRFError, validate_egress_url
from app.crm.connectivity.providers.base import ConnectorHandshakeError
from app.crm.connectivity.schemas.connector import OnboardResult
from app.crm.connectivity.schemas.message import CredentialBundle

# The bundle's two keys — spelled once, read by actions.py. They are the
# keys stored inside the credential row in the `credentials` table.
AUTH_HEADER = "auth_header"
AUTH_VALUE = "auth_value"

# A header name must be an RFC 7230 *token* — no whitespace, no CR/LF, no ``:``
# (which would split name from value), nothing a hostile ``auth_header`` could
# smuggle a second header through. ``!#$%&'*+-.^_`|~`` plus ASCII alphanumerics.
_HTTP_TOKEN = _re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# In a header value, newlines and the other control characters are what would
# let a value stop reading as one header and start reading as instruction:
# request splitting. Reject C0 (``\\x00-\\x1f``) and DEL (``\\x7f``); SP and
# HTAB are legal inside a field value, so they are not in the set.
_CONTROL_CHARS = _re.compile(r"[\x00-\x1f\x7f]")

# Headers the ``request`` action sets itself (or that aiohttp sets for it) and
# that a credential must never be allowed to clobber: ``host`` would aim the
# egress guard at a different wall, and ``accept``/``idempotency-key`` are the
# action-issued slots the merchant's endpoint may legitimately rely on.
RESERVED_HEADERS = frozenset({"host", "idempotency-key", "accept"})


def normalised_base_url(raw: str) -> str:
    """PURE: the base URL as the door stores it — scheme and host lowered,
    no trailing slash, no query, no fragment. Raises ValueError with the
    sentence the console shows."""
    candidate = raw.strip()
    if "{" in candidate or "}" in candidate:
        raise ValueError("base_url may not contain placeholders")
    parts = urlsplit(candidate)
    if parts.scheme.lower() != "https":
        raise ValueError("base_url must be https")
    if not parts.hostname:
        raise ValueError("base_url has no host")
    if parts.query or parts.fragment:
        raise ValueError("base_url may not carry a query or a fragment")
    if parts.username or parts.password:
        raise ValueError("base_url may not carry credentials")
    path = parts.path.rstrip("/")
    return f"https://{parts.netloc.lower()}{path}"


def normalised_door_name(raw: str) -> str:
    """PURE: the door's stable name, as a plan will spell it — trimmed,
    no placeholders, no characters a URL-or-id-looking string would confuse.
    A name is what a plan pins, so it must be stable and addressable, never
    derived from the base URL (that is what lets a URL edit stay in place)."""
    name = raw.strip()
    if "{" in name or "}" in name:
        raise ValueError("door name may not contain placeholders")
    if "/" in name or ":" in name or " " in name:
        raise ValueError("door name may not contain /, : or spaces")
    return name


class OnboardMerchantHttpRequest(BaseModel):
    """Body for POST /connectors/merchant_http/onboard."""

    merchant_id: str = Field(..., description="Tenant scope — required")
    name: str = Field(
        ...,
        min_length=1,
        description=(
            "The door's stable name a plan addresses (`payments`, `returns`): "
            "unique per merchant, and never the base URL, so a URL edit is an "
            "in-place change no plan has to republish"
        ),
    )
    base_url: str = Field(
        ...,
        min_length=1,
        description="Where the merchant's API lives: https://host[/prefix]",
    )
    auth_header: Optional[str] = Field(
        None,
        min_length=1,
        description="Header name carrying the secret, e.g. Authorization",
    )
    auth_value: Optional[str] = Field(
        None,
        min_length=1,
        description="The header's value, e.g. 'Bearer …' — vault only, " "never echoed",
    )
    display_label: Optional[str] = Field(
        None,
        description="What the merchant calls this endpoint in the console",
    )

    @model_validator(mode="after")
    def _shape(self) -> "OnboardMerchantHttpRequest":
        if (self.auth_header is None) != (self.auth_value is None):
            raise ValueError("auth_header and auth_value come together, or not at all")
        if self.auth_header is not None:
            # aiohttp rejects non-token names when the request is sent; refuse a
            # header it would refuse there, at the console, and refuse one it may
            # NOT refuse — nothing may reach the wire named ``host`` (the egress
            # guard's aim) or the slots the request action itself reserves.
            if not _HTTP_TOKEN.match(self.auth_header):
                raise ValueError(
                    "auth_header must be a single HTTP header name — no spaces, "
                    "colons, or control characters"
                )
            if _CONTROL_CHARS.search(self.auth_value or ""):
                raise ValueError(
                    "auth_value may not contain control characters such as CR or LF"
                )
            if self.auth_header.lower() in RESERVED_HEADERS:
                raise ValueError(
                    f"auth_header {self.auth_header!r} is reserved and may not be set"
                )
        self.base_url = normalised_base_url(self.base_url)
        self.name = normalised_door_name(self.name)
        return self


class MerchantHttpOnboarder:
    """The no-provider handshake: validate the address, keep the secret."""

    def identify(self, request: Any) -> Tuple[Optional[str], Optional[str]]:
        """PURE: the provider's account id is the door's STABLE name (the one
        a plan addresses), never the base URL; no channel address."""
        return request.name, None

    async def gather(self, request: Any) -> OnboardResult:
        """Refuse an address the guard would refuse at fire time, then
        report the door. The bundle holds the auth header, or nothing — the
        same two keys the `credentials` row will store. The base URL is
        stored as door_data, NOT as external_account_id, so re-onboarding the
        same name edits the URL on the same row — a URL change is an in-place
        update no published plan has to republish."""
        try:
            await validate_egress_url(request.base_url)
        except SSRFError as e:
            raise ConnectorHandshakeError(f"base_url refused: {e}") from e
        bundle: Dict[str, Any] = {}
        if request.auth_header is not None:
            bundle = {
                AUTH_HEADER: request.auth_header,
                AUTH_VALUE: request.auth_value,
            }
        return OnboardResult(
            external_account_id=request.name,
            door_name=request.name,
            door_data={"base_url": request.base_url},
            address=None,
            display_label=request.display_label or request.base_url,
            bundle=bundle,
            token_expires_at=None,
            health_level="healthy",
        )

    async def revoke(self, bundle: CredentialBundle, external_account_id: str) -> None:
        """Nothing to tell the endpoint: there is no session to end."""
        return None

    async def resubscribe(
        self, bundle: CredentialBundle, external_account_id: str
    ) -> None:
        """No event stream of its own; nothing to turn back on."""
        return None


__all__ = [
    "AUTH_HEADER",
    "AUTH_VALUE",
    "MerchantHttpOnboarder",
    "OnboardMerchantHttpRequest",
    "normalised_base_url",
]
