"""The transport Shopify actions use until clairvoyance holds the tokens.

Nautilus already has every shop's offline token and already writes tags and
notes for the call path. Rather than duplicate that OAuth install, an action
POSTs a small envelope to nautilus's relay and lets it perform the write.

This file is the ONLY place in the system that knows nautilus exists in this
direction — the plan document, the node, the registry, the contract and the
validator all speak ``connector: "shopify", action: "add_tag"``. When the
tokens move, this file is deleted and ``actions._transport()`` loses its
fork; nothing above changes. That is the whole reason the envelope below is
built HERE and not authored anywhere.

The wire contract is nautilus's existing one, unchanged: compact JSON, an
HMAC-SHA256 of that exact text in a ``checksum`` header, keyed by
ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY — the same signer the call reporter
uses, because it is the same receiver verifying it.

Two things about that signature are load-bearing, and both are why this
sends ``content=`` rather than ``json=``:

- The receiver verifies over ``JSON.stringify(JSON.parse(body))``, so the
  bytes we sign must be the bytes we send, in the order we wrote them.
- Every value in the envelope is a string, a list of strings, or null.
  Numbers are deliberately excluded: Python renders 1.0 as ``1.0`` and
  JavaScript re-renders it as ``1``, and the signature would fail for a
  reason no log would explain. The Shopify order id is stringified for
  exactly this reason — the receiver does its own ``Number()`` on it.
"""

import json
from typing import Any, Dict, List, Optional

import httpx

from app.core.config.static import (
    CRM_ACTION_TIMEOUT_SECONDS,
    NAUTILUS_WEBHOOK_URL,
    ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY,
)
from app.core.logger import logger
from app.core.security.sha import calculate_hmac_sha256
from app.core.transport.http_client import create_http_client
from app.crm.connectivity.providers.base import ActionError
from app.crm.shared.redact import mask_digit_runs

#: The discriminator nautilus's shared webhook route branches on BEFORE its
#: own order lookup. Not vocabulary anyone else may read: it is this
#: transport's wire word, and it dies with this file.
_ENVELOPE_TYPE = "order_action"


class ViaNautilus:
    """Shopify writes performed by the relay that holds the shop's token."""

    async def add_tag(
        self, shop_domain: str, order_id: str, tags: List[str], run_ref: str
    ) -> Dict[str, Any]:
        return await self._post(
            shop_domain,
            order_id,
            run_ref,
            {"add_shopify_tag": [str(t) for t in tags], "add_shopify_note": None},
        )

    async def add_note(
        self, shop_domain: str, order_id: str, note: str, run_ref: str
    ) -> Dict[str, Any]:
        return await self._post(
            shop_domain,
            order_id,
            run_ref,
            {"add_shopify_tag": [], "add_shopify_note": str(note)},
        )

    async def update_order(
        self,
        shop_domain: str,
        order_id: str,
        tags: List[str],
        note: Optional[str],
        run_ref: str,
    ) -> Dict[str, Any]:
        """Both writes in ONE envelope.

        The relay's payload has always carried `add_shopify_tag` AND
        `add_shopify_note` together — the call-outcome path sends both at once
        — so doing a tag and a note as two POSTs would be this transport
        pretending it cannot do what it can. One request also means one
        signature, one idempotency key and one retry, rather than a pair that
        can half-succeed.
        """
        return await self._post(
            shop_domain,
            order_id,
            run_ref,
            {
                "add_shopify_tag": [str(t) for t in tags],
                "add_shopify_note": str(note) if note else None,
            },
        )

    async def _post(
        self,
        shop_domain: str,
        order_id: str,
        run_ref: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Sign and send one envelope; return the ACTION's facts, not the
        relay's body.

        Normalised on the way out (``{"ok": True}``) because a caller's
        response paths are written against these keys, and the day this
        transport is deleted those paths must still resolve.
        """
        if not NAUTILUS_WEBHOOK_URL:
            # Fail CLOSED, and as a DEFECT: no retry configures a URL, and a
            # run that silently did nothing would report success.
            raise ActionError(
                "shopify actions are not configured for this deployment "
                "(NAUTILUS_WEBHOOK_URL is unset)"
            )

        run_id, _, node_id = run_ref.partition(":")
        envelope: Dict[str, Any] = {
            "type": _ENVELOPE_TYPE,
            # The relay's wire name for the SHOP DOMAIN — its
            # getShopByDomain() key, not our tenant id. The provider's
            # own id for the account is what an installation stores.
            "merchant_id": shop_domain,
            # A string: see the module docstring on numbers and signatures.
            "shopify_order_id": str(order_id),
            **body,
            "run_id": run_id,
            "node_id": node_id,
        }
        # Compact and non-ASCII-preserving, matching JSON.stringify exactly.
        text = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
        headers = {
            "Content-Type": "application/json",
            # At-least-once with a deterministic key: a lease retry after a
            # lost response is the SAME write, and the receiver may say so.
            "Idempotency-Key": run_ref,
        }
        if not ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY:
            # Fail CLOSED, locally, and as a DEFECT — the same treatment as
            # an unset URL, because it is the same failure: this deployment
            # is not configured to perform Shopify actions. Posting unsigned
            # and letting nautilus answer 401 reaches the same parked run one
            # hop later, with "refused (401)" where a legible sentence
            # belongs, and spends a request to learn what is knowable here.
            raise ActionError(
                "shopify actions are not configured for this deployment "
                "(ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY is unset)"
            )
        headers["checksum"] = calculate_hmac_sha256(
            text, ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
        )

        async with create_http_client(timeout=CRM_ACTION_TIMEOUT_SECONDS) as client:
            # content=, never json=: the receiver re-serialises what it
            # parsed and compares, so it must parse the bytes we signed.
            response = await client.post(
                NAUTILUS_WEBHOOK_URL, content=text, headers=headers
            )

        if response.status_code >= 500:
            # A BAD MOMENT: the relay is up but unwell. Raise as itself so
            # the walker's ladder backs off and re-sends.
            raise RuntimeError(
                f"nautilus answered {response.status_code} to a shopify "
                f"{_ENVELOPE_TYPE}: {_excerpt(response)}"
            )
        if response.status_code >= 400:
            # A DEFECT: a refused signature, an unknown shop, an order that
            # is not there. Retrying spends attempts on the same answer.
            raise ActionError(
                f"nautilus refused the shopify action "
                f"({response.status_code}): {_excerpt(response)}"
            )

        logger.info(
            f"shopify action applied via nautilus for {shop_domain} "
            f"order {mask_digit_runs(str(order_id))} ({run_ref})"
        )
        return {"ok": True}


def _excerpt(response: httpx.Response, limit: int = 200) -> str:
    """The receiver's own sentence, truncated — usually the only thing an
    operator can act on. Never the whole body: it is unbounded and may carry
    a merchant's data into our logs."""
    try:
        text = response.text
    except Exception:  # pragma: no cover — a body that will not decode
        return ""
    return mask_digit_runs(text[:limit].strip())


__all__ = ["ViaNautilus"]
