"""Template-owned speech for tool_based mode (``function.say`` blocks).

In tool_based mode the LLM never writes spoken prose — every utterance is
authored in the template JSON and rendered here when the model calls the
function. This module owns:

- ``render_say``: language selection, variant selection and ``{placeholder}``
  substitution for one ``say`` block. Placeholders are dotted paths into the
  call payload / tool args — ``{items.first.price}`` resolves nested dicts,
  lists (``first``/``last``/index) and JSON-encoded strings.
- ``TOOL_BASED_RULES_PROMPT``: the standard system-prompt section the builder
  appends to the initial node's role messages so the model knows the
  tool-only contract.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.template.types import (
    SayConfig,
    SayPhrasing,
)
from app.core.logger import logger

# ``{token}`` placeholders inside say utterances. The token is a dotted path:
# an identifier root, then segments that are identifiers, ``first``/``last``
# or a numeric list index — ``{customer_name}``, ``{items.0.price}``,
# ``{items.first.name}``. One brace level; literal braces are not a thing in
# spoken lines.
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[0-9a-zA-Z_]+)*)\}")

# ``first`` / ``last`` list accessors inside a placeholder path.
_LIST_ACCESSORS: Dict[str, int] = {"first": 0, "last": -1}

# Sentinel for "path did not resolve" (distinct from any legal leaf value).
_MISSING = object()


def _resolve_path(root: Dict[str, Any], token: str) -> Any:
    """Walk a dotted placeholder path through dicts, lists and JSON strings.

    Returns the leaf value, or ``_MISSING`` when any segment fails. A leaf
    that is still a dict/list does not resolve — raw JSON must never reach
    TTS. A mid-path string that looks like encoded JSON (``{...}``/``[...``)
    is parsed once, so payloads carrying JSON-encoded fields work too.
    """
    current: Any = root
    segments = token.split(".")
    index = 0
    while index < len(segments):
        segment = segments[index]
        if isinstance(current, str):
            if current.lstrip()[:1] not in ("{", "["):
                return _MISSING
            try:
                current = json.loads(current)
            except ValueError:
                return _MISSING
            continue  # re-dispatch the segment against the parsed container
        if isinstance(current, dict):
            if segment not in current or current[segment] is None:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list):
            if segment in _LIST_ACCESSORS:
                if not current:
                    return _MISSING
                current = current[_LIST_ACCESSORS[segment]]
            elif segment.isdigit():
                position = int(segment)
                if position >= len(current):
                    return _MISSING
                current = current[position]
            else:
                return _MISSING
        else:
            return _MISSING
        index += 1
    if isinstance(current, (dict, list)):
        return _MISSING
    return current


# Rendered utterances longer than this are almost certainly a malformed
# substitution; log them loudly so template authors notice.
_MAX_RENDERED_CHARS = 500


def select_say_language(
    say: SayConfig,
    args: Optional[Dict[str, Any]],
) -> str:
    """Pick the spoken language for a ``say`` block.

    Order: the ``language`` argument the model passed (the tool_based
    convention — the model tracks the conversation language), then
    ``say.default_language``, then the first key in ``utterances``. An
    argument naming a language with no authored variants falls back to the
    default / first key (with a warning) so a bad model arg can never mute
    the bot.
    """
    args = args or {}
    available = list(say.utterances.keys())

    requested = args.get("language")
    if isinstance(requested, str) and requested.strip():
        requested = requested.strip().lower()
        if requested in say.utterances:
            return requested
        if available:
            logger.warning(
                f"say: requested language {requested!r} has no utterances; "
                f"falling back (available: {available})"
            )

    if say.default_language and say.default_language in say.utterances:
        return say.default_language
    return available[0]


def pick_utterance(say: SayConfig, language: str) -> str:
    """Pick one variant for ``language`` honouring the phrasing strategy."""
    variants: List[str] = say.utterances.get(language) or []
    if not variants:
        # select_say_language guarantees a valid language; this is defensive.
        first_lang = next(iter(say.utterances))
        variants = say.utterances[first_lang]
        language = first_lang
    if say.phrasing == SayPhrasing.FIRST or len(variants) == 1:
        return variants[0]
    return random.choice(variants)


def substitute_placeholders(
    text: str, args: Dict[str, Any], template_vars: Dict[str, Any]
) -> Tuple[str, List[str]]:
    """Replace ``{token}`` paths with arg values first, then payload vars.

    A token is a dotted path (``{items.first.price}``) walked through dicts,
    lists (``first``/``last``/numeric index) and JSON-encoded strings. The
    ROOT key decides the source: if it exists in ``args`` the path resolves
    against args only, otherwise against ``template_vars`` — args win on
    root-key collision, matching the flat-placeholder behaviour.

    Returns the rendered text plus the list of tokens left unresolved (kept
    verbatim in the text so the failure is audible/visible rather than a
    silent empty slot).
    """
    unresolved: List[str] = []

    def _sub(match: "re.Match[str]") -> str:
        token = match.group(1)
        root = token.split(".", 1)[0]
        source = (
            args if root in args else template_vars if root in template_vars else None
        )
        if source is not None:
            value = _resolve_path(source, token)
            if value is not _MISSING:
                return str(value)
        unresolved.append(token)
        return match.group(0)

    rendered = _PLACEHOLDER_RE.sub(_sub, text)
    return rendered, unresolved


def render_say(
    say: SayConfig,
    args: Optional[Dict[str, Any]],
    template_vars: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Render the spoken line for one function call.

    Args:
        say: the function's ``say`` configuration.
        args: the arguments the LLM passed with the call (``language`` and
            placeholder values come from here).
        template_vars: template payload variables (fallback substitution
            source — args win on key collision).

    Returns:
        ``(text, language)`` — the utterance to push to TTS and the language
        it was selected for.
    """
    args = args or {}
    template_vars = template_vars or {}

    language = select_say_language(say, args)
    utterance = pick_utterance(say, language)
    rendered, unresolved = substitute_placeholders(utterance, args, template_vars)

    if unresolved:
        logger.warning(
            f"say: unresolved placeholders {unresolved} in utterance "
            f"(rendered: {rendered[:120]!r})"
        )
    if len(rendered) > _MAX_RENDERED_CHARS:
        logger.warning(
            f"say: rendered utterance is {len(rendered)} chars "
            f"(> {_MAX_RENDERED_CHARS}); check placeholder substitution"
        )

    return rendered, language


# The standard contract section appended to the initial node's system prompt.
# Authored prompts stay cache-friendly: the block is appended at the tail and
# is identical for every tool_based template.
TOOL_BASED_RULES_PROMPT = """## TOOL-BASED OPERATION (NON-NEGOTIABLE)

You are driving a voice call where EVERY line you speak is pre-authored.
You never write conversational prose — the customer must never hear text
typed by you.

HARD RULES:
1. Your entire response is exactly ONE function call. Never two. Never zero.
2. Never write any spoken text. No greetings, no questions, no acknowledgements,
   no explanations — the function you call does the talking for you.
3. The utterance each function speaks is fixed and already written. Your choice
   of WHICH function to call IS your reply.
4. Pass `language` ("hi" or "en") matching the language the customer is
   speaking right now, not the language they started with.
5. Fill other arguments ONLY with values the customer actually said in their
   latest message. If they said a number in words ("चौंतीस"), pass the words
   exactly as spoken — NEVER convert number words to digits yourself; you
   get Hindi numerals wrong. Pass digits only when the customer said digits.
6. If nothing matches the customer's message, call the most appropriate
   repeat/clarify function — staying silent is not an option."""

# ---------------------------------------------------------------------------
# Sentence-split queueing for say utterances
# ---------------------------------------------------------------------------

# Sentence boundary: Latin/Devanagari terminators followed by whitespace.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.।!?…])\s+")

# Gap between queued sentence frames. Mirrors the pacing the LLM text path
# gets from sentence aggregation: TTS starts synthesizing the first sentence
# while the rest of the utterance is still being queued behind it.
SENTENCE_QUEUE_GAP_SECS = 0.05


def split_say_for_tts(text: str) -> List[str]:
    """Split a rendered say utterance into TTS-sized sentence parts.

    Falls back to the whole line when no boundary is found (single-sentence
    utterances and lines with no terminal punctuation stay whole).
    """
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text.strip()) if p.strip()]
    return parts or [text]


async def queue_say_sentences(
    task: Any,
    text: str,
    *,
    gap_secs: float = SENTENCE_QUEUE_GAP_SECS,
) -> int:
    """Queue one ``TTSSpeakFrame`` per sentence of a say utterance, with a
    small gap between them.

    Matches how streamed LLM prose reaches TTS (sentence aggregation): the
    first sentence enters synthesis immediately and the remaining sentences
    follow in order, so long say lines stop paying the whole-utterance TTFB
    and a barge-in only flushes what hasn't played yet. Returns the number of
    sentences queued.
    """
    from pipecat.frames.frames import TTSSpeakFrame

    sentences = split_say_for_tts(text)
    for i, sentence in enumerate(sentences):
        if i:
            await asyncio.sleep(gap_secs)
        await task.queue_frame(TTSSpeakFrame(text=sentence, append_to_context=True))
    return len(sentences)
