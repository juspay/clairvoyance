"""Final-review assembly: draft -> real ``ReplaceTemplateRequest``.

Uses Breeze Buddy's canonical request model as the validator. Read-only
fields on ``TemplateModel`` (``id``, ``created_at``, etc.) are not part of
``ReplaceTemplateRequest`` and are stripped by its ``extra="ignore"``
config — so Blueprint's draft can be passed as-is.

Returns either a clean ``template_json`` (dumped dict) or a flat list of
Pydantic error strings suitable for ``state.validation_issues``.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.template.types import ReplaceTemplateRequest


def assemble_final_template(
    draft: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Round-trip ``draft`` through ``ReplaceTemplateRequest``.

    Returns ``(template_json, issues)``:

    * On success: ``(dumped_dict, [])``. The dumped dict is what we persist.
    * On validation failure: ``(None, [human_readable_error, ...])`` — the
      planner picks these up and decides which specialist to delegate to.
    """
    try:
        validated = ReplaceTemplateRequest.model_validate(draft)
    except ValidationError as err:
        return None, _format_errors(err)

    return validated.model_dump(mode="json", exclude_none=True), []


def _format_errors(err: ValidationError) -> list[str]:
    out: list[str] = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ())) or "<root>"
        msg = e.get("msg", "validation error")
        out.append(f"[pydantic] {loc}: {msg}")
    return out


__all__ = ["assemble_final_template"]
