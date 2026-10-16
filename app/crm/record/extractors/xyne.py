"""XyneSpaces flow_action, as a code-layer SPEC — one decode engine, one word.

A flow_action submission is the relay's answer after a recipient completes an
embedded form (an interview-availability picker, a consent screen, an address
capture). A submission is about the MERCHANT'S FORM, not a person we can name:
the body carries no phone, no email, no account id — only the platform's
`context.userId` for WHO filled it. There is no binding table from that userId
to a merchant customer, so resolution is impossible by design. The entry is
declared `about="merchant"` (canon T13 col 14): processed with customer NULL,
never quarantined, and every consumer still hears the letter — a workflow can
react to the form being completed and read the chosen values, just not tag a
person.

Nothing here reads the payload by hand at decode time. The declared fields are
the submission's stable shape — the action name, the submitted values object,
and the context envelope — and `selected_slots` is ONE derive(): the slot keys
(`values.slots_2026-10-15`) are date-named and therefore unknowable in advance,
so the engine's path grammar (which never indexes or globs) cannot name them.
That is exactly the ~10% a derive() is for, and it gives a workflow ONE stable
placeholder (`{selected_slots}`) regardless of which dates the form showed.

Fixtures under tests/crm/fixtures/xyne/ pin every declared path.
"""

from typing import Any, Dict, List, Optional

from app.crm.record.extractors.engine import Deriver
from app.crm.record.schemas import CatalogEntry, CatalogField, FieldType

SOURCE = "xyne"
GROUP = "XyneSpaces"

#: Every per-date answer key is named for its date (`slots_2026-10-15`), so the
#: date part is the suffix after this prefix. Spelled here once, the same reason
#: topics.py exists: the letter is the relay's shape, this word is theirs.
SLOT_KEY_PREFIX = "slots_"


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
) -> CatalogField:
    return CatalogField(
        path=path, type=type, label=label, variable=variable, derived=derived
    )


ENTRIES: List[CatalogEntry] = [
    CatalogEntry(
        source=SOURCE,
        topic="flow_action",
        label="Form submitted",
        group=GROUP,
        layer="code",
        about="merchant",
        fields=[
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
