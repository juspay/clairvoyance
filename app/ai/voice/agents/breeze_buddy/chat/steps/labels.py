"""Step-progress labels + generic result summarizer (multi-step UX slice 1).

Common (flavor-agnostic) layer for the live step lines the widget renders
while the agent loop executes tools — "Searching the catalog…" flipping in
place to "Searched the catalog ✓ (6)". The agent emits ``step_started`` /
``step_completed`` SSE events (see :mod:`chat.sse`) around each ungated
tool execution; this module owns the tool-name → label mapping and the
best-effort completion summary.

Flavors register their labels lazily — mirroring
``ui_catalog.register_primitives`` — via :func:`register_step_labels` from
a module imported on ``ui_catalog.ensure_group_loaded(<flavor>)`` (see
``assist/commerce/ucp/step_labels.py``). Unregistered tools fall back to a
generic humanizer so a step line never surfaces a raw ``search_foobar``
string. NO flavor-specific logic lives here: completion summaries come
from flavor-registered summarizers (:func:`register_step_summarizer` —
commerce's reads its ``products`` / ``line_items`` shapes); with none
registered a step completes with no summary. Everything vertical belongs
in the flavor registries.

Registration is per catalog GROUP and keyed by tool ROLE (see
``chat/flavors.py``): a label applies only to sessions whose template
enabled that group, and follows the tool the template bound the role to.
Without the group gate the first commerce template to warm a worker would
relabel every other merchant's steps.
"""

from typing import Any, Callable, List, Mapping, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.chat.flavors import (
    EMPTY_SCOPE,
    FlavorScope,
    scoped_keys,
)

# Same envelope helper the reducers / intent engine use — one definition of
# "success" across every result consumer. Private-name import is deliberate
# (same posture as chat/ui_binding.py and assist/commerce/intents.py).
from app.ai.voice.agents.breeze_buddy.template.session_state import _is_tool_success

# ---------------------------------------------------------------------------
# Label registry — process-global, additive-only (like ui_catalog's catalog).
# ---------------------------------------------------------------------------

# Engine tools ship first-class labels, ungrouped because they belong to
# every session: render_ui is not a shopper-visible "tool" — from the
# shopper's side it IS the response being generated, so its step must never
# read "Rendering the ui" (the humanizer's output).
_ENGINE_STEP_LABELS: dict[str, Tuple[str, str]] = {
    "render_ui": ("Generating response", "Generated response"),
}

# group → (role or tool_name) → (running_label, done_label)
_STEP_LABELS: dict[str, dict[str, Tuple[str, str]]] = {}


def register_step_labels(group: str, mapping: Mapping[str, Tuple[str, str]]) -> None:
    """Register ``role → (running_label, done_label)`` entries for ``group``.

    Called by a flavor's lazily-imported module (mirrors
    ``ui_catalog.register_primitives``). Keys are roles the flavor
    declared via ``register_flavor_roles``, or literal tool names for
    tools it does not expose as rebindable roles.

    Idempotent — re-registration overwrites the same entries; it never
    removes existing ones, so per-session behaviour stays deterministic
    regardless of which flavors a process has loaded.
    """
    _STEP_LABELS.setdefault(group, {}).update(mapping)


# A few irregular / doubling verbs common in tool names. Everything else
# goes through the regular -ing / -ed rules below. Best-effort by design:
# flavors register proper labels for the tools users actually see.
_IRREGULAR_VERBS: dict[str, Tuple[str, str]] = {
    "get": ("getting", "got"),
    "set": ("setting", "set"),
    "put": ("putting", "put"),
    "run": ("running", "ran"),
    "send": ("sending", "sent"),
    "find": ("finding", "found"),
    "make": ("making", "made"),
}


def _verb_forms(verb: str) -> Tuple[str, str]:
    """(gerund, past) for a lowercase verb — rule-based with a tiny
    irregular map."""
    if verb in _IRREGULAR_VERBS:
        return _IRREGULAR_VERBS[verb]
    if verb.endswith("e") and not verb.endswith("ee"):
        return f"{verb[:-1]}ing", f"{verb}d"
    if (
        len(verb) in (3, 4)
        and verb[-1] not in "aeiouwxy"
        and verb[-2] in "aeiou"
        and verb[-3] not in "aeiou"
    ):
        # Short CVC verbs double the final consonant: scan → scanning/scanned,
        # ship → shipping/shipped. Length-capped so longer unstressed endings
        # ("visit") stay regular.
        return f"{verb}{verb[-1]}ing", f"{verb}{verb[-1]}ed"
    if verb.endswith("y") and len(verb) > 2 and verb[-2] not in "aeiou":
        # query → querying / queried (y stays for -ing, flips for -ed)
        return f"{verb}ing", f"{verb[:-1]}ied"
    return f"{verb}ing", f"{verb}ed"


