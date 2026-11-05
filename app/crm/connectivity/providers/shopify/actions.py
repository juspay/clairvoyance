"""Shopify's action face — what a run can DO to an order.

The public surface of this package: three actions, their argument models, and
the one function that decides which transport carries them. Everything a
plan document ever names lives here (``add_tag``, ``add_note``,
``update_order`` and their args); everything about HOW the write travels
lives in a transport module beside this one and is deletable without
touching a document.

Reached only through connectivity/connectors.py (boundary rule 11).

**The two rules from ``ConnectorAction``, made concrete.**

*Args are the contract — the WHOLE contract.* ``AddTagArgs`` is
``{order_id, tags}`` because that is what tagging an order means: which
order, which tags. Both come from the plan, where ``order_id`` is written
as a ``{placeholder}`` the square resolves from the run's facts before this
file is ever called — the send node's law (canon T19 col 6: a send's
``variables`` map is EXACTLY what is posted) applied to the fourth verb.

The alternative was for this file to reach into ``context["facts"]`` and
guess the order id from a list of likely key names. It read as convenience
and was a provider file knowing the key names of outreach's context: the
same spill as #1050 in the other direction, and a heuristic that silently
picks the wrong id the day a producer names its own field differently. The
argument model IS the contract, so it carries everything the action needs.

The model is deliberately not the relay's envelope — no ``type``, no
``add_shopify_*`` spelling. A field added here for a transport's
convenience is a field every published plan would have to be republished
to lose.

*Responses are normalised.* Every transport returns ``{"ok": True}`` plus
whatever the action itself can promise. A caller's response paths are
written against those keys, so passing a transport's raw body through
would break every plan the day the transport changes.

**Where the shop comes from, and what that guarantees.** The installation's
``external_account_id``, and nothing else — see ``_door``. The door is the
only account identifier established independently of the caller, so the
guarantee "one merchant's action cannot address another's order" rests on a
row we wrote rather than on a convention between two services.
"""

from typing import Any, ClassVar, Dict, List, Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.crm.connectivity.providers.base import ActionError, ConnectorAction
from app.crm.connectivity.providers.shopify.via_nautilus import ViaNautilus
from app.crm.connectivity.schemas.connector import ConnectorInstallation


class AddTagArgs(BaseModel):
    """What tagging an order means: which order, which tags."""

    order_id: str = Field(..., min_length=1, description="Shopify's order id")
    tags: List[str] = Field(..., min_length=1, description="Tags to add, additively")


class AddNoteArgs(BaseModel):
    """What noting an order means: which order, what text."""

    order_id: str = Field(..., min_length=1, description="Shopify's order id")
    note: str = Field(..., min_length=1, description="Text appended to the order note")


class UpdateOrderArgs(BaseModel):
    """A tag, a note, or both, in one visit.

    Both are optional INDIVIDUALLY and at least one is required together —
    the shape a merchant actually asks for ("tag it CONFIRMED and write why")
    without forcing an author to add a second square for the second half.
    """

    order_id: str = Field(..., min_length=1, description="Shopify's order id")
    tags: List[str] = Field(default_factory=list, description="Tags to add, additively")
    note: Optional[str] = Field(None, description="Text appended to the order note")

    @model_validator(mode="after")
    def _needs_something_to_do(self) -> "UpdateOrderArgs":
        """A step that changes nothing is an author's mistake, not a no-op to
        perform quietly: it would POST, succeed, and leave the order exactly
        as it was. Refused at publish, where it is still a typo."""
        if not self.tags and not (self.note or "").strip():
            raise ValueError("update_order needs tags, a note, or both")
        return self


def _door(installation: Optional[ConnectorInstallation]) -> ConnectorInstallation:
    """The merchant's Shopify connection, or a refusal — which shop this
    action addresses, and the ONLY thing that answers it.

    There used to be a fallback here to the run's merchant_id, resting on the
    relay's convention that a shop domain IS the tenant id on this side. That
    is a coincidence, not a guarantee, and it was propped up by
    ``needs_installation = False`` — a bypass flag on the one door the whole
    verb rests on, which the fail-closed law does not admit and which this
    package's own onboarder made unnecessary the moment it started recording
    a shop. A merchant with no Shopify door now cannot act, is refused at
    PUBLISH rather than parked on the first run, and the guarantee "one
    merchant's action cannot address another's order" rests on a row instead
    of on an agreement between two services.
    """
    if installation is None:
        raise ActionError(
            "this merchant has no Shopify connection to act through — "
            "connect the shop first"
        )
    return installation


