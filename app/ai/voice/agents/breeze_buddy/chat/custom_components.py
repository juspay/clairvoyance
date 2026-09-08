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

    One exclusion:

    - ``overlay_only`` defs are client-side render targets (opened by an
      ``open_detail`` / intent action in the widget's detail overlay): they
      ship on the session surface wire but never join the render_ui enum,
      coaching, or allowlist — the model cannot paint them in-thread.
    - defs with NO ``render_def`` (registered for a merchant frontend that
      renders them itself) STAY renderable: the model may paint them and
      the op persists; ``_custom_components_wire`` still never ships them,
      so the generic widget is unaffected — only the merchant's own page,
      which draws them by name, ever paints the op.
    """
    return {k: v for k, v in defs.items() if not v.flags.overlay_only}


async def resolve_custom_components(
    template: TemplateModel,
) -> Dict[str, CustomComponentDef]:
    """The CHAMELEON overlay for one session: name → resolved def.

    Attribute-tolerant on purpose: the turn path hands this whatever it
    holds as the template (test doubles included), and a template that
    never opted in must cost nothing — no DB round-trip, empty overlay.
    """
    configurations = getattr(template, "configurations", None)
    ui_cat = getattr(configurations, "ui_catalog", None)
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
        logger.opt(exception=True).error(
            f"custom_components fetch failed for template {template.name!r}; "
            "session proceeds with built-ins only"
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
            logger.opt(exception=True).error(
                f"ui_component {row.name!r} (v{row.version}) has invalid "
                "flags; skipping"
            )

    missing = set(names) - set(defs)
    if missing:
        logger.warning(
            f"template {template.name!r} opts into unknown/inactive "
            f"ui_components {sorted(missing)}; they are skipped"
        )
    return defs
