"""Session-start resolution of a template's custom-component defs.

Reads ``configurations.ui_catalog.custom_components`` (opt-in names) and
fetches the matching active ``ui_component`` rows scoped to the template's
reseller/merchant. Missing or inactive names are skipped with a warning —
a stale template narrows its catalog, it never fails the turn.

One indexed SELECT per turn; rows are immutable per version so a cache can
be added later without changing callers. Best-effort: any DB error resolves
to an empty overlay (the session degrades to built-ins, never breaks).
"""

from typing import Dict

from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.template.types import (
    CustomComponentDef,
    CustomComponentFlags,
    TemplateModel,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.ui_component import (
    get_ui_components_by_names,
)


def model_renderable(
    defs: Dict[str, CustomComponentDef],
) -> Dict[str, CustomComponentDef]:
    """The subset the MODEL may render in-thread via render_ui.

    ``overlay_only`` defs are client-side render targets (opened by an
    ``open_detail`` action in the widget's detail overlay): they ship on
    the session surface wire but never join the render_ui enum, coaching,
    or allowlist — the model cannot paint them into the conversation.
    """
    return {k: v for k, v in defs.items() if not v.flags.overlay_only}


async def resolve_custom_components(
    template: TemplateModel,
) -> Dict[str, CustomComponentDef]:
    """The CHAMELEON overlay for one session: name → resolved def."""
    ui_cat = template.configurations.ui_catalog if template.configurations else None
    names = list(getattr(ui_cat, "custom_components", None) or [])
    if not names:
        return {}
    try:
        rows = await get_ui_components_by_names(
            reseller_id=template.reseller_id,
            merchant_id=template.merchant_id,
            names=names,
        )
    except Exception:
        logger.error(
            f"custom_components fetch failed for template {template.name!r}; "
            "session proceeds with built-ins only",
            exc_info=True,
        )
        return {}
    defs: Dict[str, CustomComponentDef] = {}
    for row in rows:
        try:
            defs[row.name] = CustomComponentDef(
                name=row.name,
                version=row.version,
                props_schema=row.props_schema,
                flags=CustomComponentFlags.model_validate(row.flags or {}),
                render_def=row.render_def,
                prompt_hint=row.prompt_hint,
            )
        except ValidationError:
            logger.error(
                f"ui_component {row.name!r} (v{row.version}) has invalid "
                "flags; skipping",
                exc_info=True,
            )
    missing = set(names) - set(defs)
    if missing:
        logger.warning(
            f"template {template.name!r} opts into unknown/inactive "
            f"ui_components {sorted(missing)}; they are skipped"
        )
    return defs
