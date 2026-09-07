"""Early speech: fire TTS on function-name decode for say tools.

A say tool speaks exactly ONE line, in ONE language, chosen by its NAME —
so speech can queue for TTS the moment the streamed function name uniquely
decodes, before the arguments arrive. ``say`` on a function entry is either
a fixed string or ``{"prefix": ..., "arg": ...}`` (fixed prefix fires at
name-decode, the named LLM argument's value is spoken by the transition
handler once the call completes). Hooks (outcome saving) and transitions
run unchanged on the handler path; a per-fire marker keeps the fixed line
from speaking twice.

pipecat 1.1.0 has no name-fragment hook, so one is installed app-side (no
venv patch): ``get_chat_completions`` is wrapped with a chunk tap and
``process_frame`` with a barge-in cancel. Frames queue DIRECTLY into the
TTS service — a task-queued frame would sit behind the LLM processor's
in-flight stream — and still flow through output + assistant aggregator,
so the text commits to the LLM context and interruption flushes apply.

This module is service wiring (it patches LLM/TTS service instances), not
template vocabulary — hence ``agent/`` rather than ``template/``.
"""

from __future__ import annotations

import contextlib
import re
from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Optional

from pipecat.frames.frames import TTSSpeakFrame, UserStartedSpeakingFrame

from app.core.logger import logger

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.।!?…])\s+")
_GLOBAL_FUNCTION_TYPES = {"http", "builtin", "custom"}  # never reach the handler

__all__ = ["EarlySpeechRouter", "attach_early_speech"]


def _render(text: str, mapping: Dict[str, Any]) -> str:
    """Substitute flat ``{placeholder}`` tokens; unknown ones stay put."""
    return _PLACEHOLDER_RE.sub(
        lambda m: (
            str(mapping[m.group(1)])
            if m.group(1) in mapping
            and not isinstance(mapping[m.group(1)], (dict, list))
            else m.group(0)
        ),
        text,
    )


def _strip_placeholder(text: str, name: str) -> str:
    """Drop one placeholder token and collapse the whitespace gap."""
    return re.sub(
        r"\s+",
        " ",
        _PLACEHOLDER_RE.sub(lambda m: "" if m.group(1) == name else m.group(0), text),
    ).strip()


def _split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text.strip()) if p.strip()]
    return parts or [text]


