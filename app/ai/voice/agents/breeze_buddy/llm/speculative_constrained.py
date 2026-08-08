"""Constrained speculative completion + dispatch for ``SpeculativeTurnGate``.

The MAIN LLM is driven per-provider (OpenAI/Azure ``get_chat_completions``,
Google/Gemini ``_stream_content``) and normalized in ``_run_completion`` — so
real tool-calling works across providers. It is constrained to emit, per turn,
ONE of:

  * a single **line id number** → the gate speaks that PRE-WRITTEN line
    (placeholder vars resolved at commit) — instant cached TTS,
  * ``"."`` → silence,
  * a **function call** → a normal tool call with real, structured args.

Functions are the template's own (any function, any args) — the gate is fully
generic: it builds the tool schema from ``FlowFunction.properties/required``,
and at commit fires the function's ``hooks`` via the shared
``_execute_hooks_async`` path (capture via ``expected_fields``/``FieldConfig``)
then runs the ``handler`` via ``builtin_function_dispatcher``. No business field
(rating/feedback/…) is named anywhere in the gate.

The closing line for an action is *declarative* (``FlowFunction.line``), not
emitted by the model — so the model never has to combine text + tool-call in one
turn (gpt-4o drops content when it emits a tool-call; this sidesteps that). See
docs/SCRIPTED_RESPONSES_PLAN.md.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
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
    FlowFunction,
    PhraseEntry,
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


def _functions_block(functions: List[FlowFunction]) -> str:
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
    functions: List[FlowFunction],
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


def _tools(functions: List[FlowFunction]) -> Optional[ToolsSchema]:
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
# net: only these labels trigger the extra regenerate (see _completion_to_result).
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
    fn: FlowFunction,
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


@dataclass
class _RawCompletion:
    """Provider-normalized one-shot LLM result: emitted text + optional tool
    call (name + structured args)."""

    text: str = ""
    fn_name: Optional[str] = None
    fn_args: Optional[Dict[str, Any]] = None


def _parse_args_json(raw: str) -> Optional[Dict[str, Any]]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        logger.warning(f"[spec-constrained] bad tool args JSON: {raw[:60]!r}")
        return None


def _balanced_block(s: str, open_idx: int) -> Optional[str]:
    """Inner text between the opener at ``open_idx`` (``{`` or ``(``) and its
    matching closer, honoring quoted strings. None if unbalanced. Only the
    opener's own bracket type counts toward depth — fine for flat function-call
    args (nested arrays in a value are handled by the JSON path first)."""
    open_ch = s[open_idx]
    close_ch = "}" if open_ch == "{" else ")"
    depth = 0
    in_str = False
    quote = ""
    for i in range(open_idx, len(s)):
        ch = s[i]
        if in_str:
            if ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[open_idx + 1 : i]
    return None


def _coerce_lenient_value(v: str) -> Any:
    v = v.strip()
    if not v:
        return ""
    if (v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'"):
        # Quoted string — try JSON (handles escapes), else strip the quotes.
        if v[0] == '"':
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v[1:-1]
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v  # bare string (e.g. Hindi feedback the model left unquoted)


def _parse_call_args(inner: str) -> Dict[str, Any]:
    """Best-effort parse of a function-call args block. Handles valid JSON,
    JSON-with-unquoted-keys, and loose ``key: value`` / ``key=value`` pairs
    (Gemini's text-form serialization is often not valid JSON, and appears as
    both brace ``fn{k: v}`` and Python-call ``fn(k=v)`` syntax). Commas inside
    quoted strings or nested brackets are preserved."""
    s = inner.strip()
    if not s:
        return {}
    parsed = _parse_args_json(s)
    if parsed is not None:
        return parsed
    depth = 0
    in_str = False
    quote = ""
    cur = ""
    pairs: List[str] = []
    for ch in s:
        if in_str:
            cur += ch
            if ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
            cur += ch
        elif ch in "{(":
            depth += 1
            cur += ch
        elif ch in "})":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            pairs.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        pairs.append(cur)
    out: Dict[str, Any] = {}
    for p in pairs:
        # Split key from value on the first ':' or '=' (brace form uses ':',
        # Python-call kwargs use '='). The value may contain the other char
        # (e.g. a time "12:30"), so only the FIRST separator splits.
        sep_idx = -1
        for i, ch in enumerate(p):
            if ch in ":=":
                sep_idx = i
                break
        if sep_idx < 0:
            continue
        k = p[:sep_idx].strip().strip("\"'")
        v = p[sep_idx + 1 :].strip()
        if not k:
            continue
        out[k] = _coerce_lenient_value(v)
    return out


def _extract_text_form_function_call(
    text: str,
) -> Optional[tuple]:
    """Recover a function call the model emitted as TEXT (Gemini's
    'malformed function call' mode serializes a call in the text part instead of
    a structured ``function_call`` part). Observed syntaxes:
      * ``default_api:fn{args}`` / ``fn{args}``     (brace form, ``key: value``)
      * ``fn(key=value, ...)``                       (Python-call kwargs form)
    Returns ``(name, args_dict)`` or None.

    The function NAME is the critical part — it drives dispatch — and any
    dropped integer args are recovered from history at dispatch, so best-effort
    arg parsing is fine. ``_completion_to_result`` validates the name against the
    function map, so prose that happens to contain ``word(``/``word{`` can't
    mis-dispatch.
    """
    s = (text or "").strip().strip("`").strip()
    if "{" not in s and "(" not in s:
        return None
    m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*([{(])", s)
    if not m:
        return None
    name = m.group(1)
    inner = _balanced_block(s, m.end() - 1)
    if inner is None:
        return None
    return (name, _parse_call_args(inner))


def _normalize_text_form_call(comp: _RawCompletion) -> _RawCompletion:
    """If the provider returned a function call serialized as text (no
    ``function_call`` part), lift it into ``fn_name``/``fn_args`` so the turn
    dispatches instead of going unparsable -> silent."""
    if comp.fn_name or not comp.text:
        return comp
    extracted = _extract_text_form_function_call(comp.text)
    if extracted is None:
        return comp
    name, args = extracted
    logger.info(
        f"[spec-constrained] recovered text-form call {name}({args}) "
        f"from model text {comp.text[:60]!r}"
    )
    comp.fn_name = name
    comp.fn_args = args
    comp.text = ""
    return comp


async def _run_completion(llm: Any, ctx: LLMContext) -> _RawCompletion:
    """Run ONE constrained completion across providers and normalize the
    response to (text, tool-call).

    pipecat's out-of-band ``run_inference`` is text-only by design — both the
    OpenAI and Google implementations drop function-call parts — so to keep
    real tool-calling we drive each provider's native completion stream and
    normalize its output here. This is the only provider-aware code in the
    gate; tool SCHEMATA are still mapped by pipecat's adapter (we set tools on
    the shared ``LLMContext``), so the same ``FlowFunction`` tools work on every
    provider.
    """
    # OpenAI / Azure — get_chat_completions is awaited and yields OpenAI-shaped
    # chunks (delta.content / delta.tool_calls).
    if hasattr(llm, "get_chat_completions"):
        stream = await llm.get_chat_completions(ctx)
        text_parts: List[str] = []
        fn_name: Optional[str] = None
        args_buf = ""
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
                # Constrained mode emits EXACTLY ONE action per turn (the prompt
                # forbids >1 call), so index 0 is the whole call — not a dropped
                # parallel call. Arguments stream in incrementally across chunks,
                # so accumulate into args_buf.
                fn_obj = getattr(tool_calls[0], "function", None)
                if fn_obj is not None:
                    nm = getattr(fn_obj, "name", None)
                    if nm and fn_name is None:
                        fn_name = nm
                    args_chunk = getattr(fn_obj, "arguments", None)
                    if args_chunk:
                        args_buf += args_chunk
        return _RawCompletion("".join(text_parts), fn_name, _parse_args_json(args_buf))
    # Google Vertex (Gemini) — _stream_content yields GenerateContentResponse
    # objects whose candidates[].content.parts carry .text and .function_call.
    if hasattr(llm, "_stream_content"):
        candidate = llm._stream_content(ctx)
        # _stream_content may be an async generator (call -> iterator) or a
        # coroutine (await -> iterator); normalize either form.
        stream = await candidate if inspect.isawaitable(candidate) else candidate
        text_parts = []
        fn_name = None
        fn_args: Optional[Dict[str, Any]] = None
        async for resp in stream:
            for cand in getattr(resp, "candidates", None) or []:
                content = getattr(cand, "content", None)
                parts = getattr(content, "parts", None) if content else None
                for part in parts or []:
                    if getattr(part, "text", None):
                        text_parts.append(part.text)
                    fc = getattr(part, "function_call", None)
                    if fc is not None and getattr(fc, "name", None):
                        fn_name = fc.name
                        fn_args = dict(getattr(fc, "args", None) or {})
        return _RawCompletion("".join(text_parts), fn_name, fn_args)
    raise TypeError(
        f"constrained speculative gate must stream tool calls from the LLM "
        f"directly, but {type(llm).__name__} has neither get_chat_completions "
        f"(OpenAI/Azure) nor _stream_content (Google/Gemini). Anthropic/Claude "
        f"(incl. Vertex) is not yet wired for constrained output — use an OpenAI "
        f"or Gemini LLM, or disable constrained_output/enable_interim_llm_send."
    )


def _completion_to_result(
    comp: _RawCompletion,
    functions: Dict[str, FlowFunction],
    phrases: List[PhraseEntry],
    template_vars: Dict[str, Any],
    user_text: str,
) -> SpecResult:
    """Turn a provider-normalized completion into a SpecResult (number / "." /
    function-call). No streaming, no dispatch."""
    # Action turn: a function call. Resolve the function's declared closing line.
    if comp.fn_name and comp.fn_name in functions:
        fn = functions[comp.fn_name]
        args = comp.fn_args or {}
        text = _resolve_line(fn.line, phrases, template_vars)
        logger.info(
            f"[spec-constrained] {user_text[:30]!r} -> call {comp.fn_name}({args}) "
            f"line={fn.line} speak={(text or '')[:25]!r}"
        )
        return SpecResult(text=text, function=comp.fn_name, function_args=args)

    # Conversational turn: a line number or ".".
    raw = (comp.text or "").strip().strip("`").strip()
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
    functions: List[FlowFunction],
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
            comp = await _run_completion(llm, ctx)
        except Exception as e:  # noqa: BLE001
            return SpecResult(error=f"{type(e).__name__}: {e}", spec_text=user_text)
        # Gemini sometimes serializes a function call as text
        # (default_api:fn{args}) instead of a structured function_call part —
        # recover it before resolution so the turn dispatches, not goes silent.
        comp = _normalize_text_form_call(comp)
        result = _completion_to_result(
            comp, fn_map, phrases, template_vars_fn(), user_text
        )
        # Stamp the (possibly partial) transcript this ran on so the gate can
        # tell when a confident result actually came from a short partial the
        # user then completed, and regenerate on the clearer final.
        result.spec_text = user_text
        return result

    return complete_fn


def build_constrained_dispatch_fn(
    bot: Any, functions: List[FlowFunction]
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
                await _execute_hooks_async(
                    context, call_args, [h.model_dump() for h in fn.hooks], fn.name
                )
                logger.info(
                    f"[spec-constrained] fired {len(fn.hooks)} hook(s) for {name!r} "
                    f"with args keys={list(call_args.keys())}"
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[spec-constrained] hooks for {name!r} failed: {type(e).__name__}: {e}"
                )
        # Run the builtin handler (end_conversation preserves an outcome the
        # hooks just set on the lead). A function without a handler is a
        # speak-line-only action — the hooks above already fired, so there is
        # nothing more to run (and GlobalBuiltinFunction requires a non-null
        # handler).
        if fn.handler:
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
