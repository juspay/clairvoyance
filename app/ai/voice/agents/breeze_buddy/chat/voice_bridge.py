"""WidgetVoiceBridge — drives widget voice turns through the chat brain.

Stream-mode widget voice runs **no LLM in the pipeline** (see
``agent/pipeline.py`` ``mode="stream"``). Each finished user turn
(``on_user_turn_stopped``) is handed to this bridge, which runs the SAME
``ChatAgent`` turn as ``POST /message`` (via :func:`run_chat_turn`) and:

- streams assistant prose back as ``TTSSpeakFrame``s (sentence-chunked) +
  ``assistant-transcript`` RTVI events so the widget renders the bubble,
- forwards ``ui_op`` events as ``ui-op`` RTVI events (carousels/Tiles),
- plays a short filler before a slow tool call (so the user isn't left in
  silence), and
- ends the turn with a ``turn-end`` RTVI event.

One brain, voice = pure audio I/O. cart_id, content_blocks history,
agent_state / client-context, carousels — all inherited from chat because it
*is* chat. See ``docs/widget/VOICE_AS_CHAT.md`` + the re-architecture plan.

Concurrency: the bridge holds exactly one in-flight turn task. A barge-in
(``on_user_turn_started`` → :meth:`cancel_inflight`) cancels it — pipecat's
native ``broadcast_interruption`` has already flushed TTS — and bumps a
generation counter so any tail frames from the cancelled turn are dropped.
The cancelled ``run_chat_turn`` generator's ``finally`` closes its aiohttp /
MCP pool, and the next turn's ``repair_dangling_tool_uses`` heals any tool_use
left dangling by the interrupt (exactly like chat's ``/cancel``).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, List, Optional

from pipecat.frames.frames import TTSSpeakFrame

from app.ai.voice.agents.breeze_buddy.chat.turn_core import (
    run_chat_approval_turn,
    run_chat_turn,
)
from app.ai.voice.agents.breeze_buddy.chat.ui_stream import strip_ui_stream_markers
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    list_chat_messages_for_session,
)
from app.schemas.breeze_buddy.chat import ChatMessageRole
from app.services.redis.locks import LockAcquireError, RedisLock

# Cap on a single TTSSpeakFrame (mirrors agent/__init__.py TTS_SPEAK_MAX_CHARS).
# Defined locally so this module doesn't import the Agent (which imports it).
_TTS_SPEAK_MAX_CHARS = 2000

# Per-session Redis lock — the SAME key + TTL the chat /message + /approval +
# /voice routes use (chat/handlers.py ``_lock_key``, widget/handlers.py
# ``_session_lock``). A voice turn writes chat_message rows whose idx is the
# non-atomic ``MAX(idx)+1`` — only race-free WHILE this lock serialises every
# writer of the session. While current_channel=VOICE the chat HTTP paths are
# already 409-gated, so this is rarely contended; it closes the post-flip window
# where a flip to CHAT races a still-writing bridge turn against a new /message.
# Key kept inline (not imported from the FastAPI router) so this stays importable
# in the voice subprocess.
_SESSION_LOCK_TTL_SECONDS = 180


def _lock_key(session_id: str) -> str:
    return f"chat:session:{session_id}:lock"


# Optional chunk-length floor for the sentence aggregator. 0 flushes every
# complete sentence the moment it lands (lowest time-to-first-audio); raise it
# to batch very short adjacent sentences into one TTS request.
_SENTENCE_MIN_CHARS = 0

# Spoken before the first tool call of a turn when the model hasn't said
# anything yet, so a slow tool (cart fetch, search) isn't dead air.
_DEFAULT_FILLER_PHRASE = "Just a second."

# RTVI server-message types the widget consumes. ``ui-op`` +
# ``function-approval-*`` match the agent-mode HITL / VoiceUiStreamProcessor
# wire contract, so the widget's existing handlers work unchanged.
_RTVI_ASSISTANT_TRANSCRIPT = "assistant-transcript"
_RTVI_UI_OP = "ui-op"
_RTVI_TURN_END = "turn-end"
_RTVI_ERROR = "error"
_RTVI_APPROVAL_REQUEST = "function-approval-request"
_RTVI_APPROVAL_RESOLVED = "function-approval-resolved"
# Tool-activity signals so the widget can show a "thinking / executing <tool>"
# state on the voice orb (names match the chat SDK's events for 1:1 reuse).
_RTVI_FUNCTION_CALL_STARTED = "function-call-started"
_RTVI_FUNCTION_CALL_COMPLETED = "function-call-completed"

# Agent._emit_rtvi_event(event_type, payload) — swallows its own exceptions.
EmitRtvi = Callable[[str, Optional[dict]], Awaitable[None]]


class _SentenceAggregator:
    """Buffer assistant tokens and flush complete sentences for TTS.

    Flushes the buffer up to and including a sentence-terminal punctuation
    (``. ! ? … \\n``) that is followed by whitespace / end-of-buffer / another
    terminal — so TTS speaks natural sentence units instead of word-by-word
    fragments, while ``3.5`` (terminal followed by a digit) is never split.
    With ``min_chars=0`` every complete sentence flushes the moment it lands;
    a positive ``min_chars`` batches shorter sentences. ``flush()`` drains
    whatever remains (called at turn end / before a tool call so held prose is
    spoken).
    """

    _TERMINALS = ".!?…\n"

    def __init__(self, min_chars: int = _SENTENCE_MIN_CHARS) -> None:
        self._buf = ""
        self._min = min_chars
        self.has_emitted = False

    @property
    def is_empty(self) -> bool:
        return not self._buf.strip()

    def push(self, text: str) -> List[str]:
        self._buf += text
        out: List[str] = []
        while True:
            cut = self._next_cut()
            if cut is None:
                break
            chunk = self._buf[:cut].strip()
            self._buf = self._buf[cut:]
            if chunk:
                self.has_emitted = True
                out.append(chunk)
        return out

    def _next_cut(self) -> Optional[int]:
        # Return the index AFTER the first sentence-terminal whose chunk is at
        # least ``min_chars`` long and is followed by whitespace / end-of-buffer
        # / another terminal. Scanning from 0 means a short complete sentence
        # flushes promptly (low latency) rather than merging with the next.
        n = len(self._buf)
        for i in range(n):
            if self._buf[i] in self._TERMINALS:
                j = i + 1
                if j < self._min:
                    continue
                if j >= n or self._buf[j].isspace() or self._buf[j] in self._TERMINALS:
                    return j
        return None

    def flush(self) -> List[str]:
        chunk = self._buf.strip()
        self._buf = ""
        if chunk:
            self.has_emitted = True
            return [chunk]
        return []


class WidgetVoiceBridge:
    """Drive one widget voice session's turns through the chat brain."""

    def __init__(
        self,
        *,
        session_id: str,
        task: Any,
        emit_rtvi: EmitRtvi,
        filler_phrase: str = _DEFAULT_FILLER_PHRASE,
    ) -> None:
        self.session_id = session_id
        self._task = task
        self._emit_rtvi = emit_rtvi
        self._filler_phrase = filler_phrase
        self._inflight: Optional["asyncio.Task[None]"] = None
        # Monotonic per-turn id. Bumped on every cancel / new turn so frames
        # from a superseded turn (cancelled mid-stream by a barge-in) are
        # dropped instead of leaking into the next turn's audio.
        self._generation = 0

    # -- turn lifecycle -----------------------------------------------------

    async def handle_user_turn(self, user_content: Optional[str]) -> None:
        """Entry point for ``on_user_turn_stopped`` — run one chat turn.

        Fire-and-forget: spawns a tracked task so the pipecat event handler
        returns immediately (the turn streams TTS frames in the background).
        Blank utterances (the aggregator emits an empty turn on some VAD
        edges) are ignored.
        """
        text = (user_content or "").strip()
        if not text:
            return
        # Fully drain (cancel + await) any prior turn so its Redis lock is
        # released before the new turn tries to acquire — otherwise a barge-in
        # quickly followed by a new utterance would self-contend and drop the
        # new turn.
        await self._drain()
        self._generation += 1
        gen = self._generation
        self._inflight = asyncio.create_task(self._run_turn(text, gen))

    async def cancel_inflight(self) -> None:
        """Barge-in (``on_user_turn_started``): stop the in-flight turn.

        pipecat's ``broadcast_interruption`` already flushed queued TTS before
        this fires; we just cancel the chat-turn task and bump the generation so
        any frame it tries to queue before unwinding is dropped. We do NOT await
        the task here (keep barge-in snappy) — the next ``handle_user_turn``'s
        ``_drain`` awaits its teardown (incl. the lock release) before starting
        the next turn. ``_inflight`` is left set so that drain can find it.
        """
        self._generation += 1
        task = self._inflight
        if task is not None and not task.done():
            task.cancel()

    async def aclose(self) -> None:
        """Teardown (end_conversation): cancel + await the in-flight turn so its
        Redis lock is released before the channel flips back to CHAT. Without
        this drain a flip-to-CHAT could let a new /message turn race the bridge's
        still-writing turn on the non-atomic chat_message idx allocation."""
        await self._drain()

    async def _drain(self) -> None:
        """Cancel the in-flight turn and AWAIT its full teardown (run_chat_turn's
        finally + the Redis lock release)."""
        task = self._inflight
        self._inflight = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 - drain must never propagate
            pass

    async def handle_approval_decision(
        self, tool_call_id: Optional[str], approved: bool, reason: Optional[str] = None
    ) -> None:
        """A HITL decision arrived over RTVI during a voice turn.

        Drives the resume turn through the chat brain (execute or deny the gated
        call, then continue speaking the reply) — the voice counterpart of
        ``POST /approval``. Same fire-and-forget + drain-prior shape as
        ``handle_user_turn``.
        """
        if not tool_call_id:
            return
        await self._drain()
        self._generation += 1
        gen = self._generation
        self._inflight = asyncio.create_task(
            self._run_approval(tool_call_id, approved, reason, gen)
        )

    async def _run_turn(self, user_content: str, gen: int) -> None:
        await self._drive(
            lambda: run_chat_turn(
                session_id=self.session_id, user_content=user_content
            ),
            gen,
        )

    async def _run_approval(
        self, tool_call_id: str, approved: bool, reason: Optional[str], gen: int
    ) -> None:
        await self._drive(
            lambda: run_chat_approval_turn(
                session_id=self.session_id,
                tool_call_id=tool_call_id,
                approved=approved,
                reason=reason,
            ),
            gen,
        )

    async def _drive(self, make_events: Callable[[], Any], gen: int) -> None:
        """Acquire the session lock, consume one chat-brain event stream into
        TTS + RTVI, and release the lock (cancel-safe). Shared by the spoken-turn
        and approval-decision paths."""
        lock = RedisLock(
            _lock_key(self.session_id), ttl_seconds=_SESSION_LOCK_TTL_SECONDS
        )
        try:
            await lock.acquire()
        except LockAcquireError:
            # Another writer holds the session lock — almost always a /message
            # racing this turn through the post-flip window. Skip rather than
            # risk a PK collision on the chat_message idx; the user can repeat.
            logger.warning(
                f"[voice-bridge] session {self.session_id} lock busy; skipping turn"
            )
            await self._emit_rtvi(
                _RTVI_ERROR, {"code": "busy", "message": "One moment, please."}
            )
            if self._inflight is asyncio.current_task():
                self._inflight = None
            return

        events = make_events()
        try:
            await self._consume_events(events, gen)
        except asyncio.CancelledError:
            # Barge-in / teardown. The CancelledError thrown into the async
            # generator already ran its finally (aiohttp + MCP close) as it
            # propagated; the next turn's repair heals any dangling tool_use.
            # Uncancel so the shielded lock release below isn't re-cancelled
            # (Python 3.11 cancel-counter gotcha — same fix the chat SSE stream
            # uses); we don't re-raise, the turn is done.
            cur = asyncio.current_task()
            if cur is not None:
                try:
                    cur.uncancel()
                except AttributeError:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"[voice-bridge] turn failed for session {self.session_id}: {exc}",
                exc_info=True,
            )
            await self._emit_rtvi(
                _RTVI_ERROR, {"code": "internal", "message": "voice turn failed"}
            )
        finally:
            # Shield the release so a cancellation that lands here still issues
            # the Redis DEL (else the lock lingers until its 180s TTL and the
            # next turn / message 409s).
            try:
                await asyncio.shield(lock.release())
            except Exception:  # noqa: BLE001
                pass
            if self._inflight is asyncio.current_task():
                self._inflight = None

    async def _consume_events(self, events: Any, gen: int) -> None:
        """Adapt one chat-brain SSE stream into TTS frames + RTVI events."""
        agg = _SentenceAggregator()
        filler_fired = False
        async for ev in events:
            if gen != self._generation:
                # Superseded by a newer turn without a task cancel (rare).
                # Close the generator so its aiohttp/MCP pool is released.
                await events.aclose()
                return
            if ev.event == "assistant_token":
                delta = ev.data.get("delta", "") if isinstance(ev.data, dict) else ""
                for chunk in agg.push(delta):
                    await self._speak(chunk, gen)
            elif ev.event == "ui_op":
                # TODO(voice-as-chat): flush the sentence aggregator before
                # emitting the ui-op, mirroring function_call_started /
                # function_approval_requested / turn_end below. A <ui_stream>
                # block is a hard turn boundary (the widget finalizes the open
                # transcript bubble on ui-op), but unlike those branches this one
                # does NOT agg.flush() first. If the prose before the marker ends
                # mid-sentence — no terminal punctuation/newline, e.g. a trailing
                # colon ("...best options:") — that tail stays buffered and gets
                # prepended to the post-carousel prose, leaking into the next
                # bubble. Low severity (the <ui_stream> marker is usually
                # newline-led, so the buffer is empty at the boundary), deferred.
                # Fix: `for chunk in agg.flush(): await self._speak(chunk, gen)`
                # immediately before the emit below. See docs/widget/
                # VOICE_GENERATIVE_UI_TODO.md.
                op = ev.data.get("op") if isinstance(ev.data, dict) else None
                if op is not None:
                    await self._emit_rtvi(_RTVI_UI_OP, {"op": op})
            elif ev.event == "function_call_started":
                # Speak any prose held before this tool call, then play a filler
                # only if the turn has been silent so far.
                for chunk in agg.flush():
                    await self._speak(chunk, gen)
                # Tell the widget a tool is running so the orb can show
                # "executing <tool>" (carries the name → TOOL_LABELS on the client).
                data = ev.data if isinstance(ev.data, dict) else {}
                await self._emit_rtvi(
                    _RTVI_FUNCTION_CALL_STARTED,
                    {
                        "name": data.get("name"),
                        "args": data.get("args") or {},
                        "tool_call_id": data.get("tool_call_id"),
                    },
                )
                if not filler_fired and not agg.has_emitted:
                    filler_fired = True
                    await self._speak_filler(gen)
            elif ev.event == "function_call_completed":
                # Tool finished — let the widget clear the "executing" state.
                data = ev.data if isinstance(ev.data, dict) else {}
                await self._emit_rtvi(
                    _RTVI_FUNCTION_CALL_COMPLETED,
                    {
                        "name": data.get("name"),
                        "tool_call_id": data.get("tool_call_id"),
                    },
                )
            elif ev.event == "function_approval_requested":
                # HITL gate hit mid-turn — speak any held prose, then surface the
                # approval card live over RTVI (same wire contract the agent-mode
                # HITL used, so the widget renders it unchanged). The decision
                # comes back via on_client_message → handle_approval_decision.
                for chunk in agg.flush():
                    await self._speak(chunk, gen)
                data = ev.data if isinstance(ev.data, dict) else {}
                # Speak the approval prompt so the voice user HEARS the question
                # (the card shows the same text plus the actual args). Without
                # this the card would appear silently mid-call. Audio only
                # (echo=False) — the inline card already renders this text, so
                # echoing a transcript bubble too would duplicate the line.
                prompt = data.get("prompt")
                if isinstance(prompt, str) and prompt.strip():
                    await self._speak(prompt, gen, echo=False)
                await self._emit_rtvi(
                    _RTVI_APPROVAL_REQUEST,
                    {
                        "approval_id": data.get("tool_call_id"),
                        "function_name": data.get("name"),
                        "arguments": data.get("args") or {},
                        "prompt": prompt,
                    },
                )
            elif ev.event == "function_approval_resolved":
                data = ev.data if isinstance(ev.data, dict) else {}
                await self._emit_rtvi(
                    _RTVI_APPROVAL_RESOLVED,
                    {
                        "approval_id": data.get("tool_call_id"),
                        "status": data.get("status"),
                        "reason": data.get("reason"),
                    },
                )
            elif ev.event == "turn_end":
                for chunk in agg.flush():
                    await self._speak(chunk, gen)
                status = (
                    ev.data.get("session_status") if isinstance(ev.data, dict) else None
                )
                await self._emit_rtvi(_RTVI_TURN_END, {"status": status})
            elif ev.event == "error":
                data = ev.data if isinstance(ev.data, dict) else {}
                await self._emit_rtvi(
                    _RTVI_ERROR,
                    {
                        "code": data.get("code"),
                        "message": data.get("message", "voice turn error"),
                    },
                )
            # user_committed / node_transition are intentionally not echoed: the
            # user transcript already reaches the widget from STT via pipecat RTVI.

    # -- emission helpers ---------------------------------------------------

    async def _speak(self, text: str, gen: int, *, echo: bool = True) -> None:
        if gen != self._generation:
            return
        # Defense-in-depth: assistant_token deltas are already <ui_stream>-
        # stripped by ChatAgent, but strip again so a stray marker never
        # reaches TTS.
        clean = strip_ui_stream_markers(text).strip()
        if not clean:
            return
        clean = clean[:_TTS_SPEAK_MAX_CHARS]
        if self._task is not None:
            await self._task.queue_frame(TTSSpeakFrame(text=clean))
        # `echo=False` speaks audio only (no transcript bubble). Used for the
        # HITL approval prompt: its text already renders on the inline approval
        # card, so echoing it as a transcript would show the same line twice.
        if echo:
            await self._emit_rtvi(
                _RTVI_ASSISTANT_TRANSCRIPT, {"content": clean, "final": False}
            )

    async def _speak_filler(self, gen: int) -> None:
        if gen != self._generation or self._task is None:
            return
        # No transcript echo — the filler is audio comfort, not assistant
        # content the widget should render or persist.
        await self._task.queue_frame(TTSSpeakFrame(text=self._filler_phrase))

    # -- greeting -----------------------------------------------------------

    async def maybe_speak_greeting(self) -> None:
        """Speak the persisted chat greeting on the FIRST voice attachment.

        Only when the session has no user turns yet — a brand-new conversation
        that went straight to voice. A reconnect mid-conversation does NOT
        re-greet (the widget already shows the history). No transcript echo:
        the greeting bubble is already rendered from the session's loaded
        messages, so we only push it to TTS.
        """
        if self._task is None:
            return
        try:
            messages = await list_chat_messages_for_session(self.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[voice-bridge] greeting load failed for {self.session_id}: {exc}"
            )
            return
        # Only REAL user speech/text suppresses the greeting. Tool-result rows
        # are persisted as role=USER with content=None (chat/agent.py); they
        # must not count as "the user already spoke".
        if any(m.role == ChatMessageRole.USER and m.content for m in messages):
            return  # mid-conversation reconnect — don't re-greet
        greeting = next(
            (
                m.content
                for m in messages
                if m.role == ChatMessageRole.ASSISTANT and m.content
            ),
            None,
        )
        if greeting:
            await self._task.queue_frame(
                TTSSpeakFrame(text=greeting[:_TTS_SPEAK_MAX_CHARS])
            )


__all__ = ["WidgetVoiceBridge"]
