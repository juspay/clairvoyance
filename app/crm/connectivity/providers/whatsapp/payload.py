"""Manifest row -> Cloud API request body. Assembly and normalisation only:
nothing here reads the database, decides a retry, or talks to Meta.
"""

import re
from typing import Any, Dict, List, Optional, Union

_NON_DIGITS = re.compile(r"\D")


def to_meta_recipient(address: str) -> Optional[str]:
    """E.164 in, Meta's digits-only form out.

    Stripping happens HERE and the stripped form is never persisted: one
    representation in the database, whatever each provider prefers at its
    own edge.
    """
    digits = _NON_DIGITS.sub("", address or "")
    # Deliberate parity with shared/normalize.py's ^\+[1-9][0-9]{6,14}$ (and
    # the platform_identity CHECK), so a number this system was willing to
    # store is never rejected here as an "invalid address". 15 is E.164's
    # ceiling; 7 is the real short end (Saint Helena, +290 plus 4 digits);
    # no country code starts with 0.
    if not 7 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return digits


# The value types str() renders faithfully. bool is refused below despite
# being an int subclass: str(True) is 'True', which no customer message
# means to say.
_TEXTABLE_TYPES = (str, int, float)


def build_parameters(variables: Dict[str, Any]) -> Union[List[Dict[str, Any]], str]:
    """Manifest variables -> Meta template body parameters, or the defect.

    Meta accepts two forms and the producer chooses by how it writes the keys:

      {"1": "Priya", "2": "ORD-42"}         -> positional, in numeric order
      {"customer_name": "Priya", ...}       -> named (parameter_name)

    A str return means the dict cannot be sent and says why — the caller
    logs it and refuses terminally with REASON_BAD_VARIABLES. Two defects
    earn that:

      · A value that is not text or a number. str() rendered a JSON null as
        the literal word 'None' inside a customer's message — corruption
        that LOOKS delivered. The defect names the key and type, never the
        value, which may be personal data.
      · Positional and named keys mixed. Meta takes one style per request,
        so no rendering is correct; guessing one only buys a round trip to
        the refusal this string already states.

    ASCII digits decide positional vs named, not str.isdigit(), which also
    accepts digit-CATEGORY characters like '²' that int() then refuses —
    turning a bad key into a mid-send exception instead of an outcome.
    """
    if not variables:
        return []
    items = [(str(key), value) for key, value in variables.items()]
    for key, value in items:
        if isinstance(value, bool) or not isinstance(value, _TEXTABLE_TYPES):
            return f"variable '{key}' is {type(value).__name__}, not text"
    positional = [key for key, _ in items if key.isascii() and key.isdigit()]
    if len(positional) == len(items):
        numbers = sorted(int(key) for key in positional)
        if numbers != list(range(1, len(numbers) + 1)):
            return (
                f"positional variables must be numbered 1 to {len(numbers)} "
                f"with no gaps, got {sorted(positional, key=int)}"
            )
        # Sorting as strings would put "10" before "2" and silently swap two
        # values in a customer's message.
        ordered = sorted(items, key=lambda item: int(item[0]))
        return [{"type": "text", "text": str(value)} for _, value in ordered]
    if positional:
        return "mixes positional and named template variables"
    return [
        {"type": "text", "parameter_name": key, "text": str(value)}
        for key, value in items
    ]


def build_send_body(
    template_name: str,
    language: str,
    recipient: str,
    parameters: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """The Cloud API send body. Assembly only — ``parameters`` arrive already
    built and judged sendable by the adapter.

    ``language`` comes from the template registry (T23), which is the one
    place that knows which locale a merchant's template was approved in.
    """
    components: List[Dict[str, Any]] = []
    if parameters:
        components.append({"type": "body", "parameters": parameters})
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    }
