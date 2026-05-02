"""Pre-finalize linter for Blueprint v3 templates.

Runs the 24 silent-breakage checks from
``docs/blueprint/TEMPLATE_PERFECT_PLAYBOOK.md`` (Part 4) before the draft is handed
off to ``ReplaceTemplateRequest.model_validate()``. Each check either
auto-fixes the draft (for objectively safe transforms) or surfaces a
human-readable error/warning for the LLM / user to resolve.

Pure function — no LLM, no I/O. Safe to call on every tick.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field

from app.ai.voice.agents.breeze_buddy.template.transformation_function import (
    TEMPLATE_FUNCTION_REGISTRY,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
"""Matches ``{identifier}``. Disallows whitespace or non-identifier chars."""

WHITESPACE_PLACEHOLDER_RE = re.compile(r"\{(\s*[A-Za-z_][A-Za-z0-9_ \t]*\s*)\}")
"""Matches ``{ var }`` — placeholder with whitespace inside braces."""

BUILTIN_HANDLERS: set[str] = {
    "connect_to_live_agent",
    "end_conversation",
    "get_current_time",
    "update_outcome",
}
"""Registered builtin global-function handlers."""

TERMINAL_NODE_CANDIDATES: set[str] = {
    "closing",
    "end_call",
    "end_conversation_node",
}
"""Node names that look like the standard terminal node."""

STANDARD_TERMINAL_NODE = "end_conversation_node"

KNOWN_CREDENTIAL_KEYS: set[str] = {
    # Common secret-ish keys resolved from env/secrets rather than payload.
    "api_key",
    "api_base_url",
    "auth_token",
    "bearer_token",
    "client_id",
    "client_secret",
    "merchant_id",
    "reseller_id",
    "shop_name",
}
"""Keys that may legitimately appear in prompts without being in the
payload schema (resolved from template-level secrets or session)."""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class LintResult(BaseModel):
    """Outcome of linting a draft template."""

    auto_fixes_applied: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fixed_draft: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def lint_template(draft: dict[str, Any]) -> LintResult:
    """Run every silent-breakage check against ``draft``.

    Returns a :class:`LintResult` with auto-fixes, errors, and warnings.
    The input ``draft`` is never mutated.
    """

    fixed = copy.deepcopy(draft)
    auto: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []

    # Auto-fixes first so subsequent checks see the normalized draft.
    _autofix_is_active(fixed, auto)
    _autofix_end_conversation_callbacks(fixed, auto)
    _autofix_placeholder_whitespace(fixed, auto)
    _autofix_outcome_casing(fixed, auto)
    _autofix_function_property_defaults(fixed, auto)
    _autofix_global_http_type(fixed, auto)
    _autofix_terminal_node(fixed, auto)

    # Errors / warnings.
    _lint_initial_node(fixed, errors)
    _lint_builtin_handlers(fixed, errors)
    _lint_transition_targets(fixed, errors)
    _lint_warm_transfer(fixed, errors, warnings)
    _lint_outcome_hooks(fixed, errors)
    _lint_http_request_hooks(fixed, errors)
    _lint_ivr_options(fixed, errors)
    _lint_llm_expected_fields(fixed, errors)
    _lint_function_name_collisions(fixed, errors)
    _lint_payload_transformation_names(fixed, errors)
    _lint_prompt_placeholders(fixed, errors)
    _lint_background_sound(fixed, errors)
    _lint_user_speech_timeout(fixed, errors)
    _lint_ivr_inbound_flag(fixed, errors)
    _lint_pre_tts_message_placement(fixed, errors, warnings)

    # Warnings.
    _warn_outbound_number_id(fixed, warnings)
    _warn_keyword_filter(fixed, warnings)
    _warn_noise_filter(fixed, warnings)
    _warn_placeholder_in_function_description(fixed, warnings)

    return LintResult(
        auto_fixes_applied=auto,
        errors=errors,
        warnings=warnings,
        fixed_draft=fixed,
    )


# ---------------------------------------------------------------------------
# Traversal helpers
# ---------------------------------------------------------------------------


def _flow(draft: dict[str, Any]) -> dict[str, Any]:
    flow = draft.get("flow")
    return flow if isinstance(flow, dict) else {}


def _nodes(draft: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = _flow(draft).get("nodes")
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def _global_functions(draft: dict[str, Any]) -> list[dict[str, Any]]:
    gfs = _flow(draft).get("global_functions")
    return [g for g in gfs if isinstance(g, dict)] if isinstance(gfs, list) else []


def _configurations(draft: dict[str, Any]) -> dict[str, Any]:
    cfg = draft.get("configurations")
    return cfg if isinstance(cfg, dict) else {}


def _iter_node_functions(
    draft: dict[str, Any],
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for node in _nodes(draft):
        for fn in node.get("functions") or []:
            if isinstance(fn, dict):
                yield node, fn


def _iter_hooks(
    draft: dict[str, Any],
) -> Iterable[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Yield ``(owner_desc, owner, hook)`` tuples for node and global
    function hooks. ``owner_desc`` is a short breadcrumb for error
    messages."""

    for node, fn in _iter_node_functions(draft):
        for hook in fn.get("hooks") or []:
            if isinstance(hook, dict):
                yield (
                    f"node '{node.get('node_name')}' function '{fn.get('name')}'",
                    fn,
                    hook,
                )
    for gf in _global_functions(draft):
        for hook in gf.get("hooks") or []:
            if isinstance(hook, dict):
                yield (f"global function '{gf.get('name')}'", gf, hook)


