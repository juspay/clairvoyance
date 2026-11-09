"""A Xyne form: the layout a merchant registers, and the message a send posts.

Xyne keeps no templates. Every postMessage carries its whole form — ``flow``:
version 2.0, a screenId, the components, ``data`` and an initial ``state``
(xyne-spaces apps/backend/src/apps/controllers/chatController.ts,
PostMessageBodySchema) — and Xyne validates it as it arrives
(packages/shared/src/validation/flowSchema.ts, validateFlowDefinition). So a
registry row's ``components`` IS Xyne's components array, verbatim, and this
file wraps it for one person:

- ``{blank}`` inside any string of the layout is filled from the send's
  text variables. Xyne shows text exactly as written; it has no blanks of
  its own.
- Every variable also rides in ``data``, where Xyne's ``"$key"`` references
  read it — a list there becomes a dropdown's or a checkbox group's options
  (``"options": "$slots"`` reads ``data.slots``).
- ``state`` starts empty; Xyne's schema requires it present.

PURE. The layout checks here are the cheap, local ones; Xyne's own
validator, run on the preview at submit (templates.py), is the authority on
the layout. It is NOT on the per-person data — Xyne accepts any ``data`` and
shows an empty picker when an option list is missing or misshapen — so
build_flow refuses that itself, before anything is posted.
"""

import json
import re
from typing import Any, Dict, List, Optional, Set, Union

FLOW_VERSION = "2.0"

#: A blank is ``{name}`` — the workflow's placeholder spelling, numbered
#: ones (``{1}``, the WhatsApp habit) included, so none can reach a person
#: unfilled. Anything else in braces is text, left as written.
BLANK = re.compile(r"\{([A-Za-z0-9_]+)\}")

#: Xyne reads ``"options": "$key"`` from the message's ``data[key]``, and
#: shows nothing — no error, an empty picker — unless that is a list of
#: ``{label, value}`` (dashboard SelectNode.tsx / CheckboxNode.tsx, mobile
#: flowState.ts getControlOptions). Its validator accepts any ``data``.
OPTION_REF = "$"

#: The layout a merchant may register, and a whole form with one person's
#: data. Generous for a form; a bound so neither the registry row nor a
#: manifest row can grow without limit.
MAX_LAYOUT_BYTES = 32 * 1024
MAX_FLOW_BYTES = 64 * 1024

_TEXT = (str, int, float)


def layout_problem(components: Any) -> Optional[str]:
    """PURE: what is wrong with a layout before Xyne is asked, or None.

    Only what Xyne requires of EVERY component (an ``id`` and a ``type``,
    flowSchema.ts baseComponentSchema) and a size bound. Each component
    type's own rules are Xyne's to judge — restating them here would refuse
    shapes Xyne accepts the day it changes them."""
    if not isinstance(components, list) or not components:
        return "a Xyne form needs at least one component"
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            return f"component {index} is not an object"
        for key in ("id", "type"):
            value = component.get(key)
            if not isinstance(value, str) or not value.strip():
                return f"component {index} needs a non-empty '{key}'"
    if _size(components) > MAX_LAYOUT_BYTES:
        return f"the form is larger than {MAX_LAYOUT_BYTES // 1024} KB"
    return None


def blanks(components: Any) -> Set[str]:
    """PURE: every ``{name}`` the layout's strings ask to be filled."""
    found: Set[str] = set()
    for text in _strings(components):
        found.update(BLANK.findall(text))
    return found


def build_flow(
    components: List[Dict[str, Any]],
    variables: Dict[str, Any],
    screen_id: str,
    *,
    preview: bool = False,
) -> Union[Dict[str, Any], str]:
    """PURE: the ``flow`` one person is sent, or why there is none.

    A blank with no text variable is refused — a person must never read
    ``{candidate_name}`` — except in a preview, which shows the merchant
    the layout as written. ``variables`` arrive as the manifest stored them
    (what we actually posted, canon T16 col 11): text values fill blanks,
    and all of them, lists included, ride in ``data``."""
    text = {
        name: value
        for name, value in variables.items()
        if isinstance(value, _TEXT) and not isinstance(value, bool)
    }
    missing = sorted(blanks(components) - set(text))
    if missing and not preview:
        return f"no value for {', '.join('{' + name + '}' for name in missing)}"
    if not preview:
        for key in sorted(option_refs(components)):
            if not _is_options(variables.get(key)):
                return (
                    f"data.{key} must be a non-empty list of {{label, value}} — "
                    "Xyne would show an empty picker"
                )
    flow = {
        "version": FLOW_VERSION,
        "screenId": screen_id,
        "components": _render(components, text),
        "data": dict(variables),
        "state": empty_state(),
    }
    if _size(flow) > MAX_FLOW_BYTES:
        return f"the form with its data is larger than {MAX_FLOW_BYTES // 1024} KB"
    return flow


def option_refs(components: Any) -> Set[str]:
    """PURE: every ``data`` key the layout's pickers read options from."""
    found: Set[str] = set()
    for node in _dicts(components):
        options = node.get("options")
        if isinstance(options, str) and options.startswith(OPTION_REF):
            found.add(options[len(OPTION_REF) :])
    return found


def _is_options(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(
            isinstance(item, dict)
            and isinstance(item.get("label"), str)
            and item["label"].strip()
            and isinstance(item.get("value"), str)
            and item["value"].strip()
            for item in value
        )
    )


def empty_state() -> Dict[str, Any]:
    """The state a form opens in — every field Xyne's schema requires."""
    return {
        "values": {},
        "touched": {},
        "errors": {},
        "submitting": False,
        "submitted": False,
        "history": [],
        "loadingComponentIds": [],
    }


def _render(node: Any, text: Dict[str, Any]) -> Any:
    """``node`` with every string's blanks filled, in one pass — a value
    that itself contains ``{x}`` is never filled again."""
    if isinstance(node, str):
        return BLANK.sub(
            lambda m: str(text[m.group(1)]) if m.group(1) in text else m.group(0),
            node,
        )
    if isinstance(node, list):
        return [_render(item, text) for item in node]
    if isinstance(node, dict):
        return {key: _render(value, text) for key, value in node.items()}
    return node


def _dicts(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _dicts(item)


def _strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
