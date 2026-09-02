"""WhatsApp's onboarding face — Meta's Embedded Signup, end to end.

Everything vendor-shaped lives here so `connectivity/onboarding.py` can be
four generic steps: look up the merchant, ask this face for facts, write the
credential, write the door and its pipe. Reached only through
connectivity/connectors.py (boundary rule 11).

The ladder this walks is canon T11's: configured -> authenticated ->
subscribed -> heartbeat -> healthy. It gets as far as `subscribed` and says
so honestly; `heartbeat` needs an inbound event, which arrives on its own
once the ingress bay is live.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config.static import META_APP_ID, META_APP_SECRET
from app.core.logger import logger
from app.crm.connectivity.providers.base import ConnectorHandshakeError
from app.crm.connectivity.providers.meta.graph import GraphError, call, segment
from app.crm.connectivity.providers.whatsapp import TOKEN_KEY
from app.crm.connectivity.schemas import (
    CredentialBundle,
    HealthLevel,
    OnboardResult,
)

#: Ceiling on the phone-number page walk. Twenty-five per page, so this is
#: 500 numbers — far past any real WABA, and a hard stop if a cursor loops.
_MAX_PHONE_NUMBER_PAGES = 20

# The interim truth on a freshly subscribed WABA: Meta is now sending us
# events and there is nothing here that reads the ones it sends. Recorded as
# the health_why rather than left implicit, because the alternative is a row
# that says 'healthy' while receipts and inbound replies go nowhere.
_NO_RECEIVER_WHY = (
    "subscribed to the WABA; nothing consumes its events yet — delivery "
    "receipts, inbound replies and template status updates are not processed"
)


class WhatsappOnboardingError(ConnectorHandshakeError):
    """Meta refused a step of the handshake. Carries no token or code.

    Subclasses the port's declared type so onboarding.py can tell this — a
    refusal written for the merchant — from an unexpected exception whose
    text must not reach the API response.
    """


def _bundle_of(token: str) -> Dict[str, Any]:
    """The credential bundle this connector writes (canon T11 col 6).

    app_secret and verify_token join it when webhook verification becomes
    per-merchant; today one app secret verifies every bay, so it is config.
    """
    return {TOKEN_KEY: token}


async def exchange_code(code: str) -> str:
    """Embedded Signup code -> short-lived user access token.

    The one Graph call with no bearer token: client_id and client_secret ARE
    the authentication, which is why call() takes an optional token. They go
    in the POST body, never the query string — Meta accepts either, and the
    app secret must not reach a proxy access log.
    """
    body = await call(
        "POST",
        "oauth/access_token",
        form={
            "client_id": META_APP_ID,
            "client_secret": META_APP_SECRET,
            "code": code,
        },
    )
    token = body.get("access_token")
    if not token:
        raise WhatsappOnboardingError("Meta returned no access token for the code")
    return str(token)


async def exchange_for_long_lived(short_lived: str) -> Tuple[str, Optional[int]]:
    """Short-lived token -> (long-lived token, seconds until it expires).

    ``expires_in`` is returned rather than discarded, and that is the whole
    point of this signature. Meta's tokens run about sixty days; a door whose
    token_expires_at is NULL claims to hold a PERMANENT credential, and the
    refresh job that will exist watches non-NULL rows only. Dropping this
    number is how every WhatsApp connection dies silently on day sixty with a
    green light on the screen.

    None is still a legal answer — some business-integration tokens genuinely
    never expire — and it must mean that, not "we forgot to look".
    """
    body = await call(
        "POST",
        "oauth/access_token",
        form={
            "grant_type": "fb_exchange_token",
            "client_id": META_APP_ID,
            "client_secret": META_APP_SECRET,
            "fb_exchange_token": short_lived,
        },
    )
    token = body.get("access_token")
    if not token:
        raise WhatsappOnboardingError("Meta returned no long-lived access token")
    expires_in = body.get("expires_in")
    return str(token), int(expires_in) if expires_in else None


async def list_phone_numbers(waba_id: str, token: str) -> List[Dict[str, Any]]:
    """Every number on the account, following Meta's cursor pagination.

    Paged, not just the first response: Meta returns 25 by default and this
    list is what verify_phone_number checks against, so a business with more
    numbers than one page would have a legitimate number refused as "not on
    this account" — a wrong answer, not a slow one.

    The page walk is bounded. An unbounded `while next` is one malformed
    cursor away from spinning forever inside a request a person is waiting on.
    """
    numbers: List[Dict[str, Any]] = []
    path: Optional[str] = f"{segment(waba_id)}/phone_numbers"
    params: Optional[Dict[str, Any]] = {"limit": 100}
    for _ in range(_MAX_PHONE_NUMBER_PAGES):
        if path is None:
            break
        body = await call("GET", path, access_token=token, params=params)
        data = body.get("data")
        if isinstance(data, list):
            numbers.extend(item for item in data if isinstance(item, dict))
        paging = body.get("paging")
        path = paging.get("next") if isinstance(paging, dict) else None
        params = None
    return numbers


async def verify_phone_number(waba_id: str, phone_number_id: str, token: str) -> None:
    """Confirm the phone_number_id the browser sent really is on this WABA.

    The signup flow is client-driven, so both ids arrive from a page we do
    not control. Binding a number that is not on the account would build a
    door onto somebody else's endpoint.
    """
    numbers = await list_phone_numbers(waba_id, token)
    if not any(number.get("id") == phone_number_id for number in numbers):
        raise WhatsappOnboardingError(
            f"phone number {phone_number_id} is not on this WhatsApp Business "
            f"Account"
        )


async def subscribe(waba_id: str, token: str) -> None:
    """POST /{waba}/subscribed_apps — start receiving this account's events."""
    body = await call("POST", f"{segment(waba_id)}/subscribed_apps", access_token=token)
    if not body.get("success"):
        raise WhatsappOnboardingError("Meta did not confirm the webhook subscription")


