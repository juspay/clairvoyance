"""Connecting a Xyne app — ``xyne:<appId>``.

The merchant hands over what their Xyne app already has: its id, its API
token, the secret Xyne signs its webhooks with, and the base URL it lives at.
Before anything is written the token is proven to be THIS app's — its
``appId`` claim must equal ``app_id`` — and then proven live by one provider
call. onboarding.py then vaults the bundle and writes the door, keyed
``(merchant_id, "xyne", app_id)``. An app is one merchant's: the registry
marks Xyne ``exclusive_account``, so a second merchant naming the same app id
is refused rather than taking over its webhooks.

What the vault holds (``credentials``, one custom row per app):
``api_token``, ``webhook_secret``. The door holds no secret — the app id, a
pointer to that row, and ``door_data: {"base_url": ...}``, because where the
app's API lives is a fact about the door, not a credential.

After onboarding, the merchant points the app's webhook in Xyne at
``/ingest/webhooks/xyne/<appId>``: the app id in the path is how the inbound
door finds THIS app's secret before it trusts a byte of the body.

Reached only through connectivity/connectors.py (boundary rule 11).
"""

import base64
import binascii
import json
import re
from typing import Any, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from app.crm.connectivity.providers.base import ConnectorHandshakeError
from app.crm.connectivity.providers.xyne import client
from app.crm.connectivity.schemas.connector import OnboardResult
from app.crm.connectivity.schemas.message import CredentialBundle

#: The door_data key for where the app's API lives — the one spelling
#: actions.py reads back.
BASE_URL = "base_url"

#: The bundle's keys — the one spelling inbound and actions read back.
API_TOKEN = "api_token"
WEBHOOK_SECRET = "webhook_secret"

#: An app id rides in a URL path (/ingest/webhooks/xyne/<appId>) and in the
#: credential's name, so it is held to a plain token shape.
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: A door_name is written in plan documents (an action node's ``door_name``),
#: so it is a plain token — the same shape migration 085's CHECK enforces.
_DOOR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: Short secrets make the webhook HMAC guessable; Xyne's own are far longer.
_MIN_SECRET_LENGTH = 16

#: The JWT claim Xyne reads to learn which app a token belongs to (Apps
#: guide, "How Xyne verifies the token").
_APP_ID_CLAIM = "appId"


def token_app_id(api_token: str) -> Optional[str]:
    """PURE: the ``appId`` claim of a Xyne API token, or None when the token
    is not a JWT or carries no such claim.

    Read WITHOUT checking the JWT's signature — we do not hold Xyne's key.
    That is enough here because the claim is only half the proof: the token
    check then asks Xyne itself, which does verify the signature, so a forged
    claim on a token Xyne refuses never becomes a door.
    """
    parts = api_token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    app_id = claims.get(_APP_ID_CLAIM)
    return app_id if isinstance(app_id, str) and app_id else None


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
        ...,
        description="Where the app's Xyne API lives, e.g. "
        "https://spaces.xyne.juspay.net — stored as door_data.base_url",
    )
    display_label: Optional[str] = Field(
        None, description="What the merchant calls this app in the console"
    )
    door_name: Optional[str] = Field(
        None,
        description="Optional name a workflow action uses to pick THIS app "
        "(its door_name) when the merchant has several",
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

    @field_validator("door_name")
    @classmethod
    def _plain_door_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not _DOOR_NAME.match(value):
            raise ValueError(
                "door_name must be letters, digits, '.', '_' or '-' (max 64)"
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
        """Is this token THIS app's, and does Xyne accept it at this base URL?

        The claim is checked first and costs no call: a token that works
        somewhere but names another app is how one merchant would onboard
        another's app id, so it is refused outright. A refusal is the
        merchant's to fix (wrong token, wrong host) and is raised in their
        words. A Xyne that cannot answer right now is raised too — writing a
        door we could not check would show green on a token that may never
        have worked.
        """
        if token_app_id(request.api_token) != request.app_id:
            raise ConnectorHandshakeError(
                f"this API token does not belong to Xyne app '{request.app_id}'"
            )
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
                API_TOKEN: request.api_token,
                WEBHOOK_SECRET: request.webhook_secret,
            },
            door_data={BASE_URL: request.base_url},
            door_name=request.door_name,
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
    "token_app_id",
]
