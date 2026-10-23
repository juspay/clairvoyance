"""Ask Buddy to answer a thread (D41): a call to the answer route on the API
pods, where Buddy's turns run the way a widget message is answered.

app/crm never imports app.ai (rule 4), so the projector — and a hand back
or a lapsed handoff, which tell Buddy why it has the thread back — reach
Buddy over HTTP at ``APP_BASE_URL``, with the platform's RBAC access token:
short-lived and scoped to the thread's merchant, the S2S token shape
merchants already use. The route takes the request and returns at once; the
turn runs there. Never raises: like the widget, a request that never lands
is not retried — her next message asks again, for everything unanswered.
"""

from datetime import timedelta
from typing import Optional

from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.core.config.static import APP_BASE_URL
from app.core.logger import logger
from app.core.transport.http_client import create_http_client
from app.schemas import UserRole

ANSWER_PATH = "/agent/voice/breeze-buddy/inbox/threads/{thread_id}/answer"
#: The route only starts the turn, so a call this slow has failed.
ASK_TIMEOUT_SECONDS = 5.0
#: The token is for this one call.
TOKEN_LIFETIME = timedelta(minutes=1)
ACCEPTED = 202


def _token(merchant_id: str) -> str:
    return rbac_token_manager.create_access_token_with_rbac(
        user_id=f"merchant:{merchant_id}",
        username=f"merchant-{merchant_id}",
        role=UserRole.MERCHANT,
        reseller_ids=[],
        merchant_ids=[merchant_id],
        expires_delta=TOKEN_LIFETIME,
    )


async def ask_buddy(
    merchant_id: str, thread_id: str, reason: Optional[str] = None
) -> bool:
    """Ask Buddy to answer the thread; True when the route took it.
    ``reason`` (status.RESUME_*): why Buddy has it back — Buddy is told so
    in a turn of its own, her waiting messages with it."""
    log = logger.bind(merchant_id=merchant_id, thread_id=thread_id)
    if not APP_BASE_URL:
        log.warning("inbox: APP_BASE_URL is not set, so Buddy cannot be asked")
        return False
    url = APP_BASE_URL.rstrip("/") + ANSWER_PATH.format(thread_id=thread_id)
    try:
        async with create_http_client(timeout=ASK_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                json={"merchant_id": merchant_id, "reason": reason},
                headers={"Authorization": f"Bearer {_token(merchant_id)}"},
            )
    except Exception as e:  # noqa: BLE001 — asking never fails the caller
        log.warning(f"inbox: asking Buddy to answer failed ({type(e).__name__})")
        return False
    if response.status_code != ACCEPTED:
        log.warning(f"inbox: asking Buddy to answer got {response.status_code}")
        return False
    return True
