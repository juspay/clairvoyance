"""A built agent as the fields a merchant edits, and back again.

The prompt is the agent's behaviour, and a merchant editing it as one long
piece of Markdown is a merchant one stray heading away from breaking their own
assistant. The template studio's answer is to show the same content as named
fields — brand line, trust signals, the question that decides the sale — and
assemble the prompt from them. This is that, over HTTP.

Read and write are inverses of the vertical's own assembly, kept honest by
going through the same code onboarding uses. The shared parts of the prompt —
operating principles, UI rules, refusals — are never exposed here and never
rewritten: they come from the blueprint and stay identical across every
merchant, which is what makes a fleet-wide prompt change possible at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping

from fastapi import APIRouter, Depends, HTTPException

from app.ai.voice.agents.breeze_buddy.assist import profiles
from app.ai.voice.agents.breeze_buddy.assist.commerce.parse import fields_from_prompt
from app.ai.voice.agents.breeze_buddy.assist.engine.prompt_core import (
    replace_vertical_section,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.slots import (
    GENERIC_PROFILE,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding.service import (
    _configuration_dict,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry
from app.ai.voice.agents.breeze_buddy.assist.verticals import registry as verticals
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.core.security.authorization import require_role
from app.database.accessor.breeze_buddy.template import (
    get_template_by_id,
    replace_template,
)
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.fields import (
    AgentFieldsResponse,
    AgentFieldsUpdate,
    FieldSectionOut,
    FieldSpecOut,
)

router = APIRouter()

_FIELD_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]


async def _load(template_id: str, current_user: UserInfo):
    """The agent, its configuration as a mapping, and who built it.

    ``configurations`` is a model rather than a mapping, so it goes through
    onboarding's own conversion — the studio must read exactly what the
    builder wrote. The adapter comes off the MCP servers the template
    carries, which beats re-probing a merchant's site to answer a question
    the template already answers.
    """
    require_role(current_user, _FIELD_ROLES)
    template = await get_template_by_id(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    validate_reseller_access(current_user, reseller_id=template.reseller_id)
    if template.merchant_id:
        validate_merchant_access(current_user, merchant_id=template.merchant_id)

    configurations: Dict[str, Any] = _configuration_dict(template)
    servers = [
        str(server.get("name") or "")
        for server in ((configurations.get("mcp") or {}).get("servers") or [])
    ]
    adapter = registry.for_template(servers)
    vertical = verticals.for_request(adapter.vertical)
    return template, configurations, adapter, vertical


@router.get("/assist/agents/{template_id}/fields", response_model=AgentFieldsResponse)
async def read_fields(
    template_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> AgentFieldsResponse:
    """The agent's editable fields, in the sections its studio shows."""
    template, configurations, adapter, vertical = await _load(template_id, current_user)
    prompt = str((template.flow or {}).get("system_prompt") or "")
    values = fields_from_prompt(prompt, configurations)
    profile = profiles.resolve(adapter.slot_profile())
    return AgentFieldsResponse(
        template_id=template_id,
        platform=adapter.id,
        profile=profile.name,
        # Whether this agent has a studio at all. A site nobody recognises
        # gets the neutral field list, which is not a studio — it has no
        # assembly behind it and editing it would write nothing. The
        # console keys off this rather than off a platform name.
        editable=profile.name != GENERIC_PROFILE.name,
        sections=[
            *[
                FieldSectionOut(
                    key=section.key,
                    title=section.title,
                    brief=section.brief,
                    fields=[
                        FieldSpecOut(
                            key=entry.key,
                            label=entry.label,
                            hint=entry.hint,
                            many=entry.many,
                            kind=entry.kind,
                            example=entry.example,
                            item=entry.item,
                            group=entry.group,
                            index=entry.index,
                            readonly=entry.readonly,
                            values=list(values.get(entry.key) or []),
                        )
                        for entry in section.fields
                        if not entry.hidden
                    ],
                )
                for section in profile.sections
            ],
        ],
    )


@router.put("/assist/agents/{template_id}/fields", response_model=AgentFieldsResponse)
async def write_fields(
    template_id: str,
    body: AgentFieldsUpdate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> AgentFieldsResponse:
    """Reassemble this agent's prompt from edited fields, and save it.

    Only the merchant's own sections move. Everything shared is carried over
    from the live prompt untouched, so a save here can never drift one store's
    operating rules away from the fleet's.
    """
    template, configurations, adapter, vertical = await _load(template_id, current_user)
    flow: Dict[str, Any] = dict(template.flow or {})
    prompt = str(flow.get("system_prompt") or "")
    if not prompt:
        raise HTTPException(status_code=400, detail="This agent has no prompt to edit.")
    if profiles.resolve(adapter.slot_profile()).name == GENERIC_PROFILE.name:
        # No studio, no assembly to write through. Refusing beats accepting an
        # edit and silently dropping it.
        raise HTTPException(
            status_code=400,
            detail="This agent is edited as a prompt, not as fields.",
        )

    # Start from what the agent already says and lay the edits over it.
    # Anything not sent — a hidden field, or a field a console version does
    # not know about yet — keeps its live value instead of being blanked by
    # its own absence.
    fields: Dict[str, List[str]] = dict(fields_from_prompt(prompt, configurations))
    for key, values in (body.fields or {}).items():
        fields[key] = [str(value) for value in values if str(value).strip()]

    brand = vertical.brand_block_from_fields(
        fields,
        assistant_name=_one(fields, "assistant_name") or template.name,
        brand_name=_one(fields, "brand_line") or template.name,
        site_host=_one(fields, "domain"),
    )
    rebuilt = _replace_brand(prompt, brand)
    section = vertical.vertical_section(fields)
    if section:
        rebuilt = replace_vertical_section(rebuilt, vertical.skeleton, section)
    flow["system_prompt"] = rebuilt

    for key, value in vertical.widget_values(fields).items():
        if isinstance(value, Mapping):
            merged = dict(configurations.get(key) or {})
            merged.update(value)
            configurations[key] = merged
        else:
            configurations[key] = value

    saved = await replace_template(
        template_id=template.id,
        reseller_id=template.reseller_id,
        merchant_id=template.merchant_id,
        name=template.name,
        flow=flow,
        expected_payload_schema=template.expected_payload_schema,
        expected_callback_response_schema=template.expected_callback_response_schema,
        configurations=configurations or None,
        secrets=template.secrets,
        telephony_number_id=template.telephony_number_id,
        is_active=template.is_active,
        supported_channels=list(template.supported_channels or []),
        now=datetime.now(timezone.utc),
    )
    if saved is None:
        raise HTTPException(status_code=500, detail="Could not save the agent.")
    logger.info("assist fields saved", template_id=template_id, fields=len(fields))
    return await read_fields(template_id, current_user)


def _one(fields: Mapping[str, List[str]], key: str) -> str:
    values = fields.get(key) or []
    return str(values[0]).strip() if values else ""


def _replace_brand(prompt: str, brand: str) -> str:
    """Swap the brand block, leaving everything after it exactly as it was.

    Bounded by the next top-level heading rather than by a marker: the marker
    only exists in a blueprint, and this is a built agent whose marker was
    consumed the day it was created.
    """
    start = prompt.find("## Brand identity")
    if start < 0:
        return prompt
    end = prompt.find("\n## ", start + 1)
    tail = prompt[end:] if end > 0 else ""
    return brand.rstrip("\n") + "\n" + tail.lstrip("\n") if tail else brand


__all__ = ["router"]
