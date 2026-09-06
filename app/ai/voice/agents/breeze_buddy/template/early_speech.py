"""Early speech: fire TTS on function-name decode for tool_based say tools.

The LLM streams a tool call as name fragments first, then argument fragments.
In tool_based mode the name ALONE determines what is spoken for split-per-
language and fixed-script functions — so the utterance can be queued for TTS
the moment the name unambiguously matches, without waiting for the arguments
to finish decoding.

Frames are queued DIRECTLY into the TTS service, not via ``task.queue_frame``
(which enters at the transport input). The pipeline is
``input → stt → gate → tap → user_agg → llm → prose_guard → tts → metrics →
output → assistant_agg``: the LLM processor handles a context frame by running
the entire stream inline, so a frame queued at the top sits behind it until
the stream completes — measured as ttfc ≈ LLM complete + ~2ms on every turn,
i.e. the early-fire gained nothing. Entering at the TTS service skips the LLM
gate entirely; the frame still flows through the metrics collector, the
output transport and the assistant aggregator, so the spoken text still
commits to the LLM context (``append_to_context=True``) and interruption
flushes still apply.

Safety rules:
- Outcome tools (``say.end_call``) fire too: their closing text is static, so
  speech does not depend on the arguments — the hook writes and
  ``end_conversation`` still run at function completion, on the handler path.
  The barge-in window between name-decode and completion is the recoverable
  failure already handled below (cancelled tail + model re-ask).
- Only fires when the partial name prefix matches EXACTLY ONE known speech
  tool — ambiguous prefixes wait for more fragments.
- Only fires when the rendered utterance has no unresolved ``{placeholder}``
  after template_vars substitution — lines that need an LLM argument (e.g.
  address read-backs) fall back to the normal handler path.
- The transition handler consumes a one-shot marker so the line is never
  queued twice; a repeat call of the same function (new tool_call_id) fires
  and re-arms normally.
- A user turn starting right after a fire (barge-in) cancels the sentence
  tail and clears the marker — the interruption flush already dropped the
  queued audio, and the cancelled handler will never consume the marker.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional

from pipecat.frames.frames import TTSSpeakFrame

from app.ai.voice.agents.breeze_buddy.template.tool_speech import (
    _PLACEHOLDER_RE,
    SENTENCE_QUEUE_GAP_SECS,
    render_say,
    split_say_for_tts,
)
from app.ai.voice.agents.breeze_buddy.template.types import SayConfig
from app.core.logger import logger

# A pending early-fire is only honored by the handler for this long. The
# handler runs <2s after the name in practice; a stream interrupted between
# the early fire and the handler would otherwise leave a stale marker that
# wrongly skips a later call of the same function.
_PENDING_TTL_SECS = 10.0

__all__ = ["EarlySpeechRouter", "attach_early_speech"]


class EarlySpeechRouter:
    """Matches streaming function-name fragments to say tools and queues the
    utterance early. Attached to the LLM service via the
    ``on_function_name_fragment`` hook (see pipecat base_llm patch)."""

    def __init__(
        self,
        functions: list[Dict[str, Any]],
        task_getter: Callable[[], Any],
        template_vars_getter: Callable[[], Dict[str, Any]],
        tts_getter: Optional[Callable[[], Any]] = None,
        early_say_note: Optional[Callable[[], None]] = None,
    ) -> None:
        # Outcome tools (say.end_call) are included: their closing is static
        # text, so it can fire at name-decode while the arguments (rating /
        # feedback) decode for another ~1s. The handler still runs hooks and
        # end_conversation at function completion — see the module safety
        # rules.
        self._says: Dict[str, Dict[str, Any]] = {
            f["function_name"]: f["say"]
            for f in functions
            if f.get("say") and f.get("function_name")
        }
        self._names = sorted(self._says)
        self._task_getter = task_getter
        self._template_vars_getter = template_vars_getter
        self._tts_getter = tts_getter
        self._early_say_note = early_say_note
        self._fired: set[str] = set()
        self._fire_tasks: set[asyncio.Task] = set()
        self._pending_function: Optional[str] = None
        self._pending_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._names)

    def attach(self, llm_service: Any) -> None:
        """Install the fragment hook on the LLM service (idempotent)."""
        if llm_service is not None and self._names:
            llm_service.on_function_name_fragment = self.on_name_fragment

    def consume_pending(self, function_name: Optional[str]) -> bool:
        """One-shot: True when this function's speech was already queued early.

        Called by the transition handler; consuming clears the marker so a
        later call of the same function goes through the normal path again.
        """
        if (
            function_name is not None
            and self._pending_function == function_name
            and (time.monotonic() - self._pending_at) <= _PENDING_TTL_SECS
        ):
            self._pending_function = None
            return True
        return False

    def cancel_pending_tails(self) -> None:
        """Drop early-fire leftovers when the user starts a barge-in.

        Called at user-turn start. An interruption around the name-decode
        cancels the tool call itself, so any not-yet-queued sentence tail
        would leak past the interruption flush and replay over the user's
        speech. The pending marker is cleared as well: the cancelled handler
        will never consume it, and a stale marker would wrongly mute the next
        call of the same function within the TTL window.
        """
        if self._fire_tasks:
            for t in self._fire_tasks:
                t.cancel()
            logger.debug(
                f"[early-speech] cancelled {len(self._fire_tasks)} "
                f"sentence tail(s) at user turn start"
            )
        self._pending_function = None
        self._pending_at = 0.0

    async def on_name_fragment(self, partial_name: str, tool_call_id: str) -> None:
        """LLM-service hook: called per streamed function-name fragment. Must
        never raise (the venv caller guards, but keep this side effect-free on
        error) and must return fast — it runs inline in the stream loop."""
        try:
            if not self._names or not partial_name:
                return
            key = tool_call_id or partial_name
            if key in self._fired:
                return
            matches = [n for n in self._names if n.startswith(partial_name)]
            if len(matches) != 1:
                return
            name = matches[0]
            if not tool_call_id and name != partial_name:
                # No tool_call_id yet and the name may still grow — wait for
                # the id so repeat calls of the same function dedup correctly.
                return

            say = SayConfig.model_validate(self._says[name])
            text, _language = render_say(say, {}, self._template_vars_getter() or {})
            if _PLACEHOLDER_RE.search(text):
                # The utterance needs an LLM argument — the normal handler
                # path renders it after the full decode.
                return

            task = self._task_getter()
            if task is None:
                return
            self._fired.add(key)
            self._pending_function = name
            self._pending_at = time.monotonic()
            # Sentence-split queueing, matching the LLM prose path: the FIRST
            # sentence queues inline (zero added latency inside the stream
            # loop), the rest follow via a gapped background task — this hook
            # must never sleep, it runs inline in the LLM stream.
            sentences = split_say_for_tts(text)
            # Queue straight into the TTS service when available: a
            # task-queued frame enters at the transport input and waits
            # behind the LLM processor's in-flight stream (module docstring).
            tts = self._tts_getter() if self._tts_getter is not None else None
            target = tts if tts is not None else task
            await target.queue_frame(
                TTSSpeakFrame(text=sentences[0], append_to_context=True)
            )
            if self._early_say_note is not None:
                # Metrics hook must never break the inline stream path.
                try:
                    self._early_say_note()
                except Exception:
                    pass
            if len(sentences) > 1:
                self._spawn_sentence_tail(target, name, sentences[1:])
            logger.info(
                f"[early-speech] {name} queued direct to TTS at name-decode "
                f"({len(sentences)} sentence(s), ahead of argument decode)"
            )
        except Exception as e:
            logger.warning(f"[early-speech] failed to fire early: {e}")

    def _spawn_sentence_tail(
        self, target: Any, name: str, sentences: list[str]
    ) -> None:
        """Queue the remaining sentences with the standard gap, off the LLM
        stream loop. Task refs are held so the tail can't be garbage-collected
        mid-flight."""

        async def _tail() -> None:
            try:
                for sentence in sentences:
                    await asyncio.sleep(SENTENCE_QUEUE_GAP_SECS)
                    await target.queue_frame(
                        TTSSpeakFrame(text=sentence, append_to_context=True)
                    )
            except Exception as e:
                logger.warning(f"[early-speech] {name} sentence tail failed: {e}")

        t = asyncio.create_task(_tail())
        self._fire_tasks.add(t)
        t.add_done_callback(self._fire_tasks.discard)


def attach_early_speech(
    *,
    llm_service: Any,
    bot: Any,
    flow: Dict[str, Any],
    early_say_note: Optional[Callable[[], None]] = None,
) -> Optional[EarlySpeechRouter]:
    """Build and attach an EarlySpeechRouter for a tool_based flow.

    Collects every say-equipped function across all nodes (the qwen tool_based
    templates are single-node; merging keeps multi-node flows working as long
    as function names stay unique). Returns None (and attaches nothing) when
    the flow has no say functions or is not tool_based.
    """
    if not flow or flow.get("mode") != "tool_based":
        return None
    functions: list[Dict[str, Any]] = [
        f for node in flow.get("nodes", []) for f in node.get("functions", [])
    ]
    router = EarlySpeechRouter(
        functions=functions,
        task_getter=lambda: getattr(bot, "task", None),
        template_vars_getter=lambda: getattr(bot, "template_vars", None) or {},
        tts_getter=lambda: getattr(bot, "tts_service", None),
        early_say_note=early_say_note,
    )
    router.attach(llm_service)
    if router.enabled:
        closings = sum(
            1
            for f in functions
            if f.get("say") and f.get("function_name") and f["say"].get("end_call")
        )
        logger.info(
            f"[early-speech] armed for {len(router._names)} say functions "
            f"({closings} end-call closing(s) fire early; hooks and "
            f"end_conversation still run at function completion)"
        )
    return router
