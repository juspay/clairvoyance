"""XyneSpaces flow_action, as a code-layer SPEC — one decode engine, one word.

A flow_action submission is the relay's answer after a recipient completes an
embedded form (an interview-availability picker, a consent screen, an address
capture). We SEND the form now, so the submission's identity fields are the
ones the sending plan and the walker wrote into ``context.flowJSON.data``:
the merchant is ``merchant_id`` (the run's own merchant, never free-typed by
the author) and the person is ``customer_mobile_number`` (the run's customer's
phone). So the entry is declared ``about="customer"`` (canon T13 col 14): the
identity role resolves the PM/Dev who filled the form back to the same
customer the run is for, and the run's wait wakes. Without the phone the real
callback could never reach a run (finding 3); the ask-slots step sends it, and
the walker fills it from the run's own customer (finding 11).

Nothing here reads the payload by hand at decode time. The declared fields are
the submission's stable shape — the action name, the submitted values object,
the context envelope, and the two fields we sent — and `selected_slots` is ONE
derive(): the slot keys (`values.slots_2026-10-15`) are date-named and
therefore unknowable in advance, so the engine's path grammar (which never
indexes or globs) cannot name them. That is exactly the ~10% a derive() is for,
and it gives a workflow ONE stable placeholder (`{selected_slots}`) regardless
of which dates the form showed.

Fixtures under tests/crm/fixtures/xyne/ pin every declared path.
"""

from typing import Any, Dict, List, Optional

from app.crm.record.extractors.engine import Deriver
from app.crm.record.schemas import (
    CatalogEntry,
    CatalogField,
    FieldType,
    IdentityRole,
)

SOURCE = "xyne"
GROUP = "XyneSpaces"

#: Every per-date answer key is named for its date (`slots_2026-10-15`), so the
#: date part is the suffix after this prefix. Spelled here once, the same reason
#: topics.py exists: the letter is the relay's shape, this word is theirs.
SLOT_KEY_PREFIX = "slots_"

#: The flowJSON.data fields the SENDING side writes (finding 1, 3, 11) and the
#: identity/merchant reads name. Spelled here once so the catalog and the walker
#: agree with inbound.py.
MERCHANT_ID_DATA = "payload.context.flowJSON.data.merchant_id"
CUSTOMER_MOBILE_DATA = "payload.context.flowJSON.data.customer_mobile_number"


# --- derive(): the one legitimate read --------------------------------------


def selected_slots(payload: Dict[str, Any]) -> Optional[str]:
    """The picked time slots across every date, one line per date.

    The submitted `values` object holds a `selected_dates` list and one
    `slots_<date>` list per date the form showed. The date keys are dynamic, so
    the engine's declarative variable map cannot name them; this derive() turns
    whatever is actually there into ONE persona-readable string a workflow can
    throw at a call node — ``2026-10-15: 10:00\\n2026-10-16: 09:00, 10:00``.
    Nothing is renamed or reworded; a date the recipient picked nothing for is
    simply left out, and an empty submission reads as None (the run's context
    drops a None before any template sees it).
    """
    values = payload.get("values")
    if not isinstance(values, dict):
        return None
    lines: List[str] = []
    for key in sorted(values):
        if not key.startswith(SLOT_KEY_PREFIX):
            continue
        picked = values[key]
        if not isinstance(picked, list) or not picked:
            continue
        date = key[len(SLOT_KEY_PREFIX) :]
        times = ", ".join(
            str(item) for item in picked if isinstance(item, (str, int, float))
        )
        if times:
            lines.append(f"{date}: {times}")
    return "\n".join(lines) or None


# --- the declaration ----------------------------------------------------------


def _field(
    path: str,
    type: FieldType,
    label: str,
    variable: bool = False,
    derived: bool = False,
    keyable: bool = False,
    identity: Optional[IdentityRole] = None,
) -> CatalogField:
    return CatalogField(
        path=path,
        type=type,
        label=label,
        variable=variable,
        derived=derived,
        keyable=keyable,
        identity=identity,
    )


ENTRIES: List[CatalogEntry] = [
    CatalogEntry(
        source=SOURCE,
        topic="flow_action",
        label="Form submitted",
        group=GROUP,
        layer="code",
        about="customer",
        fields=[
            _field(
                CUSTOMER_MOBILE_DATA,
                "phone",
                "Filler phone",
                variable=True,
                keyable=True,
                identity="phone",
            ),
            _field(MERCHANT_ID_DATA, "text", "Merchant id", variable=True),
            _field("payload.actionId", "text", "Action id", variable=True),
            _field("payload.type", "text", "Submission type", variable=True),
            _field(
                "payload.values.selected_dates", "list", "Selected dates", variable=True
            ),
            _field(
                "selected_slots", "text", "Selected slots", variable=True, derived=True
            ),
            _field("payload.context.messageId", "text", "Message id", variable=True),
            _field(
                "payload.context.conversationId",
                "text",
                "Conversation id",
                variable=True,
            ),
            _field("payload.context.userId", "text", "Filler user id", variable=True),
            _field(
                "payload.context.flowJSON.data.kind", "text", "Form kind", variable=True
            ),
        ],
    ),
]

DERIVERS: Dict[str, Deriver] = {
    "selected_slots": selected_slots,
}

__all__ = ["SOURCE", "ENTRIES", "DERIVERS"]
