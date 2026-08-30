"""Template-DEFINED direct intents (CHAMELEON).

Flavor packages register compiled-in :class:`IntentPolicy` rows; this module
builds the config-authored equivalent from
``configurations.ui_intents.custom`` — each entry becomes an ephemeral
DIRECT policy whose ``drive`` runs the configured template tools through the
same persisted pipeline (``inject_tool_args`` → dispatch → result pipeline →
reducers) and whose ``show_op`` hydrates a REGISTRY custom component against
this turn's binding store.

Nothing here is transport- or merchant-specific: which tools run, which
payload keys feed which args, and which component renders are all template
config. Policies are built per request (cheap — closures over parsed
config) and passed to ``parse_ui_intent`` as ``extra_policies``; they are
never written into the process-global flavor registry, so two templates can
define the same intent name without colliding.
"""

from typing import Any, AsyncIterator, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict

from app.ai.voice.agents.breeze_buddy.chat.intents.router import (
    IntentPolicy,
    IntentRoute,
    ParsedIntent,
    error_events,
    run_persisted_tool,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.chat.ui.binding import (
    parse_bind_ref,
    resolve_json_pointer,
)
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _is_tool_success,
)
from app.ai.voice.agents.breeze_buddy.template.types import CustomUiIntent
from app.core.logger import logger


class TemplateIntentPayload(BaseModel):
    """Permissive payload shell for template intents: structural validation
    is the step config's job (``args_from_payload`` fails closed on any
    missing key), so the wire gate only enforces object shape."""

    model_config = ConfigDict(extra="allow")


def _dig(payload: Dict[str, Any], path: str) -> Any:
    """Resolve one dot-path into the raw intent payload; None on any miss."""
    cur: Any = payload
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _apply_enrich(agent: Any, cfg: CustomUiIntent) -> None:
    """Run the config's cross-tool list-marking rules against this turn's
    binding store, mutating the recorded payloads in place (the store hands
    back the recorded object, so the show-op resolver sees the marks).
    Fail-open by contract — enrichment is presentation, never allowed to
    fail the intent."""
    for rule in cfg.enrich:
        try:
            list_ref = parse_bind_ref(rule.list_ref)
            equals_ref = parse_bind_ref(rule.equals_ref)
            if list_ref is None or equals_ref is None:
                logger.warning(
                    f"template_intent {cfg.name!r}: enrich rule has a bad "
                    f"bind ref; skipping"
                )
                continue
            list_payload = agent.binding_store.resolve(
                list_ref.tool_name, list_ref.tool_use_id
            )
            equals_payload = agent.binding_store.resolve(
                equals_ref.tool_name, equals_ref.tool_use_id
            )
            if list_payload is None or equals_payload is None:
                continue
            items, found = resolve_json_pointer(list_payload, list_ref.pointer)
            target, t_found = resolve_json_pointer(equals_payload, equals_ref.pointer)
            if not found or not t_found or not isinstance(items, list):
                continue
            marked = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get(rule.match_field)) == str(target):
                    item.update(rule.set)
                    marked += 1
                else:
                    item.update(rule.else_set)
            logger.info(
                f"template_intent {cfg.name!r}: enrich matched {marked}/"
                f"{len(items)} items on {rule.match_field!r}"
            )
        except Exception:  # noqa: BLE001 — decoration, never fatal
            logger.exception(
                f"template_intent {cfg.name!r}: enrich rule failed; skipping"
            )


def _make_drive(cfg: CustomUiIntent):
    async def drive(
        agent: Any,
        prep: Any,
        node: Dict[str, Any],
        parsed: ParsedIntent,
        turn_id: str,
    ) -> AsyncIterator[Tuple[Optional[SSEEvent], Optional[str], Any]]:
        payload = parsed.intent.payload or {}
        final_tool: Optional[str] = None
        final_result: Any = None
        for step in cfg.steps:
            args: Dict[str, Any] = dict(step.args)
            missing = None
            for arg, key in step.args_from_payload.items():
                value = _dig(payload, key)
                if value is None:
                    missing = key
                    break
                args[arg] = value
            if missing is not None:
                logger.warning(
                    f"template_intent {cfg.name!r}: payload key {missing!r} "
                    f"missing for step {step.tool!r}"
                )
                for ev in error_events(
                    "invalid_intent_payload",
                    "This action is missing required details. Please try "
                    "again from a fresh card.",
                ):
                    yield ev, None, None
                return
            events, result = await run_persisted_tool(
                agent,
                tool_name=step.tool,
                args=args,
                node=node,
                prep=prep,
                turn_id=turn_id,
            )
            for ev in events:
                yield ev, None, None
            final_tool, final_result = step.tool, result
            if not _is_tool_success(result):
                # Surface the failing step as the final pair — the engine
                # shell renders the typed intent_tool_failed copy.
                break
        else:
            # Every step succeeded — apply the cross-tool marks BEFORE the
            # engine shell resolves the show op against the store.
            if cfg.enrich:
                _apply_enrich(agent, cfg)
        yield None, final_tool, final_result

    return drive


def _make_show_op(cfg: CustomUiIntent):
    def show_op(tool_name: str, result: Any, agent: Any) -> Dict[str, Any]:
        # id 'root' — the widget's detail-overlay tree anchors on it.
        op: Dict[str, Any] = {
            "op": "show",
            "id": "root",
            "component": cfg.component,
            "bind": dict(cfg.bind),
        }
        if cfg.props:
            op["props"] = dict(cfg.props)
        return op

    return show_op


def template_intent_policies(template: Any) -> Dict[str, IntentPolicy]:
    """Build the per-request policy map off the template's
    ``ui_intents.custom`` config. Empty config → empty map (zero cost on
    every template that never opts in)."""
    configurations = getattr(template, "configurations", None)
    ui_intents = getattr(configurations, "ui_intents", None) if configurations else None
    custom = list(getattr(ui_intents, "custom", None) or [])
    policies: Dict[str, IntentPolicy] = {}
    for cfg in custom:
        policies[cfg.name] = IntentPolicy(
            route=IntentRoute.DIRECT,
            payload_model=TemplateIntentPayload,
            default_display=cfg.display,
            drive=_make_drive(cfg),
            show_op=_make_show_op(cfg) if cfg.component else None,
            silent=cfg.silent,
        )
    return policies


__all__ = ["TemplateIntentPayload", "template_intent_policies"]