def _transport(installation: ConnectorInstallation) -> ViaNautilus:
    """Which carrier performs this shop's writes — THE seam.

    A shop clairvoyance onboarded itself holds a Shopify credential and will
    go direct; a shop that has not migrated holds none and travels by the
    relay that does hold its token. Reading the installation makes the
    migration per-shop and automatic: onboarding a shop flips it, with no
    plan republished, no version bump, and both kinds of shop working on the
    same day.

    Deliberately one function and not a branch spread through the module —
    when the last shop migrates this is the only fork to delete, alongside
    the transport file itself.
    """
    if installation.credential_id is None:
        return ViaNautilus()
    # Unreachable until the direct transport lands: nothing writes a Shopify
    # credential yet (onboard.py records the shop with credential_id NULL, as
    # the migration switch), so no installation reaches here holding one.
    raise ActionError(
        "this shop holds its own Shopify credential, and the direct "
        "transport is not built yet"
    )


def _run_ref(context: Dict[str, Any]) -> str:
    """The idempotency key for this visit: (run, square).

    Deterministic so a lease retry after a lost response is recognisable as
    the SAME write rather than a second one — the call node's deterministic
    lead id, applied to a different shape. A run id carries no colon, so a
    transport that needs the halves back can split on the first one.
    """
    return f"{context.get('run_id', '')}:{context.get('node_id', '')}"


class AddTag:
    """Add one or more tags to an order, additively."""

    args_model: ClassVar[Type[BaseModel]] = AddTagArgs

    async def perform(
        self,
        merchant_id: str,
        installation: Optional[ConnectorInstallation],
        args: BaseModel,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        # Already typed by actions.py against args_model; the check is the
        # narrowing, and a miss would be a registry wiring bug, not input.
        if not isinstance(args, AddTagArgs):
            raise ActionError("add_tag was handed the wrong argument model")
        door = _door(installation)
        return await _transport(door).add_tag(
            door.external_account_id, args.order_id, args.tags, _run_ref(context)
        )


class AddNote:
    """Append text to an order's note."""

    args_model: ClassVar[Type[BaseModel]] = AddNoteArgs

    async def perform(
        self,
        merchant_id: str,
        installation: Optional[ConnectorInstallation],
        args: BaseModel,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(args, AddNoteArgs):
            raise ActionError("add_note was handed the wrong argument model")
        door = _door(installation)
        return await _transport(door).add_note(
            door.external_account_id, args.order_id, args.note, _run_ref(context)
        )


class UpdateOrder:
    """Tag and note an order in one step — and one request.

    Not sugar over the other two: the relay's envelope carries both fields
    together, so this is a single POST, a single signature and a single
    retry. Two separate squares would be two of each, and a pair that can
    half-succeed — tagged but not noted — which is a state no author asked
    for and none can see.
    """

    args_model: ClassVar[Type[BaseModel]] = UpdateOrderArgs

    async def perform(
        self,
        merchant_id: str,
        installation: Optional[ConnectorInstallation],
        args: BaseModel,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(args, UpdateOrderArgs):
            raise ActionError("update_order was handed the wrong argument model")
        door = _door(installation)
        result = await _transport(door).update_order(
            door.external_account_id,
            args.order_id,
            args.tags,
            args.note,
            _run_ref(context),
        )
        # The action's own facts: what it DID, not what the carrier returned.
        return {**result, "tagged": list(args.tags), "noted": bool(args.note)}


#: The registry entry connectors.py hands to the ConnectorSpec. The keys are
#: the words a plan document may say, and nothing else is a Shopify action.
SHOPIFY_ACTIONS: Dict[str, ConnectorAction] = {
    "add_tag": AddTag(),
    "add_note": AddNote(),
    "update_order": UpdateOrder(),
}

__all__ = [
    "SHOPIFY_ACTIONS",
    "AddNote",
    "AddNoteArgs",
    "AddTag",
    "AddTagArgs",
    "UpdateOrder",
    "UpdateOrderArgs",
]