def _humanize(tool_name: str) -> Tuple[str, str]:
    """Generic ``verb_object`` humanizer: ``search_catalog`` →
    ("Searching the catalog", "Searched the catalog")."""
    words = [w for w in tool_name.replace("-", "_").lower().split("_") if w]
    if not words:
        return "Working", "Done"
    gerund, past = _verb_forms(words[0])
    rest = " ".join(words[1:])
    if rest:
        return (
            f"{gerund.capitalize()} the {rest}",
            f"{past.capitalize()} the {rest}",
        )
    return gerund.capitalize(), past.capitalize()


def resolve_step_label(
    tool_name: str, scope: FlavorScope = EMPTY_SCOPE
) -> Tuple[str, str]:
    """``(running_label, done_label)`` for a tool — an entry registered by
    one of ``scope``'s enabled flavors, generic humanizer otherwise.

    A session with no flavor enabled always gets the humanizer: another
    template's labels are never in scope, however warm the process is."""
    engine = _ENGINE_STEP_LABELS.get(tool_name)
    if engine is not None:
        return engine
    for group, key in scoped_keys(tool_name, scope):
        registered = _STEP_LABELS.get(group, {}).get(key)
        if registered is not None:
            return registered
    return _humanize(tool_name)


# ---------------------------------------------------------------------------
# Completion status + summary — generic keys only.
# ---------------------------------------------------------------------------


def resolve_step_status(result: Any) -> str:
    """``"ok"`` / ``"error"`` off the post-pipeline tool result, using the
    same envelope read as the state reducers (`status: "error"|"failed"`
    is a failure; anything else — including non-dicts — is success).

    ``soft: true`` on an error envelope marks a POLICY REDIRECT (e.g. a
    mid-turn QuickReplies call deferred to the end-of-turn chips cycle):
    the model still reads the error and self-corrects, but the shopper's
    step rail must not paint a failure for an outcome that still happens
    this turn — the step reports ok."""
    if isinstance(result, dict) and result.get("soft") is True:
        return "ok"
    return "ok" if _is_tool_success(result) else "error"


# Flavor-registered result summarizers, tried in registration order —
# first non-(None, None) wins. A summarizer knows its own result shapes
# (commerce: a ``products`` list → "N results", ``line_items`` → cart
# wording); the engine knows none.
StepSummarizerFn = Callable[[Any], Tuple[Optional[str], Optional[int]]]

# group → summarizers, tried in registration order within the group.
_STEP_SUMMARIZERS: dict[str, List[StepSummarizerFn]] = {}


def register_step_summarizer(group: str, fn: StepSummarizerFn) -> None:
    """Register a flavor's ``result → (summary, count)`` summarizer.

    Same lazy lifecycle as :func:`register_step_labels`; re-registering
    the same function is a no-op (idempotent on re-import).
    """
    fns = _STEP_SUMMARIZERS.setdefault(group, [])
    if fn not in fns:
        fns.append(fn)


def summarize_step_result(
    result: Any, scope: FlavorScope = EMPTY_SCOPE
) -> Tuple[Optional[str], Optional[int]]:
    """Best-effort ``(summary, count)`` for a ``step_completed`` event.

    Runs the summarizers of ``scope``'s enabled flavors in order; the
    first one that recognizes the result shape wins. Nothing registered
    (or nothing recognized) yields ``(None, None)`` and the event omits
    both fields — the engine itself reads no result keys.

    The group gate matters more here than anywhere else in this module: a
    summarizer matches on result SHAPE, not on tool name, so an ungated
    one would caption unrelated tools on unrelated templates the moment
    their payload happened to carry a familiar key.
    """
    if not isinstance(result, dict):
        return None, None
    for group in scope.groups:
        for summarizer in _STEP_SUMMARIZERS.get(group, ()):
            summary, count = summarizer(result)
            if summary is not None or count is not None:
                return summary, count
    return None, None


__all__ = [
    "register_step_labels",
    "register_step_summarizer",
    "resolve_step_label",
    "resolve_step_status",
    "summarize_step_result",
]
