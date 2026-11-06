"""``/assist/template/{template_id}/fields``: an assistant as the form a
merchant edits on the Build page, and back again.

``GET`` reads the form's values out of the assistant itself (its prompt and
settings; nothing is stored beside it). ``PUT`` saves edits and rewrites only
the merchant's parts: every line of the prompt the form does not know is kept
as it is, so an assistant set up by hand loses nothing. Taken from #1209
(routers/.../assist/fields.py).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app.ai.voice.agents.breeze_buddy.assist.onboarding.service import (
    OnboardingFailure,
    read_fields,
    save_fields,
)
from app.ai.voice.agents.breeze_buddy.assist.verticals import registry as verticals
from app.ai.voice.agents.breeze_buddy.assist.verticals.fields import AssistFields
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_role
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.onboarding.fields import (
    TemplateField,
    TemplateFieldsResponse,
    TemplateFieldsSection,
    TemplateFieldsUpdateRequest,
)

router = APIRouter()

# The merchant edits its own assistant; the scope checks bind every caller to
# the assistant's reseller and merchant.
_FIELD_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]


@router.get(
    "/assist/template/{template_id}/fields", response_model=TemplateFieldsResponse
)
async def get_template_fields(
    template_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateFieldsResponse:
    """The assistant's form, section by section, with its values."""
    template = await _load(template_id, current_user)
    return _response(read_fields(template))


@router.put(
    "/assist/template/{template_id}/fields", response_model=TemplateFieldsResponse
)
async def update_template_fields(
    template_id: str,
    body: TemplateFieldsUpdateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateFieldsResponse:
    """Save edited fields and rewrite the assistant from them.

    409 for an assistant with no brand block (edit its prompt instead); 400
    for a field the form does not have or too many values.
    """
    template = await _load(template_id, current_user)
    if read_fields(template) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This assistant is edited as a prompt, not as fields.",
        )
    try:
        saved = await save_fields(template, body.fields)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except OnboardingFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "step": exc.step,
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            },
        ) from exc
    return _response(read_fields(saved))


async def _load(template_id: str, current_user: UserInfo) -> TemplateModel:
    require_role(current_user, _FIELD_ROLES)
    template = await get_template_by_id(template_id)
    # A template with no merchant is a reseller's blueprint, not an assistant.
    if template is None or not template.merchant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Assistant not found."
        )
    validate_reseller_access(current_user, reseller_id=template.reseller_id)
    validate_merchant_access(current_user, merchant_id=template.merchant_id)
    return template


def _response(fields: Optional[AssistFields]) -> TemplateFieldsResponse:
    if fields is None:
        return TemplateFieldsResponse()
    return TemplateFieldsResponse(
        sections=[
            TemplateFieldsSection(
                key=section.key,
                title=section.title,
                brief=section.brief,
                fields=[
                    TemplateField(
                        key=spec.key,
                        label=spec.label,
                        hint=spec.hint,
                        kind=spec.kind,
                        values=list(fields.get(spec.key) or []),
                    )
                    for spec in section.fields
                ],
            )
            for section in verticals.DEFAULT.fields.sections
        ]
    )


__all__ = ["router"]
