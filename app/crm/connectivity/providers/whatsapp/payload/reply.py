"""A free-form reply's Cloud API body, and the check that it fits the
channel's limits before it is built (the adapter refuses one that does not,
naming the part, where Meta would answer with a bare code)."""

from typing import Any, Dict, Optional, Union

from app.crm.connectivity.channels.registry import ConversationLimits
from app.crm.connectivity.schemas.message.whatsapp import (
    ButtonsBody,
    ImageBody,
    ListBody,
    ReplyBody,
    TextBody,
)


def _get_length_problem(label: str, value: Optional[str], limit: int) -> Optional[str]:
    if value is not None and len(value) > limit:
        return f"{label} is {len(value)} characters, the limit is {limit}"
    return None


def get_reply_body_limit_problem(
    body: ReplyBody, limits: ConversationLimits
) -> Optional[str]:
    """PURE: why ``body`` cannot go out on this channel as it stands, or None.

    The limits are the channel's (channels/registry.py), checked HERE so a reply that
    would be refused by the provider is refused by us first — with a
    sentence naming the part that does not fit, where the provider would
    return a bare code. Whoever composes replies reads the same limits
    through contracts and shapes to fit; this is the backstop, not the plan.
    """
    if isinstance(body, TextBody):
        return _get_length_problem("text", body.text, limits.max_text_chars)
    if isinstance(body, ImageBody):
        return _get_length_problem("caption", body.caption, limits.max_caption_chars)
    problem = (
        _get_length_problem("body", body.text, limits.max_interactive_body_chars)
        or _get_length_problem("header", body.header, limits.max_header_chars)
        or _get_length_problem("footer", body.footer, limits.max_footer_chars)
    )
    if problem:
        return problem
    if isinstance(body, ButtonsBody):
        if len(body.buttons) > limits.max_reply_buttons:
            return (
                f"{len(body.buttons)} buttons, the limit is "
                f"{limits.max_reply_buttons}"
            )
        for button in body.buttons:
            problem = _get_length_problem(
                "button title", button.title, limits.max_reply_button_title_chars
            )
            if problem:
                return problem
        return None
    if len(body.rows) > limits.max_list_rows:
        return f"{len(body.rows)} rows, the limit is {limits.max_list_rows}"
    problem = _get_length_problem(
        "list button", body.button_label, limits.max_list_button_label_chars
    ) or (
        _get_length_problem(
            "section title", body.section_title, limits.max_list_row_title_chars
        )
    )
    if problem:
        return problem
    for row in body.rows:
        problem = _get_length_problem(
            "row title", row.title, limits.max_list_row_title_chars
        ) or _get_length_problem(
            "row description", row.description, limits.max_list_row_description_chars
        )
        if problem:
            return problem
    return None


def _build_interactive_header_and_footer(
    body: Union[ButtonsBody, ListBody],
) -> Dict[str, Any]:
    """The optional header and footer an interactive message may carry."""
    extra: Dict[str, Any] = {}
    if body.header:
        extra["header"] = {"type": "text", "text": body.header}
    if body.footer:
        extra["footer"] = {"text": body.footer}
    return extra


def build_reply_send_body(recipient: str, body: ReplyBody) -> Dict[str, Any]:
    """The Cloud API body for one free-form reply. Assembly only — ``body``
    arrives already judged to fit (get_reply_body_limit_problem).

    ``context.message_id`` quotes the message this reply answers; it changes
    how the reply is shown, never who it goes to.
    """
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
    }
    if body.quoted_message_id:
        payload["context"] = {"message_id": body.quoted_message_id}
    if isinstance(body, TextBody):
        payload["type"] = "text"
        payload["text"] = {"preview_url": body.show_link_preview, "body": body.text}
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
            "action": {"button": body.button_label, "sections": [section]},
        }
    interactive.update(_build_interactive_header_and_footer(body))
    payload["type"] = "interactive"
    payload["interactive"] = interactive
    return payload
