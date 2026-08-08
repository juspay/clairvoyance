"""Constrained speculative completion + dispatch for ``SpeculativeTurnGate``.

The MAIN LLM (via the provider-agnostic ``get_chat_completions`` — Azure, Gemini,
Claude, OpenAI all work) is constrained to emit, per turn, ONE of:

  * a single **line id number** → the gate speaks that PRE-WRITTEN line
    (placeholder vars resolved at commit) — instant cached TTS,
  * ``"."`` → silence,
  * a **function call** → a normal tool call with real, structured args.

Functions are the template's own (any function, any args) — the gate is fully
generic: it builds the tool schema from ``ScriptedFunction.properties/required``,
and at commit fires the function's ``hooks`` via the shared
``_execute_hooks_async`` path (capture via ``expected_fields``/``FieldConfig``)
then runs the ``handler`` via ``builtin_function_dispatcher``. No business field
(rating/feedback/…) is named anywhere in the gate.

The closing line for an action is *declarative* (``ScriptedFunction.line``), not
emitted by the model — so the model never has to combine text + tool-call in one
turn (gpt-4o drops content when it emits a tool-call; this sidesteps that). See
docs/SCRIPTED_RESPONSES_PLAN.md.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, cast

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.field_resolver import (
    replace_placeholders,
)
from app.ai.voice.agents.breeze_buddy.processors.speculative_turn_gate import (
    DispatchFn,
    SpecResult,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    PhraseEntry,
    ScriptedFunction,
)
from app.core.logger import logger

SystemMessagesFn = Callable[[], List[Dict[str, str]]]
TemplateVarsFn = Callable[[], Dict[str, Any]]
CompleteFn = Callable[[List[Dict[str, str]], str], Awaitable[SpecResult]]


def _line_table(phrases: List[PhraseEntry]) -> str:
    rows = []
    for i, p in enumerate(phrases, start=1):
        bits = [f"  {i}."]
        if p.label:
            bits.append(f" [{p.label}]")
        bits.append(f' "{p.text}"')
        if p.description:
            bits.append(f" — {p.description}")
        rows.append("".join(bits))
    return "\n".join(rows)


def _functions_block(functions: List[ScriptedFunction]) -> str:
    rows = []
    for f in functions:
        params = ", ".join(f.required) or (
            ", ".join(f.properties.keys()) if f.properties else ""
        )
        sig = f"{f.name}({params})" if params else f.name
        rows.append(f"  {sig} — {f.description}")
    return "\n".join(rows) or "  (none)"


def _system_prompt(
    base_system_messages: List[Dict[str, str]],
    phrases: List[PhraseEntry],
    functions: List[ScriptedFunction],
) -> str:
    base = "\n\n".join(
        m.get("content", "") for m in base_system_messages if m.get("content")
    )
    # Safety net: if a LEGACY template still carries a free-text "speak these
    # scripts as written" section, drop it — it conflicts with the constrained
    # "emit a line number" rule and makes some models echo the full line text
    # (-> silence). Target ONLY the legacy wording ("SCRIPTS — SPEAK …"). A
    # mode-aware template's section 8 is "OUTPUT FORMAT — CONSTRAINED" and MUST
    # be kept, so we deliberately do NOT match a bare "### 8)" — matching that
    # would strip the very constrained instructions we need (regression seen in
    # production: the model only got sections 1-7 + the line table).
    for marker in (
        "\n### 8) SCRIPTS",
        "\n###8)SCRIPTS",
        "\n8) SCRIPTS",
        "\nSCRIPTS — SPEAK",
    ):
        idx = base.find(marker)
        if idx != -1:
            base = base[:idx].rstrip()
            break
    # The constrained OUTPUT instruction lives in the template's role_messages
    # (mode-aware). Here we inject only the DATA — the numbered line table (from
    # `phrases`, so line texts aren't duplicated by hand) and the function list —
    # plus a one-line reinforcement that agrees with the template.
    return (
        f"{base}\n\n"
        f"LINE TABLE (speak by id):\n{_line_table(phrases)}\n\n"
        f"FUNCTIONS (call by name, pass the listed arguments):\n{_functions_block(functions)}\n"
        'Respond with ONLY a line id number, ".", or a function call. Never output '
        'a line\'s wording — only the bare id, ".", or the function call. A '
        "function's closing line is spoken for you.\n"
    )


def _tools(functions: List[ScriptedFunction]) -> Optional[ToolsSchema]:
    if not functions:
        return None
    schemas = [
        FunctionSchema(
            name=f.name,
            description=f.description,
            properties=f.properties or {},
            required=f.required or [],
        )
        for f in functions
    ]
    return ToolsSchema(standard_tools=schemas)


def _resolve_line(
    line_id: Optional[int], phrases: List[PhraseEntry], template_vars: Dict[str, Any]
) -> Optional[str]:
    if isinstance(line_id, int) and 1 <= line_id <= len(phrases):
        return (
            replace_placeholders(phrases[line_id - 1].text, template_vars).strip()
            or None
        )
    return None


# Line labels whose meaning is "I couldn't understand the user" (a pardon /
# ask-to-repeat). A pardon is the correct answer to a genuine half-word, but when
# the model picks one for a longer utterance the interim usually just ended on an
# ambiguous word (e.g. '...अच्छा' just before the user completed it to
# '...अच्छा नहीं था') — the final transcript reads differently and the gate should
# regenerate on it instead of locking in a wrong pardon. Convention-based safety
# net: only these labels trigger the extra regenerate (see _stream_to_result).
_UNCERTAIN_LINE_LABELS = {"pardon", "apology_reask"}


def _norm_text(s: str) -> str:
    """Normalize for fuzzy line-text matching: lowercase, drop spaces, digits,
    Devanagari danda, and common punctuation."""
    return re.sub(r"[\s\d।.?!,:;\-_\"'`()]+", "", (s or "").lower())


def _match_phrase_text(
    raw: str, phrases: List[PhraseEntry], template_vars: Dict[str, Any]
) -> Optional[int]:
    """Recover a line id when the model emitted a line's TEXT instead of its
    number (some models follow "say 'जी?'"-style cues in the role_messages).
    Returns the 1-based id, or None if nothing matches confidently.
    """
    nraw = _norm_text(raw)
    if not nraw:
        return None
    norms = [
        (i, _norm_text(replace_placeholders(p.text, template_vars)))
        for i, p in enumerate(phrases, start=1)
    ]
    norms = [(i, nph) for i, nph in norms if nph]
    # 1) Exact normalized match — handles SHORT lines like "जी?" (whose prefix
    #    is too short for the prefix test, but is itself a whole line).
    for i, nph in norms:
        if nraw == nph:
            return i
    # 2) Longest-common-prefix — handles longer echoes (model emits the start of
    #    a line). Require a meaningful prefix to avoid false positives.
    if len(nraw) < 6:
        return None
    best_id: Optional[int] = None
    best_len = 0
    for i, nph in norms:
        prefix = 0
        for a, b in zip(nraw, nph):
            if a == b:
                prefix += 1
            else:
                break
        if prefix > best_len and prefix >= 6:
            best_len, best_id = prefix, i
    return best_id


# Number words → digit, for recovering a dropped rating (or any 1-5 integer
# arg) the model omitted from a tool call. Covers ASCII digits, Devanagari
# digits, Hindi words and common English/romanized spellings — the same mapping
# the template prompt teaches the model. Generic safety net only.
_RATING_WORDS: Dict[str, int] = {}
for _w, _v in (
    ("1 one ek ik १ एक", 1),
    ("2 two do doh dho २ दो", 2),
    ("3 three teen tin ३ तीन", 3),
    ("4 four char chaar ४ चार", 4),
    ("5 five paanch panch punch ५ पाँच पांच पाच", 5),
):
    for _tok in _w.split():
        _RATING_WORDS[_tok] = _v
del _w, _v


def _extract_rating_value(text: str) -> Optional[int]:
    """First 1-5 number (ASCII/Devanagari digit, or Hindi/English word) in
    ``text``, scanning left to right. None if none found."""
    for tok in re.split(r"[\s।.?!,:;\-_\"'`()]+", text or ""):
        if not tok:
            continue
        v = _RATING_WORDS.get(tok) or _RATING_WORDS.get(tok.lower())
        if v is not None:
            return v
    return None


def _recover_missing_int_args(
    fn: ScriptedFunction,
    args: Dict[str, Any],
    history: Optional[List[Dict[str, str]]],
) -> Dict[str, Any]:
    """Fill integer (1-5) args the model dropped by scanning recent user turns.

    Generic: any ``integer`` property of ``fn`` absent from ``args`` whose value
    appears as a 1-5 number in a recent user utterance is injected — e.g.
    ``negative_feedback({feedback: ...})`` recovers the rating the user stated a
    turn or two earlier (smaller models often drop it from the tool args). Only
    1-5 integers are ever injected (a safe, narrow scale).
    """
    if not history:
        return args
    props = fn.properties or {}
    missing_int = [
        name
        for name, spec in props.items()
        if name not in args and isinstance(spec, dict) and spec.get("type") == "integer"
    ]
    if not missing_int:
        return args
    out = dict(args)
    # User turns most-recent-first: the latest stated value is the truest.
    user_texts = [
        m["content"]
        for m in reversed(history)
        if m.get("role") == "user" and m.get("content")
    ]
    for name in missing_int:
        for ut in user_texts[:8]:  # last ~8 user turns is plenty
            v = _extract_rating_value(ut)
            if v is not None:
                out[name] = v
                logger.info(
                    f"[spec-constrained] recovered missing int arg {name!r}={v} "
                    f"for {fn.name!r} from a prior user turn (model omitted it)"
                )
                break
        else:
            logger.info(
                f"[spec-constrained] could not recover missing int arg {name!r} "
                f"for {fn.name!r} from history"
            )
    return out


async def _stream_to_result(
    stream: Any,
    functions: Dict[str, ScriptedFunction],
    phrases: List[PhraseEntry],
    template_vars: Dict[str, Any],
    user_text: str,
) -> SpecResult:
    """Iterate a ``get_chat_completions`` stream into a ``SpecResult``.

    Accumulates ``delta.content`` (the line number / ".") and the first
    ``tool_call`` (name + streamed JSON arguments). No dispatch.
    """
    text_parts: List[str] = []
    tool_name: Optional[str] = None
    tool_args_parts: List[str] = []
    try:
        async for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if content:
                text_parts.append(content)
            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                fn_obj = getattr(tool_calls[0], "function", None)
                if fn_obj is not None:
                    nm = getattr(fn_obj, "name", None)
                    if nm and tool_name is None:
                        tool_name = nm
                    args_chunk = getattr(fn_obj, "arguments", None)
                    if args_chunk:
                        tool_args_parts.append(args_chunk)
    except Exception as e:  # noqa: BLE001
        return SpecResult(error=f"{type(e).__name__}: {e}")

    # Action turn: a function call. Resolve the function's declared closing line.
    if tool_name and tool_name in functions:
        fn = functions[tool_name]
        raw_args = "".join(tool_args_parts).strip()
        args: Dict[str, Any] = {}
        if raw_args:
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    args = parsed
            except json.JSONDecodeError:
                logger.warning(
                    f"[spec-constrained] bad tool args JSON: {raw_args[:60]!r}"
                )
        text = _resolve_line(fn.line, phrases, template_vars)
        logger.info(
            f"[spec-constrained] {user_text[:30]!r} -> call {tool_name}({args}) "
            f"line={fn.line} speak={(text or '')[:25]!r}"
        )
        return SpecResult(text=text, function=tool_name, function_args=args)

    # Conversational turn: a line number or ".".
    raw = "".join(text_parts).strip().strip("`").strip()
    if raw == ".":
        logger.info(f"[spec-constrained] {user_text[:30]!r} -> silence (.)")
        return SpecResult(text=None)
    # Preferred: the line id is the first integer at the START of the reply
    # (tolerate "7.", "9," and any trailing prose — we ignore it and speak the
    # table's line by id, never the model's text).
    line_id: Optional[int] = None
    m = re.match(r"\s*(\d+)", raw)
    if m:
        line_id = int(m.group(1))
    # Fallback: some models emit the line's TEXT (following the role_messages'
    # old "speak these scripts" rule) instead of its number. Recover the id by
    # fuzzy-matching the text so the turn still resolves instead of going silent.
    via_text = False
    if line_id is None:
        line_id = _match_phrase_text(raw, phrases, template_vars)
        via_text = line_id is not None
    text = _resolve_line(line_id, phrases, template_vars)
    if text is None and raw:
        # Unparsable: the model emitted prose we couldn't map. Low-confidence —
        # the gate will regenerate once on the clearer final before committing.
        logger.warning(
            f"[spec-constrained] {user_text[:30]!r} -> unparsable reply {raw[:40]!r}"
        )
        return SpecResult(text=None, low_confidence=True)
    if text is None:
        # The model returned LITERALLY nothing. Intentional silence is "."
        # (handled in its own branch above), so an empty reply is a format
        # failure, not a silence decision — committing None here is dead air,
        # and dead air on the caller's "hello?" ends the call. Fall back to a
        # "please repeat" line so the turn still speaks; flag low-confidence so
        # the gate regenerates on the clearer final first (and if that also
        # fails, this apology is what gets spoken). No such line in the
        # template -> we genuinely can't fabricate one, stay silent.
        fallback_id = next(
            (
                i
                for i, p in enumerate(phrases, start=1)
                if p.label in _UNCERTAIN_LINE_LABELS
            ),
            None,
        )
        fallback = (
            _resolve_line(fallback_id, phrases, template_vars) if fallback_id else None
        )
        if fallback:
            logger.warning(
                f"[spec-constrained] {user_text[:30]!r} -> empty reply, fall back "
                f"to line {fallback_id} {fallback[:20]!r}"
            )
            return SpecResult(text=fallback, low_confidence=True)
        logger.warning(
            f"[spec-constrained] {user_text[:30]!r} -> empty reply, no repeat-line "
            f"fallback -> silent"
        )
        return SpecResult(text=None)
    # A clean line number — but a "couldn't understand you" line (pardon /
    # ask-to-repeat) picked for a multi-word utterance is almost always a misread
    # of an interim that ended on an ambiguous word; the final disambiguates it.
    # Flag it low-confidence so the gate regenerates on the clearer final instead
    # of locking in a wrong pardon. A pardon for a genuine short fragment stays
    # confident (the zero-latency commit path is preserved for real half-words).
    uncertain_line = (
        not via_text
        and line_id is not None
        and phrases[line_id - 1].label in _UNCERTAIN_LINE_LABELS
        and len(user_text.split()) >= 3
    )
    tag = (
        " (text-match LOW-CONF)"
        if via_text
        else (" (uncertain-line LOW-CONF)" if uncertain_line else "")
    )
    logger.info(
        f"[spec-constrained] {user_text[:30]!r} -> line {line_id}{tag} "
        f"speak={(text or '')[:25]!r}"
    )
    low_conf = via_text or uncertain_line
    return SpecResult(text=text, low_confidence=low_conf)


def build_constrained_complete_fn(
    llm: Any,
    system_messages_fn: SystemMessagesFn,
    phrases: List[PhraseEntry],
    functions: List[ScriptedFunction],
    template_vars_fn: TemplateVarsFn,
) -> CompleteFn:
    """Build the constrained main-LLM completion (number / "." / tool-call)."""
    tools = _tools(functions)
    fn_map = {f.name: f for f in functions}
    # System prompt built lazily (system_messages_fn is populated only after the
    # initial node is prepared in _handle_client_connected).
    prompt_cache: Dict[str, Optional[str]] = {"prompt": None}

    def _prompt() -> str:
        if prompt_cache["prompt"] is None:
            prompt_cache["prompt"] = _system_prompt(
                system_messages_fn(), phrases, functions
            )
        return prompt_cache["prompt"]

    async def complete_fn(history: List[Dict[str, str]], user_text: str) -> SpecResult:
        logger.debug(
            f"[spec-constrained] complete: ctx={len(history)}turns "
            f"user={user_text[:40]!r} tools={[f.name for f in functions]}"
        )
        messages: List[Dict[str, str]] = [{"role": "system", "content": _prompt()}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})
        ctx = LLMContext()
        ctx.set_messages(cast(List[LLMContextMessage], messages))
        if tools is not None:
            ctx.set_tools(tools)
        try:
            stream = await llm.get_chat_completions(ctx)
        except Exception as e:  # noqa: BLE001
            return SpecResult(error=f"{type(e).__name__}: {e}", spec_text=user_text)
        result = await _stream_to_result(
            stream, fn_map, phrases, template_vars_fn(), user_text
        )
        # Stamp the (possibly partial) transcript this ran on so the gate can
        # tell when a confident result actually came from a short partial the
        # user then completed, and regenerate on the clearer final.
        result.spec_text = user_text
        return result

    return complete_fn


def build_constrained_dispatch_fn(
    bot: Any, functions: List[ScriptedFunction]
) -> DispatchFn:
    """Build the commit-time dispatcher (generic): fire the function's hooks
    (capture via expected_fields — any fields) then run its builtin handler.
    """
    fn_map = {f.name: f for f in functions}

    async def dispatch_fn(
        name: str,
        args: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        fn = fn_map.get(name)
        if fn is None:
            logger.warning(f"[spec-constrained] unknown function {name!r}")
            return
        from app.ai.voice.agents.breeze_buddy.handlers.internal.builtin_dispatcher import (
            builtin_function_dispatcher,
        )
        from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
        from app.ai.voice.agents.breeze_buddy.template.transition import (
            _execute_hooks_async,
        )
        from app.ai.voice.agents.breeze_buddy.template.types import (
            GlobalBuiltinFunction,
        )

        context = TemplateContext(bot)
        call_args = dict(args or {})
        pre_keys = set(call_args.keys())
        # Safety net: recover integer (1-5) args the model dropped from earlier
        # user turns (e.g. a rating stated a turn before negative_feedback).
        call_args = _recover_missing_int_args(fn, call_args, history)
        recovered = sorted(k for k in call_args if k not in pre_keys)
        logger.info(
            f"[spec-constrained] dispatch {name!r} args={call_args} "
            f"recovered={recovered or 'none'} handler={fn.handler!r}"
        )
        # Fire the function's hooks (generic capture — expected_fields/FieldConfig
        # decide which args are LLM/STATIC/COMPUTED). Same path the normal flow
        # uses, so lead.outcome + lead.metaData are written identically.
        if fn.hooks:
            try:
                await _execute_hooks_async(context, call_args, fn.hooks, fn.name)
                logger.info(
                    f"[spec-constrained] fired {len(fn.hooks)} hook(s) for {name!r} "
                    f"with args keys={list(call_args.keys())}"
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[spec-constrained] hooks for {name!r} failed: {type(e).__name__}: {e}"
                )
        # Run the builtin handler (end_conversation preserves an outcome the
        # hooks just set on the lead).
        function_config = GlobalBuiltinFunction(
            name=fn.name, handler=fn.handler, description=fn.description
        )
        try:
            await builtin_function_dispatcher(context, call_args, function_config)
            logger.info(
                f"[spec-constrained] handler {fn.handler!r} completed for {name!r}"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"[spec-constrained] handler {fn.handler!r} failed: {type(e).__name__}: {e}"
            )

    return dispatch_fn
