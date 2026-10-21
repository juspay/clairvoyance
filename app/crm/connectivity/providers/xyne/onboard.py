"""Connecting a Xyne app — ``xyne:<appId>``.

The merchant hands over what their Xyne app already has: its id, its API
token, the secret Xyne signs its webhooks with, and (optionally) the base URL
it lives at. One provider call proves the token before anything is written;
onboarding.py then vaults the bundle and writes the door, keyed
``(merchant_id, "xyne", app_id)``.

What the vault holds (``credentials``, one custom row per app):
``base_url``, ``api_token``, ``webhook_secret``. The door itself holds no
secret — only the app id and a pointer to that row.

After onboarding, the merchant points the app's webhook in Xyne at
``/ingest/webhooks/xyne/<appId>``: the app id in the path is how the inbound
door finds THIS app's secret before it trusts a byte of the body.

Reached only through connectivity/connectors.py (boundary rule 11).
"""

import re
from typing import Any, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from app.crm.connectivity.providers.base import ConnectorHandshakeError
from app.crm.connectivity.providers.xyne import client
from app.crm.connectivity.schemas.connector import OnboardResult
from app.crm.connectivity.schemas.message import CredentialBundle

#: The bundle's keys — the one spelling inbound and actions read back.
BASE_URL = "base_url"
API_TOKEN = "api_token"
WEBHOOK_SECRET = "webhook_secret"

#: An app id rides in a URL path (/ingest/webhooks/xyne/<appId>) and in the
#: credential's name, so it is held to a plain token shape.
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: Short secrets make the webhook HMAC guessable; Xyne's own are far longer.
_MIN_SECRET_LENGTH = 16


class OnboardXyneRequest(BaseModel):
    """Body for POST /connectors/xyne/onboard."""

    merchant_id: str = Field(..., description="Tenant scope — required")
    app_id: str = Field(..., description="The Xyne app's id — the account id")
    api_token: str = Field(
        ..., min_length=1, repr=False, description="The app's Xyne API token"
    )
    webhook_secret: str = Field(
        ...,
        min_length=_MIN_SECRET_LENGTH,
        repr=False,
        description="The secret Xyne signs this app's webhooks with",
    )
    base_url: str = Field(
        client.DEFAULT_BASE_URL, description="Where the app's Xyne API lives"
    )
    display_label: Optional[str] = Field(
        None, description="What the merchant calls this app in the console"
    )

    @field_validator("app_id")
    @classmethod
    def _plain_app_id(cls, value: str) -> str:
        value = value.strip()
        if not _APP_ID.match(value):
            raise ValueError(
                "app_id must be letters, digits, '.', '_' or '-' (max 128)"
            )
        return value

    @field_validator("base_url")
    @classmethod
    def _xyne_base_url(cls, value: str) -> str:
        return client.normalize_base_url(value)


class XyneOnboarder:
    """Prove the token, then report the app as a door."""

    def identify(self, request: Any) -> Tuple[Optional[str], Optional[str]]:
        """PURE: the app id is known before the handshake; no address — a
        Xyne app is a door with no pipe."""
        return request.app_id, None

    async def gather(self, request: Any) -> OnboardResult:
        """One call: does Xyne accept this token at this base URL?

        A refusal is the merchant's to fix (wrong token, wrong host) and is
        raised in their words. A Xyne that cannot answer right now is raised
        too — writing a door we could not check would show green on a token
        that may never have worked.
        """
        try:
            await client.check_token(request.base_url, request.api_token)
        except client.XyneRefused as e:
            raise ConnectorHandshakeError(str(e)) from e
        except Exception as e:
            raise ConnectorHandshakeError(
                "Xyne could not be reached to check the API token — try again"
            ) from e
        return OnboardResult(
            external_account_id=request.app_id,
            address=None,
            display_label=request.display_label or f"Xyne app {request.app_id}",
            bundle={
                BASE_URL: request.base_url,
                API_TOKEN: request.api_token,
                WEBHOOK_SECRET: request.webhook_secret,
            },
            token_expires_at=None,
            health_level="healthy",
        )

    async def revoke(self, bundle: CredentialBundle, external_account_id: str) -> None:
        """Nothing to tell Xyne: the webhook URL is configured on their side,
        and a revoked door already refuses every callback for this app."""
        return None

    async def resubscribe(
        self, bundle: CredentialBundle, external_account_id: str
    ) -> None:
        """Nothing to turn back on — we never subscribed; the merchant set the
        webhook URL in Xyne."""
        return None


__all__ = [
    "API_TOKEN",
    "BASE_URL",
    "OnboardXyneRequest",
    "WEBHOOK_SECRET",
    "XyneOnboarder",
]
