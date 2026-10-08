"""Manifest row -> Cloud API request body. Assembly and normalisation only:
nothing here reads the database, decides a retry, or talks to Meta.
"""

import re
from typing import Any, Dict, List, Optional, Union

from app.crm.connectivity.channels import ConversationProfile
from app.crm.connectivity.schemas.message import (
    ButtonsBody,
    ImageBody,
    ListBody,
    SessionBodyType,
    TextBody,
)

# The wire key the flow_token rides under. Meta echoes it back inside the
# customer's submission, where record's extractor strips it by the SAME
# spelling (record/extractors/whatsapp/flow.py) — spelled twice because
# rule 12 forbids the import; a test pins the two equal.
FLOW_TOKEN_KEY = "flow_token"

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


def flow_button_indexes(components: List[Dict[str, Any]]) -> List[int]:
    """Where a template's FLOW buttons sit among its buttons, in order.

    Meta names a button component by POSITION and refuses the ENTIRE send
    (131009) when a flow button arrives unnamed. The walk lives HERE
    because BUTTONS/FLOW are Meta's vocabulary: the row rides to the route
    whole, and each provider face reads its own words out of it.

    EVERY position (a second left unnamed loses the whole message), from
    the FIRST BUTTONS component only (a second would restart at 0 and name
    the wrong button). Total: a malformed component answers [], never a
    raise.
    """
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("type", "")).upper() != "BUTTONS":
            continue
        buttons = component.get("buttons")
        if not isinstance(buttons, list):
            continue
        return [
            index
            for index, button in enumerate(buttons)
            if isinstance(button, dict)
            and str(button.get("type", "")).upper() == "FLOW"
        ]
    return []


def build_send_body(
    template_name: str,
    language: str,
    recipient: str,
    parameters: List[Dict[str, Any]],
    flow_button_indexes: Optional[List[int]] = None,
    flow_token: Optional[str] = None,
) -> Dict[str, Any]:
    """The Cloud API send body. Assembly only — ``parameters`` arrive already
    built and judged sendable by the adapter.

    ``language`` comes from the template registry (T23), which is the one
    place that knows which locale a merchant's template was approved in.

    A FLOW button must be named by a button component or Meta refuses the
    whole send (131009) and nothing reaches the customer.
    ``flow_button_indexes`` holds every such position; empty — every template
    but the flow ones — leaves the body as it has always been.

    ``flow_token`` comes back verbatim on the customer's submission, so the
    caller passes the id of the message that opened the form and the answer
    names its own question. One token for every button: it identifies the
    SEND, not which button she pressed. It is the component's only optional
    part, so a caller without one sends no parameters rather than a
    placeholder — Meta then records its own word, 'unused', which the read
    side discards.
    """
    components: List[Dict[str, Any]] = []
    if parameters:
        components.append({"type": "body", "parameters": parameters})
    for index in flow_button_indexes or []:
        button: Dict[str, Any] = {
            "type": "button",
            "sub_type": "flow",
            # A string, like every other button component's index.
            "index": str(index),
        }
        if flow_token:
            button["parameters"] = [
                {"type": "action", "action": {FLOW_TOKEN_KEY: flow_token}}
            ]
        components.append(button)
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


# ---------------------------------------------------------------------------
# Session sends — free-form replies inside the customer-service window
# ---------------------------------------------------------------------------


def _too_long(label: str, value: Optional[str], limit: int) -> Optional[str]:
    if value is not None and len(value) > limit:
        return f"{label} is {len(value)} characters, the limit is {limit}"
    return None


def session_body_problem(
    body: SessionBodyType, profile: ConversationProfile
) -> Optional[str]:
    """PURE: why ``body`` cannot go out on this channel as it stands, or None.

    The limits are the channel's (channels.py), checked HERE so a reply that
    would be refused by the provider is refused by us first — with a
    sentence naming the part that does not fit, where the provider would
    return a bare code. Whoever composes replies reads the same profile
    through contracts and shapes to fit; this is the backstop, not the plan.
    """
    if isinstance(body, TextBody):
        return _too_long("text", body.text, profile.text_max)
    if isinstance(body, ImageBody):
        return _too_long("caption", body.caption, profile.caption_max)
    problem = (
        _too_long("body", body.text, profile.interactive_body_max)
        or _too_long("header", body.header, profile.header_max)
        or _too_long("footer", body.footer, profile.footer_max)
    )
    if problem:
        return problem
    if isinstance(body, ButtonsBody):
        if len(body.buttons) > profile.max_reply_buttons:
            return (
                f"{len(body.buttons)} buttons, the limit is "
                f"{profile.max_reply_buttons}"
            )
        for button in body.buttons:
            problem = _too_long(
                "button title", button.title, profile.reply_button_title_max
            )
            if problem:
                return problem
        return None
    if len(body.rows) > profile.max_list_rows:
        return f"{len(body.rows)} rows, the limit is {profile.max_list_rows}"
    problem = _too_long("list button", body.button, profile.list_button_max) or (
        _too_long("section title", body.section_title, profile.list_row_title_max)
    )
    if problem:
        return problem
    for row in body.rows:
        problem = _too_long(
            "row title", row.title, profile.list_row_title_max
        ) or _too_long(
            "row description", row.description, profile.list_row_description_max
        )
        if problem:
            return problem
    return None


def _decorations(body: Union[ButtonsBody, ListBody]) -> Dict[str, Any]:
    """The optional header and footer an interactive message may carry."""
    extra: Dict[str, Any] = {}
    if body.header:
        extra["header"] = {"type": "text", "text": body.header}
    if body.footer:
        extra["footer"] = {"text": body.footer}
    return extra


def build_session_body(recipient: str, body: SessionBodyType) -> Dict[str, Any]:
    """The Cloud API body for one free-form reply. Assembly only — ``body``
    arrives already judged to fit (session_body_problem).

    ``context.message_id`` quotes the message this reply answers; it changes
    how the reply is shown, never who it goes to.
    """
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
    }
    if body.reply_to:
        payload["context"] = {"message_id": body.reply_to}
    if isinstance(body, TextBody):
        payload["type"] = "text"
        payload["text"] = {"preview_url": body.preview_url, "body": body.text}
        return payload
    if isinstance(body, ImageBody):
        image: Dict[str, Any] = {"link": body.url}
        if body.caption:
            image["caption"] = body.caption
        payload["type"] = "image"
        payload["image"] = image
        return payload
    if isinstance(body, ButtonsBody):
        interactive: Dict[str, Any] = {
            "type": "button",
            "body": {"text": body.text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b.id, "title": b.title}}
                    for b in body.buttons
                ]
            },
        }
    else:
        section: Dict[str, Any] = {
            "rows": [
                {"id": row.id, "title": row.title}
                | ({"description": row.description} if row.description else {})
                for row in body.rows
            ]
        }
        if body.section_title:
            section["title"] = body.section_title
        interactive = {
            "type": "list",
            "body": {"text": body.text},
            "action": {"button": body.button, "sections": [section]},
        }
    interactive.update(_decorations(body))
    payload["type"] = "interactive"
    payload["interactive"] = interactive
    return payload
