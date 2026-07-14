"""Centralized tool-call result pipeline — shared by MCP tools and Global HTTP functions.

Both channels post-process a tool result before it reaches the LLM. Historically
each seam hand-wrote its own post-processing and they drifted:

- Global HTTP (``http_handler.py``) did *projection* (``expected_response_schema``)
  then *transforms* (``response_transforms``), inline.
- MCP (``mcp/__init__.py``) did *transforms* then *ui-hint injection* (``ToolUiHint``),
  via ``_maybe_apply_transforms`` + ``_maybe_inject_ui_instructions``.

So MCP couldn't project and HTTP couldn't emit JIT UI, and the transform step
existed twice. This module is the single chokepoint both seams now route through,
applying the three features in one canonical order::

    projection  →  transforms  →  ui-hint

The reducer/injection engines (``session_state.py``) sit on the *arg/state* side and
are already shared; this module owns only the *result* side. It is channel-agnostic:
voice (pipecat FlowManager) and chat (``ChatAgent``) both dispatch through the same
handlers, so both inherit the pipeline for free.

Cycle-safety note: the ``handlers.transport.utils`` package eagerly loads
``field_resolver → template.types``. To avoid a load-order cycle we keep
``template.types`` out of module import time — annotations are deferred via
``from __future__ import annotations`` (TYPE_CHECKING-only imports) and the one
runtime dependency (``ToolUiTrigger``) is imported locally inside the ui-hint
helper. Mirrors the pattern in ``response_transform.py``.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union, cast

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.response_filter import (
    apply_response_schema,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.response_transform import (
    apply_response_transforms,
)
from app.core.logger import logger

if TYPE_CHECKING:
    from typing import Literal

    from app.ai.voice.agents.breeze_buddy.template.types import (
        ResponseTransform,
        ToolUiHint,
    )


def _projection_active(
    response_schema: Optional[Union[Dict[str, str], "Literal['full']"]],
) -> bool:
    """True when ``response_schema`` should actually project.

    An empty dict / ``None`` means "no projection", and the ``"full"`` sentinel
    means "pass the whole payload". Both are inert. Kept as one helper so the
    fast-skip in :func:`apply_result_pipeline_json_str` and the projection guard
    in :func:`apply_result_pipeline` can never disagree on what "no projection"
    means.
    """
    return bool(response_schema) and response_schema != "full"


def apply_result_pipeline(
    data: Any,
    *,
    tool_name: str = "",
    is_success: bool = True,
    response_schema: Optional[Union[Dict[str, str], "Literal['full']"]] = None,
    response_transforms: Optional[List["ResponseTransform"]] = None,
    ui_hint: Optional["ToolUiHint"] = None,
    args: Optional[Dict[str, Any]] = None,
) -> Any:
    """Apply the canonical result post-processing to a *parsed* tool payload.

    Order is ``projection → transforms → ui-hint``. Each stage is independent
    and no-ops when its config is absent, so a caller that passes only the
    features its channel supports gets exactly that channel's current behavior.

    Args:
        data: The parsed tool payload (dict / list / scalar). Scalars pass
            through every stage untouched.
        tool_name: Registered tool name, for log attribution only.
        is_success: Whether the tool call succeeded (2xx / non-error envelope).
            Projection + transforms run on success only — error bodies pass
            through verbatim so the LLM can read them. ui-hint honors its own
            ``ToolUiTrigger`` (see :func:`_apply_ui_hint`).
        response_schema: JMESPath projection map (or the ``"full"`` sentinel /
            empty, both meaning "don't project").
        response_transforms: In-place transform rules.
        ui_hint: JIT UI authoring guidance to splice into a dict payload.
        args: LLM call arguments, used to resolve ``\\`{placeholder}\\``` tokens
            in projection expressions.

    Returns:
        The processed payload. Projection may replace ``data`` with a new dict;
        transforms mutate a deep copy then rebind; ui-hint mutates the dict in
        place. Every stage is fail-open: on a projection / transform / ui-hint
        error the pre-stage payload is kept (never a partially-processed one)
        and a warning is logged. This matches the codebase's declarative-config
        philosophy (a stale template never breaks a live turn) and — critically —
        keeps a malformed schema from raising out of an MCP handler that has no
        outer try/except (see mcp/__init__.py's direct-HTTP tool handler).
    """
    # 1. Projection — narrow the payload to whitelisted fields. Success + object
    #    only; ``"full"`` / empty schema is an explicit passthrough. Fail-open:
    #    a bad schema (e.g. legacy placeholder syntax) logs and passes the
    #    payload through unfiltered rather than raising — projection is the only
    #    stage that can throw (apply_response_schema), and some callers dispatch
    #    it outside a try/except, so swallowing here is what keeps a stale
    #    template from crashing a voice turn.
    if (
        is_success
        and _projection_active(response_schema)
        and isinstance(data, (dict, list))
    ):
        try:
            # _projection_active guarantees a non-empty dict here (not "full"),
            # but that narrowing isn't visible to the type checker.
            data = apply_response_schema(
                data, cast(Dict[str, str], response_schema), args=args or {}
            )
        except Exception as e:
            logger.warning(
                f"[tool_pipeline] {tool_name!r} projection failed; "
                f"passing payload through unfiltered: {e}"
            )

    # 2. Transforms — mutate specific paths in place. Deep-copy first so a
    #    mid-loop failure leaves the pre-transform payload intact rather than
    #    handing the LLM a partially-transformed one. Capture the return value:
    #    a root-level (path="") transform rebinds to a new object and only
    #    propagates if picked up here.
    if is_success and response_transforms and isinstance(data, (dict, list)):
        try:
            transformed = copy.deepcopy(data)
            data = apply_response_transforms(transformed, response_transforms)
        except Exception as e:
            logger.warning(
                f"[tool_pipeline] {tool_name!r} response_transforms failed: {e}"
            )

    # 3. UI-hint — splice Sidekick JIT authoring guidance into a dict payload.
    if ui_hint is not None and isinstance(data, dict):
        data = _apply_ui_hint(data, ui_hint, is_success, tool_name)

    return data


def apply_result_pipeline_json_str(
    result: Any,
    *,
    tool_name: str = "",
    is_success: bool = True,
    response_schema: Optional[Union[Dict[str, str], "Literal['full']"]] = None,
    response_transforms: Optional[List["ResponseTransform"]] = None,
    ui_hint: Optional["ToolUiHint"] = None,
    args: Optional[Dict[str, Any]] = None,
) -> Any:
    """MCP-shaped variant: ``result`` is a JSON string under the tool envelope's
    ``data`` field. Parse → :func:`apply_result_pipeline` → re-encode.

    No-ops (returns ``result`` unchanged) when there is nothing to do, when the
    payload isn't a JSON string, or when it doesn't decode to an object — so
    plain-text responses and feature-free tools pass through byte-for-byte.
    Preserves the exact return-shape contract of the ``_maybe_apply_transforms``
    + ``_maybe_inject_ui_instructions`` pair it replaces (a JSON-encoded string
    the LLM sees under ``data``).
    """
    if not isinstance(result, str):
        return result

    if not (_projection_active(response_schema) or response_transforms or ui_hint):
        return result

    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return result
    if not isinstance(parsed, (dict, list)):
        return result

    processed = apply_result_pipeline(
        parsed,
        tool_name=tool_name,
        is_success=is_success,
        response_schema=response_schema,
        response_transforms=response_transforms,
        ui_hint=ui_hint,
        args=args,
    )

    try:
        return json.dumps(processed)
    except (TypeError, ValueError):
        return result


def _apply_ui_hint(
    data: Dict[str, Any],
    ui_hint: "ToolUiHint",
    is_success: bool,
    tool_name: str,
) -> Dict[str, Any]:
    """Splice a ``ToolUiHint`` into a dict payload (Sidekick JIT UI pattern).

    Keys added:
      * ``_ui_instructions`` — the free-text guidance string
      * ``_ui_examples`` — optional worked examples (each a dict)
      * ``_ui_skip`` — True when ``trigger == skip_ui``

    Trigger semantics (see ``ToolUiTrigger``):
      * ``skip_ui`` — always append ``_ui_skip`` (even on error), never the hint.
      * ``on_any`` — append the hint regardless of status.
      * ``on_success`` — append the hint only when ``is_success``.

    Errors are swallowed (logged) so a malformed hint never breaks a turn —
    matching the pre-refactor ``_maybe_inject_ui_instructions``.
    """
    # Local runtime import keeps template.types off this module's import path.
    from app.ai.voice.agents.breeze_buddy.template.types import ToolUiTrigger

    try:
        if ui_hint.trigger == ToolUiTrigger.SKIP_UI:
            data["_ui_skip"] = True
        elif ui_hint.trigger == ToolUiTrigger.ON_ANY or is_success:
            if ui_hint.instructions:
                data["_ui_instructions"] = ui_hint.instructions
            if ui_hint.examples:
                data["_ui_examples"] = [
                    ex.model_dump(exclude_none=True) for ex in ui_hint.examples
                ]
    except Exception as e:
        logger.warning(
            f"[tool_pipeline] {tool_name!r} ui_instructions inject failed: {e}"
        )
    return data
