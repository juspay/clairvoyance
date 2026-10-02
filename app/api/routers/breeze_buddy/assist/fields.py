"""An assistant as the fields a merchant edits, and back again.

``GET`` shows the vertical's form with the assistant's values; ``PUT`` saves
edits and rebuilds the assistant from its fields. The shared operating block
is never shown or rewritten here, so a save cannot move one store's rules
away from everyone else's. Taken from #1209 (routers/.../assist/fields.py),
reading the stored fields instead of parsing them back out of the prompt.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.ai.voice.agents.breeze_buddy.assist.engine.fields import AssistFields
from app.ai.voice.agents.breeze_buddy.assist.onboarding.service import (
    AssistantNotEditableError,
    OnboardingFailure,
    assistant_fields,
    save_assistant_fields,
)
from app.ai.voice.agents.breeze_buddy.assist.verticals import registry as verticals
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_role
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.fields import (
    AssistFieldsResponse,
    AssistFieldsUpdate,
    FieldOut,
    FieldSectionOut,
)

router = APIRouter()

# The merchant edits its own assistant; the scope checks bind every caller to
# the assistant's reseller and merchant.
_FIELD_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]


@router.get("/assist/agents/{template_id}/fields", response_model=AssistFieldsResponse)
async def read_fields(
    template_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> AssistFieldsResponse:
    """The assistant's form, section by section, with its values."""
    template = await _load(template_id, current_user)
    return _response(template.id, assistant_fields(template))


@router.put("/assist/agents/{template_id}/fields", response_model=AssistFieldsResponse)
async def write_fields(
    template_id: str,
    body: AssistFieldsUpdate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> AssistFieldsResponse:
    """Save edited fields and rebuild the assistant from them.

    409 for an assistant not made from fields (edit its prompt instead); 400
    for a field the form does not show or too many values.
    """
    template = await _load(template_id, current_user)
    try:
        saved = await save_assistant_fields(template, body.fields)
    except AssistantNotEditableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This assistant is edited as a prompt, not as fields.",
        ) from exc
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
    return _response(saved.id, assistant_fields(saved))


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


def _response(template_id: str, fields: AssistFields | None) -> AssistFieldsResponse:
    if fields is None:
        return AssistFieldsResponse(template_id=template_id, editable=False)
    profile = verticals.DEFAULT.fields
    return AssistFieldsResponse(
        template_id=template_id,
        editable=True,
        sections=[
            FieldSectionOut(
                key=section.key,
                title=section.title,
                brief=section.brief,
                fields=[
                    FieldOut(
                        key=spec.key,
                        label=spec.label,
                        hint=spec.hint,
                        many=spec.many,
                        kind=spec.kind,
                        example=spec.example,
                        group=spec.group,
                        index=spec.index,
                        values=list(fields.get(spec.key) or []),
                    )
                    for spec in section.fields
                    if not spec.hidden
                ],
            )
            for section in profile.sections
        ],
    )


__all__ = ["router"]