async def unsubscribe(waba_id: str, token: str) -> None:
    """DELETE /{waba}/subscribed_apps — stop receiving them."""
    await call("DELETE", f"{segment(waba_id)}/subscribed_apps", access_token=token)


class WhatsappOnboarder:
    """The ConnectorOnboarder for WhatsApp."""

    def identify(self, request: Any) -> tuple:
        """Both ids arrive in the signup body, so they are known before Meta
        is called at all — which is what lets onboarding refuse a disabled
        door or a retired number without spending the one-shot code."""
        return request.waba_id, request.phone_number_id

    async def gather(self, request: Any) -> OnboardResult:
        """Walk Embedded Signup and report what it produced.

        The order is not arbitrary. The code is single-use, so everything
        cheap and refusable happens before it is spent — the caller has
        already checked the merchant for exactly this reason. After that:
        token, token again for a long-lived one, verify the number is really
        on the account, then subscribe.

        Subscription failure does NOT raise. The account is real and its
        token works; what we lack is the event stream. That is a DEGRADED
        door with a why, not a refused onboarding — refusing would leave the
        merchant with a spent code and nothing to show for it.
        """
        try:
            short_lived = await exchange_code(request.code)
            long_lived, expires_in = await exchange_for_long_lived(short_lived)
            await verify_phone_number(
                request.waba_id, request.phone_number_id, long_lived
            )
        except GraphError as e:
            # Meta's own words are logged; the merchant gets the step that
            # failed, never the provider's raw string (it may echo an input).
            logger.warning(f"whatsapp onboarding: handshake failed — {e.detail}")
            raise WhatsappOnboardingError(
                "could not complete the WhatsApp signup handshake"
            ) from e

        health_level: HealthLevel
        try:
            await subscribe(request.waba_id, long_lived)
            health_level, health_why = "subscribed", _NO_RECEIVER_WHY
        except (GraphError, WhatsappOnboardingError) as e:
            detail = e.detail if isinstance(e, GraphError) else str(e)
            logger.warning(f"whatsapp onboarding: subscribe failed — {detail}")
            health_level = "authenticated"
            health_why = "token verified but the webhook subscription failed"

        return OnboardResult(
            external_account_id=request.waba_id,
            address=request.phone_number_id,
            display_label=request.display_label,
            bundle=_bundle_of(long_lived),
            token_expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                if expires_in
                else None
            ),
            health_level=health_level,
            health_why=health_why,
        )

    async def revoke(self, bundle: CredentialBundle, external_account_id: str) -> None:
        """Tell Meta to stop sending this account's events.

        Best-effort by contract: a merchant disconnecting must not be blocked
        because Meta is unreachable. Without it, though, Meta keeps delivering
        webhooks for a merchant who left, and every one of them is attributed
        to a door that no longer wants them.
        """
        token = bundle.secret(TOKEN_KEY)
        if not token:
            logger.warning(
                "whatsapp disconnect: no usable token, leaving the Meta "
                "subscription in place"
            )
            return
        try:
            await unsubscribe(external_account_id, token)
        except GraphError as e:
            logger.warning(f"whatsapp disconnect: unsubscribe failed — {e.detail}")
