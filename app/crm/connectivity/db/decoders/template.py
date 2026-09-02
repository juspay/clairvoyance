"""crm_channel_template rows -> domain shapes."""

from typing import Any, Mapping

from app.crm.connectivity.schemas.template import ApprovedTemplate, TemplateRead
from app.crm.shared.decode import jsonb_list


def decode_template(row: Mapping[str, Any]) -> TemplateRead:
    """One crm_channel_template row -> TemplateRead."""
    return TemplateRead(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        channel=row["channel"],
        provider_account_ref=row["provider_account_ref"],
        name=row["name"],
        language=row["language"],
        provider_template_id=row["provider_template_id"],
        category=row["category"],
        submitted_category=row["submitted_category"],
        category_updated_at=row["category_updated_at"],
        # Total for the same reason every jsonb read here is: the webhook
        # consumer decodes rows inside a batch, and one raise would strand
        # every letter beside it. jsonb_list makes the COLUMN total; the dict
        # filter finishes the job for this caller, because TemplateRead types
        # components as objects and a stored [1, 2] would raise in pydantic
        # one line later — defeating the guarantee at the layer above it.
        # The filter lives here, not in the shared helper: another module may
        # legitimately want a list of scalars out of a jsonb column.
        components=[c for c in jsonb_list(row["components"]) if isinstance(c, dict)],
        status=row["status"],
        status_updated_at=row["status_updated_at"],
        rejection_reason=row["rejection_reason"],
        quality=row["quality"],
        quality_updated_at=row["quality_updated_at"],
        last_synced_at=row["last_synced_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_approved_template(row: Mapping[str, Any]) -> ApprovedTemplate:
    """The send path's narrow read: the facts an adapter needs to send.

    Deliberately not TemplateRead — the send path runs per message and has no
    use for a components blob it will never render.
    """
    return ApprovedTemplate(
        id=str(row["id"]),
        name=row["name"],
        language=row["language"],
        provider_template_id=row["provider_template_id"],
        category=row["category"],
    )
