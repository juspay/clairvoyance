"""Deterministic tool-result verification hooks (Phase 2).

CODE checks constraints, not LLM self-critique: after a tool executes,
its registered verifiers run over ``(args, post-pipeline result)``. A
failure converts the result into a structured error envelope BEFORE it
reaches the binding store / reducers / LLM context, so:

- a ``show`` op can never hydrate UI off a result that failed its
  post-conditions (the binding store skips error envelopes);
- reducers never lift identifiers off it;
- the model sees a typed, actionable error and reacts in its next cycle
  (retry, adjust arguments, or tell the user) — surfaced on the step
  line as a failed step.

Verifiers are REGISTERED alongside the tools they check (flavor modules,
same lazy-load hook as step labels / annotations) and must be pure and
fast — no I/O, no model calls. A verifier raising is treated as
"verification unavailable" (fail-open with a log), never as a tool
failure: a buggy check must not take down a healthy tool.

Registration is per catalog GROUP and keyed by tool ROLE (see
``chat/flavors.py``). Both halves of that gate are load-bearing here,
because this is the one registry that can turn a SUCCESSFUL call into an
error: ungated, a commerce verifier would reject another merchant's
differently-shaped result on a template that never enabled commerce; and
unbound by role, a template that renamed the tool would silently lose the
check it was relying on.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.chat.flavors import (
    EMPTY_SCOPE,
    FlavorScope,
    scoped_keys,
)
from app.ai.voice.agents.breeze_buddy.template.session_state import _is_tool_success
from app.core.logger import logger

# (args, post-pipeline result) → error message, or None when satisfied.
VerifierFn = Callable[[Dict[str, Any], Any], Optional[str]]

# group → (role or tool_name) → verifiers
_VERIFIERS: Dict[str, Dict[str, List[VerifierFn]]] = {}


def register_tool_verifier(group: str, role: str, fn: VerifierFn) -> None:
    """Attach one post-condition verifier to ``role`` within ``group``
    (additive; idempotent for the same function object)."""
    fns = _VERIFIERS.setdefault(group, {}).setdefault(role, [])
    if fn not in fns:
        fns.append(fn)


def run_tool_verifiers(
    tool_name: str,
    args: Dict[str, Any],
    result: Any,
    scope: FlavorScope = EMPTY_SCOPE,
) -> Optional[str]:
    """Run the verifiers ``scope`` puts in play for ``tool_name``; first
    failure message wins.

    Already-failed results skip verification entirely — the tool already
    failed (canonical envelope read: status="error" OR "failed"); there is
    nothing to post-condition.
    """
    fns: Optional[List[VerifierFn]] = None
    for group, key in scoped_keys(tool_name, scope):
        fns = _VERIFIERS.get(group, {}).get(key)
        if fns:
            break
    if not fns:
        return None
    if not _is_tool_success(result):
        return None
    for fn in fns:
        try:
            message = fn(args, result)
        except Exception as exc:  # noqa: BLE001 — fail-open by contract
            logger.warning(f"tool verifier for {tool_name!r} raised ({exc}); skipping")
            continue
        if message:
            return message
    return None


def verification_error_envelope(message: str, original: Any) -> Dict[str, Any]:
    """The structured error the LLM sees in place of a result that failed
    verification. Carries the original payload so the model can reason
    about WHAT came back, while the envelope status keeps it out of the
    binding store and reducers."""
    return {
        "status": "error",
        "error": f"verification failed: {message}",
        "unverified_result": original,
    }


__all__ = [
    "VerifierFn",
    "register_tool_verifier",
    "run_tool_verifiers",
    "verification_error_envelope",
]
