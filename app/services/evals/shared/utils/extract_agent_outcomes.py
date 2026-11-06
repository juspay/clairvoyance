"""An agent's outcome words, read from its template.

Any eval about the call's outcome chooses among these: the preset
outcome_correctness eval today, a custom eval tomorrow. The words are read
where the template writes them literally:

  - an ``update_outcome_in_database`` hook on a flow function (flow mode:
    ``flow.nodes[].functions[]``; direct mode: ``flow.functions[]``) with
    ``expected_fields.outcome = {"source": "static", "value": <word>}``;
  - an outcome the LLM fills in (an ``llm`` source, a hook with no
    ``expected_fields``, the ``update_outcome`` builtin) only when the
    function declares ``properties.outcome.enum``;
  - IVR options' ``outcome`` and IVR nodes' ``on_timeout_outcome``;
  - enabled observers' ``action.args.outcome``.

An outcome the LLM writes freely cannot be listed, so a template with one is
unreadable: ``extract_agent_outcomes`` returns None, and an eval that needs the
full list (outcome_correctness) skips that agent.
Platform words (BUSY as the fallback, TRANSFERRED, NO_ANSWER, ...) are not
the agent's decisions and are never listed. After an agent-to-agent
transfer the lead names the second agent's template, so its words are the
ones read.
"""

from typing import Any, Dict, Iterable, List, Mapping, Optional

_OUTCOME_HOOK = "update_outcome_in_database"
_OUTCOME_BUILTIN = "update_outcome"


class _Unreadable(Exception):
    """An outcome the template does not write literally."""


def extract_agent_outcomes(template: Any) -> Optional[Dict[str, str]]:
    """The agent's outcome words, each with a description of when it is
    used, in template order; None when the template lets the LLM write an
    outcome freely. Words differing only in case are one word, spelt as the
    template first writes it."""
    flow = getattr(template, "flow", None) or {}
    words: Dict[str, List[str]] = {}
    try:
        if flow.get("mode") == "ivr":
            _ivr(flow, words)
        else:
            for function in _functions(flow):
                _function(function, words)
        _observers(getattr(template, "configurations", None), words)
    except _Unreadable:
        return None
    return {word: "; ".join(descriptions) for word, descriptions in words.items()}


def _add(words: Dict[str, List[str]], word: Any, description: Any) -> None:
    if not isinstance(word, str) or not word.strip():
        return
    word = word.strip()
    same = next((known for known in words if known.casefold() == word.casefold()), word)
    descriptions = words.setdefault(same, [])
    text = str(description or "").strip() or f"The call ended as {same}."
    if text not in descriptions:
        descriptions.append(text)


def _mappings(items: Any) -> Iterable[Mapping[str, Any]]:
    return (item for item in items or [] if isinstance(item, Mapping))


def _functions(flow: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Every function of a flow-mode or direct-mode template: node
    functions, direct-mode functions and global functions."""
    nodes = flow.get("nodes")
    for node in _mappings(nodes if isinstance(nodes, list) else []):
        yield from _mappings(node.get("functions"))
    yield from _mappings(flow.get("functions"))
    yield from _mappings(flow.get("global_functions"))


def _function(function: Mapping[str, Any], words: Dict[str, List[str]]) -> None:
    description = function.get("description")
    if function.get("type") == "builtin":
        if function.get("handler") == _OUTCOME_BUILTIN:
            _enum(function, words)
        return
    for hook in _mappings(function.get("hooks")):
        if hook.get("name") != _OUTCOME_HOOK:
            continue
        fields = hook.get("expected_fields") or {}
        if not fields:
            # the hook writes the function's own arguments: an outcome only
            # if the function takes one
            if "outcome" in (function.get("properties") or {}):
                _enum(function, words)
            continue
        outcome = fields.get("outcome")
        if not isinstance(outcome, Mapping):
            continue  # this hook sets no outcome
        if outcome.get("source") == "static":
            _add(words, outcome.get("value"), description)
        elif outcome.get("source") == "llm":
            _enum(function, words)
        else:
            raise _Unreadable


def _enum(function: Mapping[str, Any], words: Dict[str, List[str]]) -> None:
    """The outcome argument's declared values; without them the LLM writes
    the outcome freely."""
    outcome = (function.get("properties") or {}).get("outcome")
    if not isinstance(outcome, Mapping) or not outcome.get("enum"):
        raise _Unreadable
    description = outcome.get("description") or function.get("description")
    for value in outcome["enum"]:
        _add(words, value, description)


def _ivr(flow: Mapping[str, Any], words: Dict[str, List[str]]) -> None:
    nodes = flow.get("nodes")
    for name, node in nodes.items() if isinstance(nodes, Mapping) else []:
        if not isinstance(node, Mapping):
            continue
        for option in _mappings(node.get("options")):
            label = option.get("label") or option.get("message")
            _add(words, option.get("outcome"), label)
            # an option's hooks run with no arguments: only fixed words
            for hook in _mappings(option.get("hooks")):
                outcome = (hook.get("expected_fields") or {}).get("outcome")
                if (
                    hook.get("name") == _OUTCOME_HOOK
                    and isinstance(outcome, Mapping)
                    and outcome.get("source") == "static"
                ):
                    _add(words, outcome.get("value"), label)
        _add(
            words,
            node.get("on_timeout_outcome"),
            node.get("on_timeout_message") or f"No answer at {name}.",
        )


def _observers(configurations: Any, words: Dict[str, List[str]]) -> None:
    for observer in getattr(configurations, "observers", None) or []:
        if not getattr(observer, "enabled", True):
            continue
        action = getattr(observer, "action", None)
        args = getattr(action, "args", None) or {}
        _add(words, args.get("outcome"), f"Observer {observer.name} ended the call.")