def _flow_entries(flow: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flat function entries: direct-mode ``functions``, node mode's
    top-level ``global_functions`` (adapter globals live there), plus
    every node's ``functions``."""
    entries = [f for f in (flow.get("functions") or []) if isinstance(f, dict)]
    entries += [f for f in (flow.get("global_functions") or []) if isinstance(f, dict)]
    for node in flow.get("nodes") or []:
        entries += [f for f in (node.get("functions") or []) if isinstance(f, dict)]
    return entries


def _collect_say_functions(
    flow: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Optional[str]]]:
    """Normalize ``say`` configs from the raw flow dict (flat direct-mode
    ``functions`` plus every node's ``functions``); adapter-routed globals
    are excluded — they never reach the transition handler.

    Function names are node-scoped in the schema but this pool is flat, so
    a name carrying *different* say config on two nodes is a hard error —
    one of the two nodes would silently speak the other's line. Identical
    entries are allowed (same tool semantics on several nodes). A name also
    declared *without* say somewhere raises too: the bare twin would speak
    the say line and swallow the empty-result contract. Strict prefix pairs
    among say names only degrade latency, so they warn."""
    says: Dict[str, Dict[str, Optional[str]]] = {}
    bare: set = set()
    if not flow:
        return says

    for func in _flow_entries(flow):
        name = func.get("function_name") or func.get("name")
        raw = func.get("say")
        if func.get("type") in _GLOBAL_FUNCTION_TYPES or not name:
            continue
        if not raw:
            bare.add(name)
            continue
        if isinstance(raw, dict):
            say = (raw.get("text") or "").strip() or None
            prefix = (raw.get("prefix") or "").strip() or None
            arg = (raw.get("arg") or "").strip() or None
        else:
            say, prefix, arg = str(raw).strip(), None, None
        if say and arg:
            logger.warning(f"[early-speech] {name}: both text and arg set — using text")
            arg = None
        if prefix and not arg:
            logger.warning(
                f"[early-speech] {name}: prefix without arg — treating as text"
            )
            say = say or prefix
            prefix = None
        if arg and prefix and f"{{{arg}}}" in prefix:
            logger.warning(
                f"[early-speech] {name}: prefix contains {{{arg}}} — the slot is "
                f"dropped and the value is appended once after the prefix; put the "
                f"slot at the line's end or omit it"
            )
        entry = {"say": say, "prefix": prefix, "arg": arg}
        if name in says and says[name] != entry:
            raise ValueError(
                f"function '{name}' carries different say config in two places "
                f"({says[name]} vs {entry}); say names must be unique across the "
                f"flow — the early-fire pool is flat and would speak one node's "
                f"line in the other"
            )
        says[name] = entry

    names = sorted(says)
    for shorter in names:
        for longer in names:
            if shorter != longer and longer.startswith(shorter):
                logger.warning(
                    f"[early-speech] '{shorter}' is a strict prefix of '{longer}': "
                    f"the shorter name can never uniquely decode, so it forgoes "
                    f"early fire and speaks at handler time; the naming rules "
                    f"(TOOL_BASED_SPEECH_ARCHITECTURE.md) forbid this"
                )
    bare_twins = bare & set(says)
    if bare_twins:
        raise ValueError(
            f"say function(s) {sorted(bare_twins)} also declared without `say` "
            f"in another place: the pool is name-keyed, so the bare twin would "
            f"speak the say line and inherit the empty-result contract. "
            f"Rename one of them."
        )
    return says


def _collect_other_names(
    flow: Optional[Dict[str, Any]], says: Dict[str, Any]
) -> frozenset:
    """Every callable name the LLM can stream that is NOT a say function
    (adapter-routed globals under both the flat and the top-level
    ``global_functions`` key — say-bearing or not — hook-only functions,
    plain non-say entries). The knowledge-base tool is synthesized outside
    the flow dict, so its name must arrive via ``extra_tool_names`` (the
    attach site passes it alongside the MCP names). The tap sees fragments
    of every tool call, so a non-say name sharing a prefix with a say name
    must defer the fire:
    streaming ``check`` for ``check_pincode`` must not fire ``check_step``."""
    names: set = set()
    if not flow:
        return frozenset(names)
    for func in _flow_entries(flow):
        name = func.get("function_name") or func.get("name")
        if name:
            names.add(name)
    return frozenset(names) - set(says)


