"""The store's form, mapped to and from the template it builds.

``brand_block_from_fields`` writes the answers into the prompt's brand block,
``widget_values`` the links they imply into the template's settings, and
``fields_from_template`` reads the answers back out of the prompt, so the
facts are kept only in the template itself.
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Optional, Sequence

from app.ai.voice.agents.breeze_buddy.assist.commerce.skeleton import COMMERCE_V2

# Where brand_block_from_fields writes each field, for fields_from_template
# to read it back from.
_BRAND_HEAD = "## Brand identity"
_IDENTITY_LABELS = {
    "Assistant name": "assistant_name",
    "Brand": "brand_line",
    "Tagline / positioning": "tagline",
}
_ONE_LINE_SECTIONS = {
    "What we sell": "what_we_sell",
    "Vocabulary register": "vocabulary",
    "Compliance note": "compliance",
}
_LIST_SECTIONS = {
    "Hero products": "hero_items",
    "Trust signals": "trust_items",
    "Active offers": "offer_items",
    "Returns and exchanges": "returns",
    "Delivery": "delivery",
    "Common questions": "faq",
    "Escalation channel": "escalation_extra",
}
_LABEL_LINE = re.compile(r"- \*\*(.+?):\*\* (.+)")
_WHATSAPP_LINE = re.compile(r"- Primary: \*\*WhatsApp / phone (.+)\*\*")
_EMAIL_LINE = re.compile(r"- Email: `(.+)`")


def _first(fields: Mapping[str, Sequence[str]], key: str) -> str:
    values = fields.get(key) or []
    return values[0] if values else ""


def _bullets(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value:
            out.append(value if value.startswith("- ") else f"- {value}")
    return out


def _section(title: str, lines: Sequence[str]) -> List[str]:
    """A ``###`` block, or nothing when it has nothing to say."""
    body = [line for line in lines if line]
    return [f"### {title}", "", *body, ""] if body else []


def _whatsapp_url(number: str) -> str:
    """A wa.me link, only for a number written with its country code ("+91
    98765 43210"): "080-31708114" or "98765 43210" would open the wrong chat."""
    if not number.strip().startswith("+"):
        return ""
    digits = "".join(character for character in number if character.isdigit())
    return f"https://wa.me/{digits}" if digits else ""


def brand_block_from_fields(fields: Mapping[str, Sequence[str]]) -> str:
    """The brand block written from the store's fields, under the headings
    the template studio uses (taken from #1209). A line or section with
    nothing in it is left out rather than shown empty: nothing is invented,
    the merchant fills it on the Build page."""
    assistant_name = _first(fields, "assistant_name")
    brand = _first(fields, "brand_line")
    tagline = _first(fields, "tagline")
    identity = [
        f"- **Assistant name:** {assistant_name}" if assistant_name else "",
        f"- **Brand:** {brand}" if brand else "",
        "- **Storefront:** `{shop_url}`",
        f"- **Tagline / positioning:** {tagline}" if tagline else "",
    ]
    hero = _bullets(fields.get("hero_items") or [])
    whatsapp = _first(fields, "whatsapp")
    email = _first(fields, "email")
    escalation = [
        f"- Primary: **WhatsApp / phone {whatsapp}**" if whatsapp else "",
        f"- Email: `{email}`" if email else "",
        *_bullets(fields.get("escalation_extra") or []),
    ]
    return "\n".join(
        [
            _BRAND_HEAD,
            "",
            *(line for line in identity if line),
            "",
            *_section("What we sell", [_first(fields, "what_we_sell")]),
            *_section("Hero products", hero),
            *_section("Trust signals", _bullets(fields.get("trust_items") or [])),
            *_section("Vocabulary register", [_first(fields, "vocabulary")]),
            *_section("Active offers", _bullets(fields.get("offer_items") or [])),
            *_section("Returns and exchanges", _bullets(fields.get("returns") or [])),
            *_section("Delivery", _bullets(fields.get("delivery") or [])),
            *_section("Common questions", _bullets(fields.get("faq") or [])),
            *_section("Compliance note", [_first(fields, "compliance")]),
            *_section("Escalation channel", escalation),
        ]
    ).rstrip()


def widget_values(fields: Mapping[str, Sequence[str]]) -> Dict[str, object]:
    """Configuration entries the fields imply, already config-shaped: the
    WhatsApp link joins the trusted links, so the assistant may show it as a
    button."""
    whatsapp = _whatsapp_url(_first(fields, "whatsapp"))
    return {"render_ui": {"trusted_link_urls": [whatsapp]}} if whatsapp else {}


def fields_from_template(prompt: str) -> Dict[str, List[str]]:
    """The fields read back out of a built template's prompt: the reverse of
    ``brand_block_from_fields``, so the facts are kept only in the template
    itself.

    Every line under a section this vertical writes comes back, even one
    written by hand. A label or heading it does not write (the storefront
    line, a hand-added section) is not a field and is skipped.
    """
    start = prompt.find(_BRAND_HEAD)
    end = prompt.find(COMMERCE_V2.operating_head, max(start, 0))
    block = prompt[start : end if end >= 0 else None] if start >= 0 else ""
    fields: Dict[str, List[str]] = {}
    heading: Optional[str] = None
    for line in (raw.strip() for raw in block.splitlines()[1:]):
        if not line:
            continue
        if line.startswith("### "):
            heading = line[4:].strip()
            continue
        if heading is None:
            label = _LABEL_LINE.fullmatch(line)
            if label and label.group(1) in _IDENTITY_LABELS:
                fields[_IDENTITY_LABELS[label.group(1)]] = [label.group(2)]
        elif heading in _ONE_LINE_SECTIONS:
            fields.setdefault(_ONE_LINE_SECTIONS[heading], []).append(line)
        elif heading in _LIST_SECTIONS:
            whatsapp = _WHATSAPP_LINE.fullmatch(line)
            email = _EMAIL_LINE.fullmatch(line)
            if heading == "Escalation channel" and whatsapp:
                fields["whatsapp"] = [whatsapp.group(1)]
            elif heading == "Escalation channel" and email:
                fields["email"] = [email.group(1)]
            else:
                fields.setdefault(_LIST_SECTIONS[heading], []).append(
                    line.removeprefix("- ")
                )
    for key in _ONE_LINE_SECTIONS.values():
        if key in fields:
            fields[key] = [" ".join(fields[key])]
    return fields


__all__ = ["brand_block_from_fields", "fields_from_template", "widget_values"]
