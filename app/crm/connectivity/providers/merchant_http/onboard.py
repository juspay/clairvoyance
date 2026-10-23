"""The merchant_http door: a named account, its base URL and, optionally,
one auth header.

The handshake makes no provider call. What it does instead is refuse early:
the base URL crosses the egress guard HERE, at onboarding, so a merchant
who types an internal address hears it in the console rather than parking
every run of every plan that names the connector. The guard runs again on
every request at fire time (DNS answers change); this is the cheap check,
not the authoritative one.

Where things live — the same split as the Xyne door:

- ``external_account_id`` is the merchant's ``account_name`` for this
  endpoint, a plain token. It is ALSO the door's ``door_name``, so a plan
  picks this account with ``door_name: "<account_name>"`` — one name, one
  selection mechanism shared with every connector.
- ``door_data = {"base_url": ...}``: where the API lives is a fact about the
  door, not a secret.
- The secret lives in the `credentials` table and nowhere else. This module
  only declares WHICH keys the bundle holds (``AUTH_HEADER`` /
  ``AUTH_VALUE``); the shared onboarding sync writes that bundle into the
  vault and points ``credential_id`` at it. A door with no credential sends
  no auth header, which is honest: some endpoints are signed by network,
  not by header.
"""

import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.security.ssrf import SSRFError, validate_egress_url
from app.crm.connectivity.providers.base import ConnectorHandshakeError
from app.crm.connectivity.schemas.connector import OnboardResult
from app.crm.connectivity.schemas.message import CredentialBundle

# The bundle's two keys — spelled once, read by actions.py. They are the
# keys stored inside the credential row in the `credentials` table.
AUTH_HEADER = "auth_header"
AUTH_VALUE = "auth_value"

#: The door_data key for where the API lives — the one spelling actions.py
#: reads back.
BASE_URL = "base_url"

#: Headers the request sets itself. The merchant's auth header may never be
#: one of them: it would silently replace the idempotency key, the content
#: negotiation, or the host the SSRF guard checked.
PROTECTED_HEADERS = frozenset({"host", "accept", "idempotency-key"})

#: The account name rides plan documents as a door_name, so it is held to the
#: same plain-token shape migration 085's door_name CHECK enforces.
_ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: RFC 7230 token characters — what a header NAME may be made of.
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def normalised_base_url(raw: str) -> str:
    """PURE: the base URL as the door stores it — scheme and host lowered,
    no trailing slash, no query, no fragment. Raises ValueError with the
    sentence the console shows."""
    candidate = raw.strip()
    if "{" in candidate or "}" in candidate:
        raise ValueError("base_url may not contain placeholders")
    # An EMPTY query or fragment ("https://host#", "https://host?") splits to
    # "" and would pass the parsed check below: what the merchant typed would
    # be silently dropped. The characters themselves are refused.
    if "?" in candidate or "#" in candidate:
        raise ValueError("base_url may not carry a query or a fragment")
    parts = urlsplit(candidate)
    if parts.scheme.lower() != "https":
        raise ValueError("base_url must be https")
    if not parts.hostname:
        raise ValueError("base_url has no host")
    if parts.username or parts.password:
        raise ValueError("base_url may not carry credentials")
    path = parts.path.rstrip("/")
    return f"https://{parts.netloc.lower()}{path}"


class OnboardMerchantHttpRequest(BaseModel):
    """Body for POST /connectors/merchant_http/onboard."""

    merchant_id: str = Field(..., description="Tenant scope — required")
    account_name: str = Field(
        ...,
        description="The merchant's name for this endpoint, e.g. credit-line; "
        "a plan picks it with door_name",
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
        repr=False,
        description="The header's value, e.g. 'Bearer …' — vault only, never echoed",
    )
    display_label: Optional[str] = Field(
        None,
        description="What the merchant calls this endpoint in the console",
    )

    @field_validator("account_name")
    @classmethod
    def _plain_account_name(cls, value: str) -> str:
        value = value.strip()
        if not _ACCOUNT_NAME.match(value):
            raise ValueError(
                "account_name must be letters, digits, '.', '_' or '-' (max 64)"
            )
        return value

    @field_validator("auth_header")
    @classmethod
    def _a_header_we_do_not_set(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not _HEADER_NAME.match(value):
            raise ValueError("auth_header is not a valid header name")
        if value.lower() in PROTECTED_HEADERS:
            raise ValueError(
                f"auth_header may not be {value!r} — the request sets it itself"
            )
        return value

    @model_validator(mode="after")
    def _shape(self) -> "OnboardMerchantHttpRequest":
        if (self.auth_header is None) != (self.auth_value is None):
            raise ValueError("auth_header and auth_value come together, or not at all")
        self.base_url = normalised_base_url(self.base_url)
        return self


class MerchantHttpOnboarder:
    """The no-provider handshake: validate the address, keep the secret."""

    def identify(self, request: Any) -> Tuple[Optional[str], Optional[str]]:
        """PURE: the account id is the account name; no channel address."""
        return request.account_name, None

    async def gather(self, request: Any) -> OnboardResult:
        """Refuse an address the guard would refuse at fire time, then
        report the door. The bundle holds the auth header, or nothing — the
        same two keys the `credentials` row will store."""
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
            external_account_id=request.account_name,
            address=None,
            display_label=request.display_label or request.account_name,
            bundle=bundle,
            door_data={BASE_URL: request.base_url},
            door_name=request.account_name,
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
    "BASE_URL",
    "MerchantHttpOnboarder",
    "OnboardMerchantHttpRequest",
    "PROTECTED_HEADERS",
    "normalised_base_url",
]
