"""The fourth verb's root — a run asks a connector to DO something.

`send.py` drives the adapters, `connectors.py` the onboarding and template
faces, `ingress.py` the inbound faces. This file drives the action faces, and
it reaches them THROUGH the registry `connectors.py` assembled — it never
imports a provider itself, which is why it needs no door of its own.

What lives here is only the generic half, and it is the same four steps for
every connector that will ever act:

    resolve the door  ->  find the action  ->  type its args  ->  perform

`args` arrives untyped, exactly as the onboard route receives an untyped body
and lets `request_model` type it one line later. Nothing here names a
connector, an action or a transport; adding `add_note` to Shopify, or a
Zendesk connector with `create_ticket`, touches no line of this file.

**Two failures, and they are not the same.** A defect — no such connector, no
such action, args that do not fit, a 4xx from the destination — raises
`ActionError`, and the caller parks: no number of retries produces a
different answer. A bad moment — a timeout, a 5xx, a 429 — raises anything
else, and the walker's retry ladder re-sends. Collapsing the two would either
park runs on a deploy blip or spend attempts on a permanent refusal.
"""

from typing import Any, Dict, List

from pydantic import ValidationError

from app.core.logger import logger
from app.crm.connectivity.connectors import CONNECTORS, ActionError
from app.crm.connectivity.db.accessors.installation import (
    get_installation_for_connector,
)


def action_names(connector_key: str) -> List[str]:
    """What this connector can do — the publish validator's read.

    Returned rather than a bare membership test so the validator can name the
    alternatives; an author who typed `add_tags` is one sentence away from the
    right word. An unknown connector answers [] rather than raising: "no such
    connector" is the caller's sentence to write, not this function's.
    """
    spec = CONNECTORS.get(connector_key)
    return sorted(spec.actions) if spec else []


def validate_action_args(
    connector_key: str, action: str, args: Dict[str, Any]
) -> List[str]:
    """PURE: what is wrong with these args, as field names — the publish
    validator's read, empty list when they fit.

    The same model perform_action types against, asked one step earlier, so
    an author hears "tags" while they are still editing instead of finding a
    parked run later. Field names only, no messages: the caller composes its
    own sentence around them and pydantic's wording ("Field required") reads
    oddly inside one.

    An unknown connector or action answers [] — the caller has already said
    that in its own words, and repeating it as an argument problem would be
    a second, worse sentence about the same mistake.
    """
    spec = CONNECTORS.get(connector_key)
    face = spec.actions.get(action) if spec else None
    if face is None:
        return []
    try:
        face.args_model.model_validate(args)
    except ValidationError as e:
        return _bad_fields(e)
    return []


def _bad_fields(error: ValidationError) -> List[str]:
    """PURE: the dotted field names pydantic refused, deduplicated in order."""
    seen: List[str] = []
    for detail in error.errors():
        name = ".".join(str(part) for part in detail["loc"])
        if name not in seen:
            seen.append(name)
    return seen


async def perform_action(
    merchant_id: str,
    connector_key: str,
    action: str,
    args: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """Do one thing through one connector, and report it as flat facts.

    ``args`` is the plan's, still untyped; ``context`` carries only what a
    transport may need for bookkeeping (the run and node ids behind the
    idempotency key) and never reaches the provider as data.

    Raises ActionError for anything a retry cannot fix.
    """
    spec = CONNECTORS.get(connector_key)
    if spec is None:
        raise ActionError(f"no connector '{connector_key}'")

    face = spec.actions.get(action)
    if face is None:
        known = ", ".join(sorted(spec.actions)) or "none"
        raise ActionError(
            f"connector '{connector_key}' has no action '{action}' (has: {known})"
        )

    installation = await get_installation_for_connector(merchant_id, connector_key)
    if installation is None:
        # Fail CLOSED, unconditionally: the door is the tenancy, and there is
        # no flag a face can set to be excused from it. A `getattr(face,
        # "needs_installation", True)` lived here and read as a per-provider
        # accommodation; it was a bypass flag on the one check the whole verb
        # rests on, which the fail-closed law does not admit.
        raise ActionError(
            f"merchant has no usable '{connector_key}' connection to act through"
        )

    try:
        typed = face.args_model.model_validate(args)
    except ValidationError as e:
        raise ActionError(
            f"action '{action}' got {e.error_count()} bad argument(s): "
            f"{'; '.join(_bad_fields(e))}"
        ) from e

    logger.info(
        f"action: {connector_key}.{action} for {merchant_id} via "
        + (f"installation {installation.id}" if installation else "no installation")
    )
    return await face.perform(merchant_id, installation, typed, context)


__all__ = ["action_names", "perform_action", "validate_action_args"]