class EarlySpeechRouter:
    """Match streamed function-name fragments to say tools; queue speech
    early (fixed line/prefix) or at handler time (dynamic argument)."""

    def __init__(
        self,
        says: Dict[str, Dict[str, Optional[str]]],
        task_getter: Callable[[], Any],
        template_vars_getter: Callable[[], Dict[str, Any]],
        tts_getter: Optional[Callable[[], Any]] = None,
        other_names: Optional[Iterable[str]] = None,
    ) -> None:
        self._says = says
        self._names = sorted(says)
        self._task_getter = task_getter
        self._template_vars_getter = template_vars_getter
        self._tts_getter = tts_getter
        self._others = frozenset(other_names or ()) - set(says)
        self._fired: set[str] = set()
        self._pending: Counter[str] = Counter()

    def attach(self, llm_service: Any) -> None:
        """Install the name-fragment tap and barge-in cancel (idempotent)."""
        if llm_service is None or not self._names:
            return
        if not (
            hasattr(llm_service, "get_chat_completions")
            and hasattr(llm_service, "process_frame")
        ):
            return  # realtime services stream no tool-call chunks to tap

        router = self
        if not getattr(llm_service, "_early_speech_installed", False):
            original = llm_service.get_chat_completions

            async def get_chat_completions(context: Any) -> Any:
                stream = await original(context)
                hook = getattr(llm_service, "on_function_name_fragment", None)
                if hook is None:
                    return stream
                name_hook: Any = hook  # narrowed non-None for the closure

                async def _tap() -> Any:
                    # Reconstruct the accumulating function name exactly like
                    # pipecat's own _process_context loop does.
                    name_acc, tool_id, last_index = "", "", None
                    try:
                        async for chunk in stream:
                            try:
                                choices = getattr(chunk, "choices", None)
                                tool_calls = (
                                    choices[0].delta.tool_calls
                                    if choices and choices[0].delta
                                    else None
                                )
                                if tool_calls:
                                    tc = tool_calls[0]
                                    if tc.index != last_index:
                                        name_acc, tool_id, last_index = "", "", tc.index
                                    if tc.id:
                                        tool_id = tc.id
                                    if tc.function and tc.function.name:
                                        name_acc += tc.function.name
                                        await name_hook(name_acc, tool_id)
                            except Exception:
                                pass  # never let the tap break the stream
                            yield chunk
                    finally:
                        # pipecat closes ITS wrapper (this generator) and
                        # relies on that cascading onto the real stream
                        # (base_llm's _closing exists precisely so the
                        # AsyncStream doesn't leak its socket to the GC) —
                        # the tap must close what it wrapped.
                        close = getattr(stream, "close", None) or getattr(
                            stream, "aclose", None
                        )
                        if close is not None:
                            with contextlib.suppress(Exception):
                                await close()

                return _tap()

            llm_service.get_chat_completions = get_chat_completions

            orig_process_frame = llm_service.process_frame

            async def process_frame(frame: Any, direction: Any) -> Any:
                if isinstance(frame, UserStartedSpeakingFrame):
                    router.cancel_pending_tails()
                return await orig_process_frame(frame, direction)

            llm_service.process_frame = process_frame
            llm_service._early_speech_installed = True

        llm_service.on_function_name_fragment = self.on_name_fragment

    def consume_pending(self, function_name: Optional[str]) -> bool:
        """One-shot per early fire: True when this function's fixed speech
        already fired. Counted, not slotted — with parallel tool calls every
        early fire lands before ANY handler runs (pipecat drains the chunk
        stream first), so a single-slot marker would drop the first call's
        marker and re-speak its line. Never expires — arguments may stream
        for arbitrarily long before the handler runs; markers clear only on
        consumption, barge-in, or the next early fire of the same name."""
        if function_name is None or self._pending[function_name] <= 0:
            return False
        self._pending[function_name] -= 1
        if self._pending[function_name] <= 0:
            del self._pending[function_name]
        return True

    def cancel_pending_tails(self) -> None:
        """Barge-in: drop the pending markers. Already-queued frames are
        flushed by pipecat's own interruption handling."""
        self._pending.clear()

    async def on_name_fragment(self, partial_name: str, tool_call_id: str) -> None:
        """Tap hook — runs inline in the stream loop; must never raise."""
        try:
            if not self._names or not partial_name:
                return
            key = tool_call_id or partial_name
            if key in self._fired:
                return
            matches = [n for n in self._names if n.startswith(partial_name)]
            if len(matches) != 1:
                return  # ambiguous (or unmatched) — wait for more fragments
            name = matches[0]
            if any(o.startswith(partial_name) for o in self._others):
                return  # a non-say tool shares this prefix — not decodable yet
            if not tool_call_id and name != partial_name:
                return  # name may still grow — wait for the id
            entry = self._says[name]
            fixed = entry["say"] or entry["prefix"]
            if not fixed:
                return  # dynamic-only: nothing is known at name time
            if entry["arg"]:
                # The argument slot is spoken by the handler, once, after the
                # value decodes — never substituted here (a same-named
                # template var would speak a stale lead value).
                fixed = _strip_placeholder(fixed, entry["arg"])
                if not fixed:
                    return  # prefix was only the slot: wait for the handler
            text = _render(fixed, self._template_vars_getter() or {})
            if _PLACEHOLDER_RE.search(text):
                return  # needs an LLM argument — handler path renders it
            target = self._target()
            if target is None:
                return
            self._fired.add(key)
            self._pending[name] += 1
            await self._queue_say(target, text)
            logger.info(f"[early-speech] {name} queued to TTS at name-decode")
        except Exception as e:
            logger.warning(f"[early-speech] failed to fire early: {e}")

    async def speak_from_handler(
        self, function_name: str, args: Dict[str, Any]
    ) -> None:
        """Speak what the early fire could not: the dynamic argument, or the
        whole fixed line when the early fire was skipped. Never raises."""
        try:
            entry = self._says.get(function_name)
            if entry is None:
                return
            already_fired = self.consume_pending(function_name)
            mapping = {**(self._template_vars_getter() or {}), **(args or {})}
            if entry["arg"]:
                # The argument value is appended below, exactly once — keep
                # its placeholder out of the prefix render (a template var
                # of the same name would otherwise speak a stale lead value).
                mapping.pop(entry["arg"], None)
            parts = []
            fixed = entry["say"] or (entry["prefix"] if entry["arg"] else None)
            if fixed and not already_fired:
                # Late placeholders: second substitution pass, strip leftovers.
                text = re.sub(
                    r"\s+", " ", _PLACEHOLDER_RE.sub("", _render(fixed, mapping))
                ).strip()
                if text:
                    parts.append(text)
            dynamic = (args or {}).get(entry["arg"]) if entry["arg"] else None
            if isinstance(dynamic, str) and dynamic.strip():
                parts.append(dynamic.strip())
            if parts:
                target = self._target()
                if target is not None:
                    await self._queue_say(target, " ".join(parts))
        except Exception as e:
            logger.warning(
                f"[early-speech] handler speak failed for {function_name}: {e}"
            )

    @property
    def says(self) -> frozenset:
        """Names of the flow's say functions (the transition handler's
        empty-result contract keys off this set)."""
        return frozenset(self._says)

    def _target(self) -> Any:
        """Queue straight into the TTS service; fall back to the task."""
        tts = self._tts_getter() if self._tts_getter is not None else None
        return tts if tts is not None else self._task_getter()

    async def _queue_say(self, target: Any, text: str) -> None:
        """Queue every sentence inline, in order. TTS plays frames in queue
        order and flushes the unspoken remainder on interruption, so there
        is nothing to gain from a delayed background queue — and anything
        queued later (a second say call's line, the dynamic argument) must
        never land between this line's sentences."""
        for sentence in _split_sentences(text):
            await target.queue_frame(
                TTSSpeakFrame(text=sentence, append_to_context=True)
            )


def attach_early_speech(
    *,
    llm_service: Any,
    tts_service: Any,
    flow: Optional[Dict[str, Any]],
    task_getter: Callable[[], Any],
    template_vars_getter: Callable[[], Dict[str, Any]],
    extra_tool_names: Optional[Iterable[str]] = None,
) -> Optional[EarlySpeechRouter]:
    """Build, arm and attach a router; None (no-op) without say functions or
    a TTS service — realtime speech-to-speech pipelines have no TTS to
    synthesize say text, so they keep their pre-existing behavior."""
    says = _collect_say_functions(flow)
    if not says or tts_service is None:
        return None
    others = _collect_other_names(flow, says) | set(extra_tool_names or ())
    router = EarlySpeechRouter(
        says=says,
        task_getter=task_getter,
        template_vars_getter=template_vars_getter,
        tts_getter=lambda: tts_service,
        other_names=others,
    )
    router.attach(llm_service)
    dynamic = sum(1 for e in says.values() if e["arg"])
    logger.info(
        f"[early-speech] armed for {len(says)} say function(s) "
        f"({dynamic} with dynamic LLM text, {len(others)} other tool name(s) "
        f"guarded)"
    )
    return router