def _collect_placeholders(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(PLACEHOLDER_RE.findall(value))
    if isinstance(value, list):
        out: set[str] = set()
        for item in value:
            out.update(_collect_placeholders(item))
        return out
    if isinstance(value, dict):
        out = set()
        for v in value.values():
            out.update(_collect_placeholders(v))
        return out
    return set()


# ---------------------------------------------------------------------------
# Auto-fixes
# ---------------------------------------------------------------------------


def _autofix_is_active(draft: dict[str, Any], auto: list[str]) -> None:
    if "is_active" not in draft or draft.get("is_active") is None:
        draft["is_active"] = True
        auto.append("set is_active=true (default)")


def _autofix_end_conversation_callbacks(draft: dict[str, Any], auto: list[str]) -> None:
    flow = draft.setdefault("flow", {})
    if not isinstance(flow, dict):
        return
    # Only add the default if the key is missing entirely (None).
    # An explicit empty list [] means "no callbacks" — respect that choice.
    cbs = flow.get("end_conversation_callbacks")
    if cbs is None:
        flow["end_conversation_callbacks"] = ["service_callback"]
        auto.append("set flow.end_conversation_callbacks=['service_callback']")


def _autofix_placeholder_whitespace(draft: dict[str, Any], auto: list[str]) -> None:
    """Normalise ``{ var }`` → ``{var}`` everywhere in prompt strings.

    Walks the draft recursively. Only transforms strings.
    """

    fixed_any = False

    def _fix(value: Any) -> Any:
        nonlocal fixed_any
        if isinstance(value, str):
            new = WHITESPACE_PLACEHOLDER_RE.sub(
                lambda m: "{" + m.group(1).strip().replace(" ", "_") + "}",
                value,
            )
            if new != value:
                fixed_any = True
            return new
        if isinstance(value, list):
            return [_fix(v) for v in value]
        if isinstance(value, dict):
            return {k: _fix(v) for k, v in value.items()}
        return value

    for k in list(draft.keys()):
        draft[k] = _fix(draft[k])

    if fixed_any:
        auto.append("stripped whitespace from {placeholder} keys")


def _autofix_outcome_casing(draft: dict[str, Any], auto: list[str]) -> None:
    changed = 0
    for _, _, hook in _iter_hooks(draft):
        if hook.get("name") != "update_outcome_in_database":
            continue
        ef = hook.get("expected_fields")
        if not isinstance(ef, dict):
            continue
        outcome = ef.get("outcome")
        if not isinstance(outcome, dict):
            continue
        if outcome.get("source") != "static":
            continue
        val = outcome.get("value")
        if isinstance(val, str) and val and val != val.upper():
            outcome["value"] = val.upper()
            changed += 1
    if changed:
        auto.append(f"upper-cased {changed} static outcome value(s)")


def _autofix_function_property_defaults(draft: dict[str, Any], auto: list[str]) -> None:
    changed = 0
    for _node, fn in _iter_node_functions(draft):
        touched = False
        if "properties" not in fn:
            fn["properties"] = {}
            touched = True
        if "required" not in fn:
            fn["required"] = []
            touched = True
        if touched:
            changed += 1
    if changed:
        auto.append(f"added empty properties/required to {changed} node function(s)")


def _autofix_global_http_type(draft: dict[str, Any], auto: list[str]) -> None:
    changed = 0
    for gf in _global_functions(draft):
        if gf.get("http_request") and not gf.get("type"):
            gf["type"] = "http"
            changed += 1
    if changed:
        auto.append(
            f"set type='http' on {changed} global function(s) with http_request"
        )


def _autofix_terminal_node(draft: dict[str, Any], auto: list[str]) -> None:
    nodes = _nodes(draft)
    node_names = {n.get("node_name") for n in nodes}
    referenced_terminal: Optional[str] = None
    for _node, fn in _iter_node_functions(draft):
        target = fn.get("transition_to")
        if isinstance(target, str) and target in TERMINAL_NODE_CANDIDATES:
            if target not in node_names:
                referenced_terminal = target
                break
    if not referenced_terminal:
        return

    flow = draft.setdefault("flow", {})
    if not isinstance(flow, dict):
        return
    node_list = flow.setdefault("nodes", [])
    if not isinstance(node_list, list):
        return
    new_node = {
        "node_name": referenced_terminal,
        "task_messages": [
            {
                "role": "system",
                "content": (
                    "Thank the customer briefly, wish them a good day, and end the call."
                ),
            }
        ],
        "role_messages": [],
        "functions": [],
        "pre_actions": [{"type": "function", "handler": "mute_stt"}],
        "post_actions": [{"type": "function", "handler": "end_conversation"}],
    }
    node_list.append(new_node)
    auto.append(
        f"created standard terminal node '{referenced_terminal}' with mute_stt/end_conversation actions"
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _lint_initial_node(draft: dict[str, Any], errors: list[str]) -> None:
    flow = _flow(draft)
    initial = flow.get("initial_node")
    if not initial:
        errors.append("flow.initial_node is missing")
        return
    if initial not in {n.get("node_name") for n in _nodes(draft)}:
        errors.append(
            f"flow.initial_node='{initial}' does not match any node in flow.nodes"
        )


def _lint_builtin_handlers(draft: dict[str, Any], errors: list[str]) -> None:
    for gf in _global_functions(draft):
        # Only builtin-style globals are constrained. HTTP globals have
        # arbitrary names.
        if gf.get("type") == "http":
            continue
        handler = gf.get("handler")
        if handler is None:
            continue
        if handler not in BUILTIN_HANDLERS:
            errors.append(
                f"global function '{gf.get('name')}' has unknown builtin handler "
                f"'{handler}'. Expected one of: {sorted(BUILTIN_HANDLERS)}"
            )


def _lint_transition_targets(draft: dict[str, Any], errors: list[str]) -> None:
    node_names = {n.get("node_name") for n in _nodes(draft)}
    for node, fn in _iter_node_functions(draft):
        target = fn.get("transition_to")
        if target and target not in node_names:
            errors.append(
                f"node '{node.get('node_name')}' function '{fn.get('name')}' "
                f"transitions to non-existent node '{target}'"
            )


def _lint_warm_transfer(
    draft: dict[str, Any], errors: list[str], warnings: list[str]
) -> None:
    cfg = _configurations(draft)
    has_transfer = False
    transfer_global_has_pre_tts = True  # only flipped if we find one missing
    transfer_global_found = False

    for _node, fn in _iter_node_functions(draft):
        if fn.get("handler") == "connect_to_live_agent":
            has_transfer = True
    for gf in _global_functions(draft):
        if gf.get("handler") == "connect_to_live_agent":
            has_transfer = True
            transfer_global_found = True
            if not gf.get("pre_tts_message"):
                transfer_global_has_pre_tts = False

    if has_transfer and not cfg.get("transfer_number"):
        errors.append(
            "warm-transfer flow uses connect_to_live_agent but "
            "configurations.transfer_number is not set"
        )
    if transfer_global_found and not transfer_global_has_pre_tts:
        warnings.append(
            "connect_to_live_agent global has no pre_tts_message — "
            "farewell may be clipped during bridging"
        )


def _lint_outcome_hooks(draft: dict[str, Any], errors: list[str]) -> None:
    for owner, _owner_obj, hook in _iter_hooks(draft):
        if hook.get("name") != "update_outcome_in_database":
            continue
        ef = hook.get("expected_fields")
        if not isinstance(ef, dict) or "outcome" not in ef:
            errors.append(
                f"{owner} has update_outcome_in_database hook but no 'outcome' "
                f"entry in expected_fields"
            )


def _lint_http_request_hooks(draft: dict[str, Any], errors: list[str]) -> None:
    for owner, _owner_obj, hook in _iter_hooks(draft):
        if hook.get("name") != "send_http_request":
            continue
        if not hook.get("http_request"):
            errors.append(f"{owner} has send_http_request hook with no http_request")


def _lint_ivr_options(draft: dict[str, Any], errors: list[str]) -> None:
    ivr = draft.get("ivr_configuration")
    if not isinstance(ivr, dict):
        return
    options = ivr.get("options")
    if not isinstance(options, list):
        return
    for opt in options:
        if not isinstance(opt, dict):
            continue
        if "template_id" in opt and not opt.get("template_id"):
            errors.append(
                f"ivr_configuration option '{opt.get('digit') or opt}' "
                f"has empty template_id"
            )


def _lint_llm_expected_fields(draft: dict[str, Any], errors: list[str]) -> None:
    for node, fn in _iter_node_functions(draft):
        props = fn.get("properties")
        prop_keys: set[str] = set(props.keys()) if isinstance(props, dict) else set()
        for hook in fn.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            ef = hook.get("expected_fields")
            if not isinstance(ef, dict):
                continue
            for key, spec in ef.items():
                if not isinstance(spec, dict):
                    continue
                if spec.get("source") != "llm":
                    continue
                target = spec.get("value") or key
                if target not in prop_keys:
                    errors.append(
                        f"node '{node.get('node_name')}' function "
                        f"'{fn.get('name')}' llm-source field '{target}' is not "
                        f"declared in function.properties"
                    )


def _lint_function_name_collisions(draft: dict[str, Any], errors: list[str]) -> None:
    node_fn_names: set[str] = set()
    for _node, fn in _iter_node_functions(draft):
        name = fn.get("name")
        if name:
            node_fn_names.add(name)
    for gf in _global_functions(draft):
        name = gf.get("name")
        if name and name in node_fn_names:
            errors.append(
                f"function name '{name}' collides between a node function and a "
                f"global function — runtime resolution is undefined"
            )


def _lint_payload_transformation_names(
    draft: dict[str, Any], errors: list[str]
) -> None:
    schema = draft.get("expected_payload_schema")
    if not isinstance(schema, dict):
        return
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        fn = spec.get("function")
        if fn and fn not in TEMPLATE_FUNCTION_REGISTRY:
            errors.append(
                f"expected_payload_schema['{key}'] references unknown "
                f"transformation function '{fn}'. Registered: "
                f"{sorted(TEMPLATE_FUNCTION_REGISTRY.keys())}"
            )


def _lint_prompt_placeholders(draft: dict[str, Any], errors: list[str]) -> None:
    schema = draft.get("expected_payload_schema")
    secrets = draft.get("secrets")
    cfg = _configurations(draft)

    declared: set[str] = set()
    if isinstance(schema, dict):
        declared.update(schema.keys())
    if isinstance(secrets, dict):
        declared.update(secrets.keys())
    elif isinstance(secrets, list):
        for s in secrets:
            if isinstance(s, dict) and s.get("key"):
                declared.add(s["key"])
            elif isinstance(s, str):
                declared.add(s)
    declared.update(KNOWN_CREDENTIAL_KEYS)

    used: set[str] = set()

    greeting = cfg.get("initial_greeting")
    used.update(_collect_placeholders(greeting))

    transfer_number = cfg.get("transfer_number")
    used.update(_collect_placeholders(transfer_number))

    for node in _nodes(draft):
        for key in ("task_messages", "role_messages"):
            msgs = node.get(key)
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                if isinstance(m, dict):
                    used.update(_collect_placeholders(m.get("content")))

    if not used:
        return

    # Be conservative: only flag if the draft clearly declares SOMETHING
    # (a non-empty schema, or at least one other placeholder that IS
    # declared). If nothing is declared at all, treat it as
    # "schema not written yet" and only flag the obvious `customer_name`
    # case when there are additional placeholders present.
    if not isinstance(schema, dict) or not schema:
        if "customer_name" in used and len(used) > 1:
            errors.append(
                "prompt references {customer_name} but expected_payload_schema "
                "is not defined — declare it before finalize"
            )
        return

    missing = sorted(p for p in used if p not in declared)
    if missing:
        errors.append(
            f"prompt placeholders {missing} not declared in "
            f"expected_payload_schema, secrets, or known credential keys"
        )


def _lint_background_sound(draft: dict[str, Any], errors: list[str]) -> None:
    cfg = _configurations(draft)
    enabled = cfg.get("enable_background_sound")
    if enabled and not cfg.get("background_sound_file"):
        errors.append(
            "configurations.enable_background_sound is true but "
            "background_sound_file is not set"
        )


def _lint_user_speech_timeout(draft: dict[str, Any], errors: list[str]) -> None:
    cfg = _configurations(draft)
    timeout = cfg.get("user_speech_timeout")
    if timeout in (None, 0):
        return
    turn = cfg.get("turn_detection")
    if turn != "timeout":
        errors.append(
            f"configurations.user_speech_timeout={timeout} is set but "
            f"turn_detection='{turn}' (must be 'timeout' for this to take effect)"
        )


def _lint_ivr_inbound_flag(draft: dict[str, Any], errors: list[str]) -> None:
    if draft.get("ivr_configuration") and not draft.get("enable_inbound"):
        errors.append(
            "ivr_configuration is set but enable_inbound is not true — "
            "IVR will never fire"
        )


def _lint_pre_tts_message_placement(
    draft: dict[str, Any], errors: list[str], warnings: list[str]
) -> None:
    """``pre_tts_message`` is only meaningful on terminal builtins
    (connect_to_live_agent, end_conversation). Anywhere else is a UX bug.
    """

    terminal_builtins = {"connect_to_live_agent", "end_conversation"}
    for gf in _global_functions(draft):
        if not gf.get("pre_tts_message"):
            continue
        if gf.get("type") == "http":
            continue
        handler = gf.get("handler")
        if handler and handler not in terminal_builtins:
            errors.append(
                f"global function '{gf.get('name')}' has pre_tts_message set "
                f"on non-terminal builtin handler '{handler}'"
            )


# ---------------------------------------------------------------------------
# Warnings (non-fatal)
# ---------------------------------------------------------------------------


def _warn_outbound_number_id(draft: dict[str, Any], warnings: list[str]) -> None:
    if draft.get("enable_inbound"):
        return
    if not draft.get("outbound_number_id"):
        warnings.append(
            "outbound_number_id is unset and enable_inbound is false — "
            "outbound dialing will fail"
        )


_DEFAULT_FILLER_KEYWORDS = [
    "hello",
    "yes",
    "okay",
    "hmm",
    "ok",
    "haan",
    "ha",
    "ji",
    "acha",
    "right",
    "yeah",
    "hm",
    "yep",
    "sure",
]


def _warn_keyword_filter(draft: dict[str, Any], warnings: list[str]) -> None:
    cfg = _configurations(draft)
    kf = cfg.get("keyword_filter")
    if not kf or not isinstance(kf, dict) or not kf.get("keywords"):
        cfg["keyword_filter"] = {
            "enabled": True,
            "keywords": list(_DEFAULT_FILLER_KEYWORDS),
            "match_type": "exact",
        }
        warnings.append(
            "auto-applied default keyword_filter with standard filler words"
        )


def _warn_noise_filter(draft: dict[str, Any], warnings: list[str]) -> None:
    cfg = _configurations(draft)
    if not cfg.get("noise_filter"):
        cfg["noise_filter"] = {"enable": True, "type": "aic"}
        warnings.append("auto-applied noise_filter (AIC) for telephony")


def _warn_placeholder_in_function_description(
    draft: dict[str, Any], warnings: list[str]
) -> None:
    offenders: list[str] = []
    for node, fn in _iter_node_functions(draft):
        desc = fn.get("description")
        if isinstance(desc, str) and PLACEHOLDER_RE.search(desc):
            offenders.append(
                f"node '{node.get('node_name')}' function '{fn.get('name')}'"
            )
    for gf in _global_functions(draft):
        desc = gf.get("description")
        if isinstance(desc, str) and PLACEHOLDER_RE.search(desc):
            offenders.append(f"global function '{gf.get('name')}'")
    if offenders:
        warnings.append(
            "placeholders found in function.description (never substituted): "
            + ", ".join(offenders)
        )


__all__ = ["LintResult", "lint_template"]
