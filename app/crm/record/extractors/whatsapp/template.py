"""template.* — Meta's verdicts about a registered template.

Merchant-level letters (about="merchant"): a review names no person. The
values arrive flat (the door files Meta's value verbatim, no batched
arrays), so every field is a plain payload.* path — no derivers here.
"""

from typing import List

from app.crm.record.extractors.whatsapp.shared import _f
from app.crm.record.schemas import CatalogField

# Meta's template-review vocabulary (message_template_status_update.event),
# and the category/quality words the other template letters carry.
TEMPLATE_EVENTS = [
    "APPROVED",
    "IN_APPEAL",
    "PENDING",
    "REJECTED",
    "PENDING_DELETION",
    "DELETED",
    "DISABLED",
    "PAUSED",
    "LIMIT_EXCEEDED",
    "FLAGGED",
]
TEMPLATE_CATEGORIES = ["AUTHENTICATION", "MARKETING", "UTILITY"]
QUALITY_SCORES = ["GREEN", "YELLOW", "RED", "UNKNOWN"]


def _template_fields() -> List[CatalogField]:
    """The template a review letter is about: Meta's id, name, language."""
    return [
        _f(
            "payload.message_template_id",
            "number",
            "Template id",
            keyable=True,
            variable=True,
        ),
        _f("payload.message_template_name", "text", "Template name", variable=True),
        _f(
            "payload.message_template_language",
            "text",
            "Template language",
            variable=True,
        ),
    ]


def status_fields() -> List[CatalogField]:
    """Meta's verdict on a submitted template, with the reason when refused."""
    return [
        *_template_fields(),
        _f(
            "payload.event",
            "choice",
            "Review event",
            values=TEMPLATE_EVENTS,
            variable=True,
        ),
        _f("payload.reason", "text", "Rejection reason", variable=True),
    ]


def category_fields() -> List[CatalogField]:
    """Meta recategorized a template: what it was, what it is now."""
    return [
        *_template_fields(),
        _f(
            "payload.previous_category",
            "choice",
            "Previous category",
            values=TEMPLATE_CATEGORIES,
            variable=True,
        ),
        _f(
            "payload.new_category",
            "choice",
            "New category",
            values=TEMPLATE_CATEGORIES,
            variable=True,
        ),
    ]


def quality_fields() -> List[CatalogField]:
    """A template's quality score moved — the early warning before a pause."""
    return [
        *_template_fields(),
        _f(
            "payload.previous_quality_score",
            "choice",
            "Previous quality",
            values=QUALITY_SCORES,
            variable=True,
        ),
        _f(
            "payload.new_quality_score",
            "choice",
            "New quality",
            values=QUALITY_SCORES,
            variable=True,
        ),
    ]
