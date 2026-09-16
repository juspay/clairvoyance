"""The merchant_http door: a base URL and, optionally, one auth header.

The handshake makes no provider call. What it does instead is refuse early:
the base URL crosses the egress guard HERE, at onboarding, so a merchant
who types an internal address hears it in the console rather than parking
every run of every plan that names the connector. The guard runs again on
every request at fire time (DNS answers change); this is the cheap check,
not the authoritative one.

The secret lives in the vault bundle and nowhere else: the installation
row keeps the base URL (its ``external_account_id`` — the provider's own id
for the account is, for a merchant API, its address) and a pointer to the
credential. A request for a merchant whose door carries no credential goes
out with no auth header, which is honest: some endpoints are signed by
network, not by header.
"""

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

from app.core.security.ssrf import SSRFError, validate_egress_url
from app.crm.connectivity.providers.base import ConnectorHandshakeError
from app.crm.connectivity.schemas.connector import OnboardResult
from app.crm.connectivity.schemas.message import CredentialBundle

# The bundle's two keys — spelled once, read by actions.py.
AUTH_HEADER = "auth_header"
AUTH_VALUE = "auth_value"


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


class OnboardMerchantHttpRequest(BaseModel):
    """Body for POST /connectors/merchant_http/onboard."""

    merchant_id: str = Field(..., description="Tenant scope — required")
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
        description="The header's value, e.g. 'Bearer …' — vault only, never echoed",
    )
    display_label: Optional[str] = Field(
        None, description="What the merchant calls this endpoint in the console"
    )

    @model_validator(mode="after")
    def _shape(self) -> "OnboardMerchantHttpRequest":
        if (self.auth_header is None) != (self.auth_value is None):
            raise ValueError("auth_header and auth_value come together, or not at all")
        self.base_url = normalised_base_url(self.base_url)
        return self


class MerchantHttpOnboarder:
    """The no-provider handshake: validate the address, keep the secret."""

    def identify(self, request: Any) -> Tuple[Optional[str], Optional[str]]:
        """PURE: the account id is the base URL itself; no channel address."""
        return request.base_url, None

    async def gather(self, request: Any) -> OnboardResult:
        """Refuse an address the guard would refuse at fire time, then
        report the door. The bundle holds the auth header, or nothing."""
        try:
            await validate_egress_url(request.base_url)
        except SSRFError as e:
            raise ConnectorHandshakeError(f"base_url refused: {e}") from e
        bundle: Dict[str, Any] = {}
        if request.auth_header is not None:
            bundle = {AUTH_HEADER: request.auth_header, AUTH_VALUE: request.auth_value}
        return OnboardResult(
            external_account_id=request.base_url,
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
