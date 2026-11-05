"""
Registry of in-repo pre-check functions.

A pre-check with ``type: "internal_function"`` names a key in
``PRE_CHECK_FUNCTIONS`` instead of an HTTP endpoint or an MCP tool. The function
runs in-process on the dispatch worker, which makes it the right home for checks
whose data already lives in our own database -- exposing those over HTTP just to
make them callable is a pointless network hop.

To add a new pre-check function:
1. Write an ``async def fn(ctx: PreCheckFunctionContext) -> bool`` below (or in
   its own module and import it here).
2. Add it to the ``PRE_CHECK_FUNCTIONS`` registry.
3. Reference it from a config with
   ``{"type": "internal_function", "function": "<key>", "function_args": {...}}``.

Contract: return ``True`` to let the call proceed, ``False`` to block it. A
raised exception, a timeout, or a non-bool return is NOT a block -- the executor
routes those through ``default_on_failure`` exactly like an HTTP failure, so a
buggy function fails open or closed by config rather than by accident.

What a block *does* to the lead (abort vs defer) is the config's decision via
``on_failure_action``, not the function's. That is why the return type is a
plain bool and not, say, a defer duration.

This mirrors ``handlers/internal/builtin_dispatcher.BUILTIN_HANDLERS`` -- same
flat-dict shape, same "import it and add an entry" workflow.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.logger import logger
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    count_recent_contacted_leads,
)
from app.schemas import LeadCallTracker

# The ``function_args`` value that means "evaluate across every merchant"
# rather than only the lead's own. Nothing in the framework interprets this --
# it is a convention each function opts into, and functions that have no
# merchant dimension simply ignore it.
ANY_MERCHANT = "*"


@dataclass
class PreCheckFunctionContext:
    """Everything a pre-check function is given.

    Note the two merchant ids, which are NOT the same thing:

    - ``merchant_id`` is the lead's own merchant.
    - ``args["merchant_id"]`` is the *scope* the config asked the function to
      evaluate over -- a specific merchant id, or ``"*"`` for "any merchant".

    A function that has a merchant dimension should read the scope from
    ``args`` and fall back to ``merchant_id``; see ``_resolve_merchant_scope``.
    """

    lead: LeadCallTracker
    template: Optional[TemplateModel]
    customer_mobile_number: Optional[str]
    merchant_id: Optional[str]
    reseller_id: str
    args: Dict[str, Any]
    payload: Dict[str, Any]


PreCheckFunction = Callable[[PreCheckFunctionContext], Awaitable[bool]]


def _resolve_merchant_scope(ctx: PreCheckFunctionContext) -> Optional[str]:
    """Return the merchant id to filter on, or ``None`` for the ``"*"`` scope.

    ``None`` means "do not filter by merchant at all" -- callers pass it
    straight through to a query's optional ``merchant_id`` argument.
    """
    scope = ctx.args.get("merchant_id", ctx.merchant_id)
    if scope == ANY_MERCHANT:
        return None
    return scope


def _coerce_positive_number(value: Any, default: float) -> float:
    """Read a numeric ``function_args`` value, falling back on anything odd.

    ``function_args`` values go through placeholder resolution, so a number
    written in config can reach us as the string ``"24"``.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


async def recent_contact_cooldown(ctx: PreCheckFunctionContext) -> bool:
    """Block the call if we already dialled this number inside the window.

    ``function_args``:
      - ``merchant_id``: a merchant id, or ``"*"`` to count contacts across
        every merchant under the reseller. Defaults to the lead's own merchant.
      - ``window_hours``: how far back to look. Defaults to 24.

    Returns ``True`` (proceed) when there is no recent contact. Pairs naturally
    with ``on_failure_action: "defer"`` -- the cooldown is a window that expires
    on its own, so retrying later is the correct response, not abandoning the
    lead.

    Raises on a lookup failure rather than guessing, so ``default_on_failure``
    decides. Silently returning "no recent contact" on a DB error would turn a
    transient blip into a duplicate call to a real customer.

    Excludes the lead itself (``exclude_lead_id``): the dispatch worker holds
    the lead lock while pre-checks run, and the count treats a locked row as
    in-flight contact — the lead would always count itself and block every
    dispatch.

    Also excludes the lead's own retry lineage (same ``request_id``) from
    the count: ``_retry_call`` inserts a new row per retry while the prior
    attempt keeps its ``call_initiated_time``, so without this exclusion a
    lead's own NO_ANSWER retry would count as a "recent contact" and the
    cooldown would silently defer-then-abort the merchant's retry ladder
    instead of only blocking genuinely separate contact attempts.

    Concurrency is covered by the count query's in-flight branch
    (``is_locked``/``PROCESSING``): a concurrent worker mid-dispatch on a
    *different* lead for the same phone counts as contact before any
    ``call_initiated_time`` lands.
    """
    if not ctx.customer_mobile_number:
        raise ValueError("lead payload has no customer_mobile_number")

    window_hours = _coerce_positive_number(ctx.args.get("window_hours"), 24.0)
    window_start = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    merchant_scope = _resolve_merchant_scope(ctx)

    count = await count_recent_contacted_leads(
        customer_mobile_number=ctx.customer_mobile_number,
        reseller_id=ctx.reseller_id,
        window_start=window_start,
        merchant_id=merchant_scope,
        exclude_request_id=ctx.lead.request_id,
        exclude_lead_id=ctx.lead.id,
    )
    if count is None:
        raise RuntimeError("recent-contact lookup failed")

    scope_label = ANY_MERCHANT if merchant_scope is None else merchant_scope
    logger.info(
        f"recent_contact_cooldown: {count} contact(s) in the last "
        f"{window_hours}h for merchant scope '{scope_label}' "
        f"(lead {ctx.lead.id})"
    )
    return count == 0


# Registry of pre-check function name -> function.
# Each function has signature: (ctx: PreCheckFunctionContext) -> bool
PRE_CHECK_FUNCTIONS: Dict[str, PreCheckFunction] = {
    "recent_contact_cooldown": recent_contact_cooldown,
}
