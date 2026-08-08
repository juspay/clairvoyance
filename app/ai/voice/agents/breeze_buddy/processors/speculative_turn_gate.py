"""Speculative turn gate — main-LLM, plan B.

Owns the user turn when a template opts into speculative interim generation
(``configurations.enable_interim_llm_send.enable``). The **main LLM is the
brain**: a ``complete_fn`` (wired by the agent to the real LLM — same model,
the template's rendered system prompt, and the template's function schemas,
captured *without* auto-dispatch) is fired on each Soniox interim transcript to
pre-compute the bot's response. Only the **latest** result is held. On the
**final** transcript the held result is committed: the bot's line is spoken
(via DragonTTS) and — if the response carries a function call — the function is
dispatched through ``dispatch_fn`` (the existing function dispatcher). See
docs/SCRIPTED_RESPONSES_PLAN.md.

Plan B (latency-first, as agreed with the user):
  * Fire ``complete_fn`` on interims (cadence-capped); specs RACE in parallel
    (up to ``_MAX_CONCURRENT_SPECS`` in flight — the earliest has the most
    runway to finish before the final; over the cap the OLDEST is cancelled) and
    the LATEST COMPLETED one wins the held result. On the final we await the
    in-flight specs and normally commit the freshest completed interim result
    — EXCEPT when it was low-confidence (``SpecResult.low_confidence``: the
    completer hit its text-match safety net, or came back unparsable). Then we
    regenerate once on the clearer final text before committing, so a
    partial-interim misread can't lock in (clean turns skip this — zero added
    latency; a spec still in-flight at final is reused, not regenerated).
  * Function calls execute ONLY at the final-transcript commit, never during
    speculation (``_spec_one`` never dispatches). Speak the line first, then
    run the function.
  * No dead air: a pure-text line is ALWAYS spoken, even if it repeats the last
    spoken line. When a user doesn't cooperate (keeps restating the same thing)
    the model legitimately re-picks the same line — silencing it produces dead
    air, which is worse UX than a repeated question.

The LLM-call mechanism is injected (``complete_fn``) so the gate is
provider-agnostic (Azure / Gemini-Vertex / Claude-Vertex / OpenAI) and fully
testable offline with a fake. Opt-in: only created when the template enables
interim LLM send; absent that the pipeline is byte-for-byte the original.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger

# A main-LLM speculative completion: given the conversation history so far and
# the (possibly partial) user transcript, return the bot's spoken line and the
# name of any function the LLM wants to invoke. Must NOT auto-dispatch.
CompleteFn = Callable[[List[Dict[str, str]], str], Awaitable["SpecResult"]]
# Execute a captured function call for real (the proven function-dispatch
# path). ``args`` is the opaque dict of function arguments carried from the
# constrained result for capture (generic — the gate never names them).
# ``history`` is the conversation so far (role/content turns) so dispatch can
# recover values the model dropped (e.g. a rating stated a turn or two earlier).
DispatchFn = Callable[
    [str, Optional[Dict[str, Any]], Optional[List[Dict[str, str]]]], Awaitable[None]
]

_MAX_SPECS_PER_TURN = 20  # cap speculative calls per user turn (cost/resource guard)
# Max speculative calls kept IN-FLIGHT at once. Interim specs race in parallel
# (the earliest has the most runway to finish before the final); when a new spec
# fires over this cap, the OLDEST in-flight is cancelled. Bounds per-turn cost on
# long utterances while preserving the latency-hide win for slow models.
_MAX_CONCURRENT_SPECS = 3
# Bound waiting for in-flight specs at final. They race; whichever finish within
# this window populate the held result (latest completed wins). The rest are
# cancelled and we fall back to regenerating on the final if none finished.
_AWAIT_INFLIGHT_TIMEOUT = 1.0
_INLINE_COMPLETE_TIMEOUT = (
    3.0  # bound the fail-open / low-confidence inline complete on final
)
_HISTORY_CAP = 16  # max (role, text) turns retained as multi-turn context


@dataclass
class SpecResult:
    """One speculative main-LLM completion result.

    ``text`` is what the bot speaks (the resolved pre-written line, or None for
    silence). ``function``/``function_args`` are the captured action to run at
    commit (name + opaque args dict, carried generically to dispatch).
    """

    text: Optional[str] = None  # the bot's spoken line, or None (silence)
    function: Optional[str] = None  # function name to dispatch, or None
    error: Optional[str] = None  # non-None if the completion failed
    function_args: Optional[Dict[str, Any]] = None  # opaque args for the function
    # True when the line was recovered via the completer's text-match safety
    # net (model emitted line prose, not a clean number) or the reply was
    # unparsable. The gate then regenerates once on the clearer final transcript
    # before committing, instead of trusting a partial-interim misread.
    low_confidence: bool = False
    # The (possibly partial) user transcript this result was generated from.
    # Lets the gate tell when a confident result actually ran on a short PARTIAL
    # interim the user then completed (e.g. 'हाँ, ब' -> a pardon line, when the
    # final was 'हाँ, बोलो।') so it regenerates on the clearer final. ``None``
    # (e.g. older callers / tests) disables the partial-prefix check.
    spec_text: Optional[str] = None


def _normalize(text: str) -> str:
    """Normalize spoken text for duplicate comparison."""
    out = (text or "").strip().lower()
    # Collapse whitespace and strip trailing punctuation/noise that shouldn't
    # distinguish two otherwise-identical lines.
    out = " ".join(out.split())
    out = out.rstrip("।.?!,;:")
    return out


def _spec_was_on_short_partial(spec_text: Optional[str], final_text: str) -> bool:
    """Did the held speculation run on a much-shorter partial of the final?

    A spec that fires on a partial interim which is a strict *prefix* of the
    final but materially shorter (the user was still talking — e.g. the interim
    ``'हाँ, ब'`` was later completed to ``'हाँ, बोलो।'``) can resolve a clean,
    confident line that is the WRONG turn: a half-word often reads as a pardon.
    The gate then regenerates once on the clearer final before committing — the
    same bounded fallback used for low-confidence results.

    A near-complete prefix (``len(spec) >= 0.7 * len(final)``) already carries
    the turn's meaning, so it is left alone and the latency-free commit path is
    preserved for normal turns. Returns ``False`` when ``spec_text`` is unset.
    """
    s = " ".join((spec_text or "").lower().split())
    f = " ".join((final_text or "").lower().split())
    if not s or not f or s == f:
        return False
    if not f.startswith(s):
        return False
    return len(s) < len(f) * 0.7


class SpeculativeTurnGate(FrameProcessor):
    """Speculatively run the main LLM on interims; commit the latest on final.

    Interim specs RACE in parallel (no fire-time cancel); the latest COMPLETED
    one (highest seq) wins the held result, so a stale earlier interim can never
    leak a superseded choice into this turn. At final the in-flight specs are
    awaited (reused, not regenerated) and the freshest completed result is
    committed. The finalize runs in a background task so ``process_frame`` never
    blocks on the LLM — barge-in and other frames keep flowing while the
    response is being chosen.
    """

    def __init__(
        self,
        complete_fn: CompleteFn,
        dispatch_fn: DispatchFn,
        *,
        cadence_ms: Optional[int] = 180,
        max_specs_per_turn: int = _MAX_SPECS_PER_TURN,
        max_concurrent_specs: int = _MAX_CONCURRENT_SPECS,
        await_inflight_timeout: float = _AWAIT_INFLIGHT_TIMEOUT,
        inline_complete_timeout: float = _INLINE_COMPLETE_TIMEOUT,
        history_cap: int = _HISTORY_CAP,
        speculate_on_interims: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._complete_fn = complete_fn
        self._dispatch_fn = dispatch_fn
        self._cadence_ms = cadence_ms
        self._max_specs_per_turn = max_specs_per_turn
        self._max_concurrent = max_concurrent_specs
        self._await_inflight_timeout = await_inflight_timeout
        self._inline_complete_timeout = inline_complete_timeout
        self._history_cap = history_cap
        # When False (constrained-only, no interim speculation) _on_interim is a
        # no-op: the turn is completed by _on_final -> _finalize, which runs
        # complete_fn ONCE on the final transcript (held stays None -> the
        # existing "no held result" regen path) and commits. One LLM call/turn.
        self._speculate_on_interims = speculate_on_interims

        self._task: Optional[Any] = None  # set via set_task() after PipelineTask exists

        # Speculation state. Interim specs RACE in parallel (up to
        # _max_concurrent in flight at once); _held holds the LATEST COMPLETED
        # result — the highest-seq interim that actually returned — tracked via
        # _held_seq. A later interim that finishes overrides an earlier one.
        self._held: Optional[SpecResult] = None
        self._held_seq: int = -1  # seq of the current _held; -1 = none yet
        self._inflight: Dict[int, asyncio.Task] = {}  # seq -> running spec task
        self._fire_seq: int = 0
        self._last_fire_ts: float = 0.0
        self._last_interim: str = ""  # last interim fired (skip identical re-fires)
        self._spec_count: int = 0  # speculative calls fired this turn (cost cap)

        # Turn state.
        self._finalize_task: Optional[asyncio.Task] = None
        self._busy: bool = False  # a finalize is in progress -> drop duplicate finals
        # True once the current turn's finalize has reached _commit (about to
        # speak / dispatch). Guards _on_interruption: an interruption that lands
        # BEFORE the bot has spoken (no speech to interrupt yet — often the
        # LLMUserAggregator echoing "user started speaking" for the user's OWN
        # committed final on turns that arrived final-only, no interims) must
        # NOT cancel the in-flight finalize, or the bot goes mute. Set False at
        # the start of each turn (_on_final) and after a full interruption.
        self._finalize_spoke: bool = False
        self._history: List[Dict[str, str]] = []  # conversation so far (multi-turn ctx)
        self._last_spoken: Optional[str] = None  # last line spoken (diagnostics)

    # -- wiring ---------------------------------------------------------------
    def set_task(self, task: Any) -> None:
        """Inject the PipelineTask (for queue_frame). Called after task creation."""
        self._task = task

    def seed_history(self, role: str, content: str) -> None:
        """Seed a turn into the conversation buffer (e.g. the out-of-band greeting).

        Only seeds while the buffer is empty, so real turns are never overwritten.
        """
        if not content or self._history:
            return
        self._history.append({"role": role, "content": content})

    # -- frame handling -------------------------------------------------------
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            logger.info("[spec-gate] StartFrame received")
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._close()

        if isinstance(frame, InterruptionFrame):
            # Barge-in: discard any held speculation and cancel the in-flight gen.
            await self._on_interruption()

        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, InterimTranscriptionFrame) and frame.text:
                await self._on_interim(frame.text)
            elif isinstance(frame, TranscriptionFrame) and frame.text:
                await self._on_final(frame.text)

        await self.push_frame(frame, direction)

    # -- speculation ----------------------------------------------------------
    async def _on_interim(self, text: str) -> None:
        if not self._speculate_on_interims:
            # Constrained-only mode: do not speculate on interims. The turn is
            # still owned by the gate — _on_final -> _finalize runs complete_fn
            # once on the FINAL transcript (held stays None -> the existing
            # "no held result" path) and commits. One LLM call/turn, no interim
            # cost.
            return
        if self._busy:
            # A finalize (commit + possible regenerate) is in progress for the
            # current turn. This interim belongs to the NEXT turn — don't spec on
            # it now: history is stale (current turn not recorded yet) and a
            # concurrent spec would race the in-flight commit. The next interim
            # after _busy clears fires normally.
            logger.debug(f"[spec-gate] interim skipped: finalize busy ({text[:30]!r})")
            return
        if self._spec_count >= self._max_specs_per_turn:
            logger.debug(
                f"[spec-gate] interim skipped: cap reached ({self._max_specs_per_turn})"
            )
            return
        now = time.monotonic()
        if (
            self._cadence_ms
            and self._last_fire_ts
            and (now - self._last_fire_ts) * 1000 < self._cadence_ms
        ):
            logger.debug(
                f"[spec-gate] interim throttled by cadence "
                f"({(now - self._last_fire_ts) * 1000:.0f}ms < {self._cadence_ms}ms): "
                f"{text[:30]!r}"
            )
            return  # within cadence window -> skip this interim
        if text.strip() == self._last_interim.strip():
            logger.debug(f"[spec-gate] interim identical to last fired: {text[:30]!r}")
            return  # identical to the last fired interim -> nothing new to do
        self._last_interim = text
        self._last_fire_ts = now
        self._spec_count += 1
        self._fire_seq += 1
        seq = self._fire_seq
        logger.debug(f"[spec-gate] interim fire #{seq}: {text[:40]!r}")

        # Race: let prior in-flight specs keep running — the earliest has the
        # most runway to finish before the final (the latency-hide win on slow
        # models). When over the cap, cancel the NEWEST existing spec (least
        # progress, most expendable) so the earliest keeps its runway and the
        # new (most-accurate transcript) one joins the race.
        if len(self._inflight) >= self._max_concurrent:
            self._inflight.pop(max(self._inflight)).cancel()
        self._inflight[seq] = asyncio.create_task(self._spec_one(seq, text))

    async def _spec_one(self, seq: int, text: str) -> None:
        """Fire one main-LLM completion. The LATEST COMPLETED spec (highest seq)
        wins _held — a later interim that finishes overrides an earlier one, so
        the held result is always from the most-complete transcript that has
        actually returned. Never dispatches — function calls run only at commit.
        """
        try:
            result = await self._complete_fn(list(self._history), text)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — never let speculation kill the pipeline
            logger.warning(
                f"[spec-gate] speculate #{seq} failed: {type(e).__name__}: {e}"
            )
            return  # don't clobber a good held from another spec with this failure
        finally:
            self._inflight.pop(seq, None)  # done/cancelled -> prune from the race
        if result.error:
            # A soft error must not clobber a good held from another spec (a
            # later-interim failure shouldn't discard an earlier good result).
            # If no good result exists, _held stays None and _finalize
            # regenerates on the final — same fallback, no silent overwrite.
            logger.warning(
                f"[spec-gate] speculate #{seq} on {text[:30]!r} -> ERR:{result.error} "
                f"(held untouched)"
            )
            return
        _t = repr((result.text or "")[:25])
        _lc = " LOW-CONF" if result.low_confidence else ""
        verdict = f"text={_t} fn={result.function}{_lc}"
        # Latest COMPLETED interim wins (a later seq that returns overrides an
        # earlier completed one). Stale tasks from a prior turn see seq <
        # _held_seq (bumped at the turn boundary) and can't write.
        if seq >= self._held_seq:
            self._held = result
            self._held_seq = seq
            logger.info(
                f"[spec-gate] speculate #{seq} on {text[:30]!r} -> {verdict} (held)"
            )
        else:
            logger.debug(
                f"[spec-gate] speculate #{seq} on {text[:30]!r} -> {verdict} "
                f"(superseded by later completed #{self._held_seq})"
            )

    async def _on_final(self, text: str) -> None:
        # Drop duplicate finals for the same utterance; reset per-turn counters so
        # the next turn gets a fresh speculation budget.
        logger.info(
            f"[spec-gate] final received text={text[:50]!r} busy={self._busy} "
            f"task_set={self._task is not None}"
        )
        if self._busy:
            logger.warning("[spec-gate] final dropped: finalize already in progress")
            return
        self._busy = True
        self._spec_count = 0
        self._last_fire_ts = 0.0  # next turn's first interim isn't wrongly throttled
        self._last_interim = ""
        self._finalize_spoke = False  # new turn — nothing spoken yet
        # Schedule the finalize OFF the frame loop so barge-in / other frames keep
        # flowing while we wait on the LLM.
        if self._finalize_task is not None and not self._finalize_task.done():
            self._finalize_task.cancel()
        self._finalize_task = asyncio.create_task(self._finalize(text))

    async def _finalize(self, text: str) -> None:
        t0 = time.monotonic()
        last_bot = next(
            (m["content"] for m in reversed(self._history) if m["role"] == "assistant"),
            None,
        )
        held_pre = self._held
        logger.info(
            f"[spec-gate] finalize: user={text[:40]!r} "
            f"held={'yes' if held_pre else 'no'} "
            f"held_lowconf={bool(held_pre and held_pre.low_confidence)} "
            f"held_fn={held_pre.function if held_pre else None} "
            f"held_spec={((held_pre.spec_text if held_pre else None) or '')[:25]!r} "
            f"inflight={len(self._inflight)} "
            f"| ctx={len(self._history)}turns last_spoken={(self._last_spoken or '')[:25]!r} "
            f"last_bot={(last_bot or '')[:25]!r}"
        )
        try:
            # Race: let the in-flight interim specs finish instead of throwing
            # away their work and regenerating from scratch. Whichever complete
            # within the window populate _held (latest completed wins); the
            # earliest-started has the most progress, so this usually beats a
            # fresh call. Cancel the rest after the window and fall back to
            # regenerating on the final only if none produced a held result.
            if self._inflight:
                pending = [t for t in self._inflight.values() if not t.done()]
                if pending:
                    _done, still_running = await asyncio.wait(
                        pending, timeout=self._await_inflight_timeout
                    )
                    for t in still_running:
                        t.cancel()
                    if still_running:
                        await asyncio.gather(*still_running, return_exceptions=True)
                        logger.info(
                            f"[spec-gate] {len(still_running)} spec(s) late on final"
                        )
            self._inflight.clear()

            held = self._held
            self._held = None
            self._fire_seq += 1  # invalidate any trailing inflight from this turn
            self._held_seq = self._fire_seq  # block stale writes; allow next-turn

            # Regenerate on the final when we don't have a trustworthy interim
            # result: (a) no speculation produced one (every spec failed/late),
            # (b) the freshest one errored, (c) it was low-confidence — the
            # completer hit its text-match safety net or came back unparsable,
            # which usually means it ran on a partial/misread interim — or (d)
            # it ran on a much-shorter PARTIAL prefix of the final (a confident
            # but wrong line: a half-word interim often reads as a pardon). The
            # final transcript is clearer, so get a second opinion before
            # committing. BOUNDED — a wedged LLM here must not pin _busy
            # forever (which would busy-drop every later turn and mute the bot).
            low_conf = bool(held and held.low_confidence)
            stale_partial = bool(
                held
                and not held.error
                and _spec_was_on_short_partial(held.spec_text, text)
            )
            if held is None or (held and held.error) or low_conf or stale_partial:
                why = (
                    held.error
                    if (held and held.error)
                    else (
                        "low-confidence interim result"
                        if low_conf
                        else (
                            "speculation ran on a short partial interim"
                            if stale_partial
                            else "no held result"
                        )
                    )
                )
                logger.info(f"[spec-gate] {why} -> regenerate on final")
                try:
                    regen = await asyncio.wait_for(
                        self._complete_fn(list(self._history), text),
                        timeout=self._inline_complete_timeout,
                    )
                    if regen is None or (regen and regen.error):
                        # Regen failed outright. If we still have the (low-
                        # confidence) interim `held`, fall back to it — speaking
                        # a safety-net line beats dead air. Only go silent when
                        # we genuinely have nothing.
                        if held is None or (held and held.error):
                            held = regen
                        else:
                            logger.info(
                                "[spec-gate] regen failed -> fall back to interim result"
                            )
                    else:
                        logger.info(
                            f"[spec-gate] regen ok -> "
                            f"fn={regen.function} low_conf={regen.low_confidence} "
                            f"speak={((regen.text or '')[:25])!r}"
                        )
                        held = regen
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[spec-gate] regenerate timed out "
                        f"({self._inline_complete_timeout}s)"
                        + (
                            " -> committing interim result"
                            if (held and not held.error)
                            else " -> silent this turn"
                        )
                    )
                    if held is None or (held and held.error):
                        held = None
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[spec-gate] regenerate failed: {type(e).__name__}: {e}"
                        + (
                            " -> committing interim result"
                            if (held and not held.error)
                            else " -> silent this turn"
                        )
                    )
                    if held is None or (held and held.error):
                        held = None

            await self._commit(held, text)
        except (
            Exception
        ) as e:  # noqa: BLE001 — never let a finalize crash kill the pipeline
            logger.error(f"[spec-gate] finalize failed: {type(e).__name__}: {e}")
        finally:
            self._busy = False
            logger.info(f"[spec-gate] finalize done in {time.monotonic() - t0:.3f}s")

    async def _commit(self, held: Optional[SpecResult], user_text: str) -> None:
        # We've crossed the point of no return for this turn: about to speak
        # and/or dispatch. Mark it so a concurrent interruption is treated as a
        # real barge-in (speech may be stopped downstream by pipecat) rather
        # than aborting the generation/dispatch — see _on_interruption.
        self._finalize_spoke = True
        line = (held.text or "").strip() if held else ""
        fn_name: Optional[str] = held.function if (held and held.function) else None
        is_repeat = bool(line) and _normalize(line) == _normalize(
            self._last_spoken or ""
        )

        if fn_name is not None:
            # A function call ACTS on the turn (end/transfer/update outcome).
            # ALWAYS speak its line (even if it repeats the last spoken line) so
            # the caller hears the closing line before e.g. a hang-up, and
            # ALWAYS run the function — never suppress an action.
            # Record the user turn BEFORE the assistant line so multi-turn
            # history alternates user -> assistant. Recording assistant-then-user
            # inverts the context the next turn's LLM sees (root cause of a live
            # call where the model kept mis-recognizing a one-word rating).
            self._record_turn("user", user_text)
            if line:
                logger.info(
                    f"[spec-gate] commit: speak + dispatch fn {fn_name!r} "
                    f"(repeat={is_repeat}) speak={line[:40]!r}"
                )
                await self._speak(line)
                self._last_spoken = line
                self._record_turn("assistant", line)
            else:
                logger.info(
                    f"[spec-gate] commit: dispatch fn {fn_name!r} (no closing line)"
                )
            # Speak-then-call: dispatch AFTER speaking. NOTE: still awaited on
            # the finalize task for now; the wiring step will move this to a
            # tracked background task so a slow function can't pin _busy.
            _args = held.function_args if held else None
            logger.debug(
                f"[spec-gate] dispatch {fn_name!r} args={_args} "
                f"history_turns={len(self._history)}"
            )
            try:
                await self._dispatch_fn(fn_name, _args, list(self._history))
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[spec-gate] function {fn_name!r} failed: "
                    f"{type(e).__name__}: {e}"
                )
            return

        # Pure-text turn (no function). ALWAYS speak the line — even on a repeat.
        # Dead air (silencing a legitimate re-ask when the user restates the same
        # thing) is the worst UX for a voice bot; a repeated question is fine.
        # The only silence here is a genuine no-line turn (model returned "." or
        # None). Still record the assistant line so multi-turn history keeps
        # alternating roles.
        # Record the user turn BEFORE the assistant line so multi-turn history
        # alternates user -> assistant (assistant-then-user inverts the context
        # the next turn's LLM sees).
        self._record_turn("user", user_text)
        if line:
            tag = " (repeat)" if is_repeat else ""
            logger.info(f"[spec-gate] commit: speak{tag} {line[:40]!r}")
            await self._speak(line)
            self._last_spoken = line
            self._record_turn("assistant", line)
        else:
            logger.info("[spec-gate] commit: no line (empty response) -> silent")

    async def _on_interruption(self) -> None:
        """Barge-in handling.

        Always cancels in-flight SPECULATIVE specs (pre-final). But an in-flight
        FINALIZE is only cancelled once the bot has already started speaking
        this turn (``_finalize_spoke``) — a real barge-in interrupting speech.

        An interruption that lands BEFORE the bot has spoken is spurious: there
        is no speech to interrupt. The common case is the LLMUserAggregator
        broadcasting "user started speaking" for the user's OWN just-committed
        final on turns that arrived final-only (no interims). Cancelling the
        finalize then aborts the LLM generation within milliseconds and leaves
        the bot mute. Let it finish; a genuine later barge-in (after _speak)
        still stops the audio downstream via the propagated InterruptionFrame.
        """
        if self._inflight:
            for t in self._inflight.values():
                t.cancel()
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)
            self._inflight.clear()
        finalize_running = (
            self._finalize_task is not None and not self._finalize_task.done()
        )
        if finalize_running and not self._finalize_spoke:
            logger.info(
                "[spec-gate] interruption during pre-speech finalize -> ignored "
                "(no speech to interrupt yet; letting generation finish)"
            )
            return
        if self._finalize_task is not None and not self._finalize_task.done():
            self._finalize_task.cancel()
            # Drain it so a mid-dispatch cancel doesn't leak an un-retrieved
            # exception; the dispatch side effect may be abandoned (see _close).
            await asyncio.gather(self._finalize_task, return_exceptions=True)
        self._finalize_task = None
        self._held = None
        self._busy = False  # the cancelled finalize's finally won't run; clear it
        self._finalize_spoke = False
        self._fire_seq += 1  # invalidate any trailing inflight
        self._held_seq = self._fire_seq  # ...and block stale writes from it
        # Reset per-turn speculation counters (mirror _on_final) so the NEXT
        # utterance starts fresh — otherwise its first interim can be wrongly
        # deduped against this (interrupted) turn's _last_interim, throttled by
        # its _last_fire_ts, or starved by its _spec_count.
        self._spec_count = 0
        self._last_fire_ts = 0.0
        self._last_interim = ""
        logger.info("[spec-gate] interruption -> cleared held + cancelled finalize")

    async def _speak(self, text: str) -> None:
        """Inject the line for TTS. Prefers task.queue_frame (proven path)."""
        logger.info(f"[spec-gate] _speak: {text[:60]!r}")
        frame = TTSSpeakFrame(text=text)
        if self._task is not None:
            await self._task.queue_frame(frame)
        else:
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)

    def _record_turn(self, role: str, content: str) -> None:
        """Append a turn to the conversation buffer (capped)."""
        if not content:
            return
        self._history.append({"role": role, "content": content})
        if len(self._history) > self._history_cap:
            del self._history[: len(self._history) - self._history_cap]

    async def _close(self) -> None:
        # Cancel and DRAIN both tasks. A finalize cancelled mid-`await
        # dispatch_fn(...)` abandons that side effect (CancelledError is a
        # BaseException, not caught by _finalize's `except Exception`); draining
        # with return_exceptions avoids un-retrieved-exception warnings on
        # shutdown. The abandoned side effect is accepted (the call is ending).
        tasks = [t for t in self._inflight.values() if not t.done()]
        if self._finalize_task is not None and not self._finalize_task.done():
            tasks.append(self._finalize_task)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._finalize_task = None
        self._busy = False
