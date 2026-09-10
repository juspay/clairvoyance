"""The send-observer slot — who else needs to hear that a provider took a
message, filled at the composition root.

The dispatcher is the one place that learns a send's provider_message_id
(Meta's wamid), and the moment it does, the message's producer may need the
fact: outreach's listening squares match an inbound reply to ITS run by that
id (the call square's plant-and-echo pattern — a reply carries the wamid of
the message it answers, and nothing else). Connectivity may not import
outreach (outreach already reads this module's contracts; the reverse arrow
would close a cycle), so the slot is filled by app/crm/worker_main.py — the
record/consumers.py inversion, the retire-guard's exact shape.

Observers hear FACTS about the send connectivity already recorded — never a
verdict to influence: dispatch calls them after apply_outcome committed, and
an observer that raises is logged and dropped, because a bookkeeping
subscriber must never be able to fail, retry or double a customer's message.
"""

import asyncio
from typing import Awaitable, List, Optional, Protocol

from app.core.logger import logger

#: The lease-safety deadline on ONE observer call. _gate and send() are
#: wait_for-bounded precisely so a dispatch batch fits its claim lease; an
#: unbounded stamp behind them could burn the slack and hand the already-
#: accepted tail to another worker — a real double message. A cancelled
#: observer degrades to the designed no-stamp path (the reconcile read
#: recovers the id when it is needed).
OBSERVER_TIMEOUT_SECONDS = 5.0


class SendObserver(Protocol):
    """Keyword-called, so a wrong or renamed field is a TYPE error at the
    registration site, not a swallowed TypeError per send in production.
    source_kind rides through so the slot stays generic — each observer
    decides which producers are its business, exactly as record's consumers
    decide per letter."""

    def __call__(
        self,
        *,
        merchant_id: str,
        source_kind: str,
        source_id: Optional[str],
        dedupe_key: str,
        message_id: str,
        provider_message_id: str,
    ) -> Awaitable[None]: ...


_OBSERVERS: List[SendObserver] = []


def register_send_observer(observer: SendObserver) -> None:
    """Idempotent: imports can run more than once (tests, reload)."""
    if observer not in _OBSERVERS:
        _OBSERVERS.append(observer)


async def notify_send_accepted(
    *,
    merchant_id: str,
    source_kind: str,
    source_id: Optional[str],
    dedupe_key: str,
    message_id: str,
    provider_message_id: str,
) -> None:
    """Tell every registered observer the provider took this message.

    Called only for ACCEPTED outcomes that carry a provider id, and only
    after the outcome was recorded — an observer hears history, not a
    proposal. Total: one observer's raise is its own bug, logged with the
    message id and swallowed, so the batch behind this send keeps moving.
    """
    for observer in _OBSERVERS:
        try:
            await asyncio.wait_for(
                observer(
                    merchant_id=merchant_id,
                    source_kind=source_kind,
                    source_id=source_id,
                    dedupe_key=dedupe_key,
                    message_id=message_id,
                    provider_message_id=provider_message_id,
                ),
                timeout=OBSERVER_TIMEOUT_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 — a subscriber must not fail a send
            logger.opt(exception=e).error(
                f"send observer failed for message {message_id}"
            )
