"""
IVR mode walker — a pure DTMF state machine (no STT, no LLM, no Pipecat pipeline).

When a template's ``flow.mode == "ivr"`` the agent skips the streaming voice
pipeline entirely and runs this walker instead. Each node plays a TTS prompt
(synthesised once via the same batch TTS + Redis cache used by the answer-time
IVR in ``ivr/selection.py``) and waits for a keypad digit on the telephony
websocket. A pressed digit either transitions to another node (a sub-menu) or
ends the call.

Reuse notes:
- Audio synth/send: ``prepare_ivr_menu_audio`` + ``_send_audio`` from ``ivr/selection.py``.
- Finalisation: ``end_conversation`` (handlers/internal). It already guards the
  EndFrame on ``context.task`` (None here) and skips transcription when
  ``context.context`` is None, so it is reused verbatim against the pipeline-less
  ``Agent`` via ``TemplateContext``.
- Outcome persistence is fire-and-forget (``asyncio.create_task``) — exactly how
  hooks persist in flow/direct mode — so a DB write never blocks the menu.
"""

import asyncio
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast

from fastapi import WebSocket
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.ivr.selection import (
    _send_audio,
    prepare_ivr_menu_audio,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.hooks import HookRegistry
from app.ai.voice.agents.breeze_buddy.template.types import (
    HookConfig,
    IvrAction,
    IvrModeFlow,
    IvrNode,
    IvrOption,
)
from app.ai.voice.agents.breeze_buddy.tts import resolve_voice_config
from app.ai.voice.agents.breeze_buddy.utils.common import track_error
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import (
    close_websocket_safely,
    send_message,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_completion_details,
)

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.agent import Agent

# Safety cap so a mis-authored template (e.g. a transition cycle) can't loop forever.
MAX_NODE_TRANSITIONS = 25
# Wrong-key presses do NOT consume a no-input retry (callers always get the menu
# replayed after an "invalid" prompt). This generous cap only exists so a caller
# mashing wrong keys forever can't keep the call alive indefinitely.
MAX_INVALID_PRESSES = 5
DEFAULT_TIMEOUT_GOODBYE = "We didn't receive your input. Goodbye."
# Default outcome when a call ends with none set — customer hangup before choosing
# anything, or a no-input timeout on a node that doesn't configure on_timeout_outcome.
# Mirrors end_conversation_global.py's DEFAULT_OUTCOME; "BUSY" is retry-eligible
# (managers/calls.py), so the standard retry pipeline re-dials the customer.
INCOMPLETE_OUTCOME = "BUSY"
# Non-retryable outcome for IVR system/config errors (invalid flow, missing
# transport) so a broken template is NOT retried in a loop.
IVR_ERROR_OUTCOME = "IVR_ERROR"
# Extra seconds added on top of a prompt's estimated playback time before a
# no-input timeout fires — absorbs network/jitter so prompts aren't clipped or
# replayed on top of themselves.
PLAYBACK_GRACE_SECS = 0.5
# Max seconds _finalize_and_close waits for in-flight background tasks (outcome
# flushes, option hooks) to drain before the final end_conversation write. Bounds
# call teardown so a slow hook can't hang it; a single-row UPDATE finishes well
# under this.
BG_DRAIN_TIMEOUT = 5.0

# Signals returned by _run_node to the main loop.
_END = "end"  # terminal: finalise + hang up
_HANGUP = "hangup"  # customer dropped the call mid-menu
_TIMEOUT = "timeout"  # retries exhausted


class IvrWalker:
    """Walks a DTMF menu tree to completion over the raw telephony websocket."""

    def __init__(self, agent: "Agent") -> None:
        self.agent = agent
        # Telephony always provides these by the time IVR mode is dispatched
        # (ws accepted + stream_sid parsed in _setup_telephony_transport). Cast
        # for the type-checker; run() still validates at runtime before use.
        self.ws: WebSocket = cast(WebSocket, agent.ws)
        self.stream_sid: str = cast(str, agent.stream_sid)
        self.provider: str = cast(str, agent.provider)
        self.lead = agent.lead
        self.errors = agent.errors
        self.context = TemplateContext(agent)
        self.voice_config: Any = None
        # Opening-node fallback: when the initial node has no prompt of its own
        # it speaks the configured initial_greeting (already played once at call
        # start by send_initial_greeting). Captured here so it can be re-spoken
        # on a no-input retry and on every return-to-main.
        self.greeting_text: Optional[str] = getattr(agent, "greeting_text", None)
        # Synthetic transcript — same shape the pipeline writes to
        # metaData["transcription"] ([{role, content}]). Every spoken line is an
        # assistant turn, every keypress/event a user turn. IVR is deterministic,
        # so this needs no STT/LLM.
        self.transcript: List[Dict[str, str]] = []
        # Strong refs to in-flight fire-and-forget tasks (outcome flushes, option
        # hooks). asyncio only keeps weak refs, so without this a pending task can
        # be GC'd mid-execution ("Task was destroyed but it is pending!").
        # _finalize_and_close drains these before the final end_conversation write
        # so a stale intermediate flush can't clobber the full-metaData finaliser.
        self._bg_tasks: set[asyncio.Task] = set()

    # ── public entrypoint ──────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the IVR menu tree, then finalise and close the websocket."""
        if (
            self.agent.ws is None
            or self.agent.stream_sid is None
            or self.lead is None
            or self.agent.template is None
        ):
            logger.error(
                "[IVR] Missing ws/stream_sid/lead/template; cannot run IVR walker"
            )
            if self.lead is not None:
                self.lead.outcome = IVR_ERROR_OUTCOME
            await self._finalize_and_close(call_ended_by="system")
            return
        template = self.agent.template

        # Resolve the voice once (template tts_configuration -> defaults).
        tts_cfg = (
            self.agent.configurations.tts_configuration
            if self.agent.configurations
            else None
        )
        self.voice_config = await resolve_voice_config(tts_cfg)

        # Parse + validate the menu tree.
        try:
            flow = IvrModeFlow.model_validate(template.flow)
        except ValidationError as e:
            msg = f"[IVR] Invalid ivr flow for template: {e}"
            logger.error(msg)
            track_error(self.errors, msg)
            if self.lead is not None:
                self.lead.outcome = IVR_ERROR_OUTCOME
            await self._finalize_and_close(call_ended_by="system")
            return

        logger.info(
            f"[IVR] Starting IVR walker for call {self.agent.call_sid} "
            f"with {len(flow.nodes)} nodes, initial='{flow.initial_node}'"
        )

        # The opening greeting (if configured) was already played at call start by
        # send_initial_greeting; record it as the first assistant turn so it leads
        # the transcript.
        if self.greeting_text:
            self._add_turn("assistant", self.greeting_text)

        call_ended_by = "agent"
        try:
            call_ended_by = await self._walk(flow)
        except Exception as e:
            logger.error(f"[IVR] Walker error: {e}", exc_info=True)
            track_error(self.errors, f"IVR walker error: {e}")
            call_ended_by = "system"
        finally:
            await self._finalize_and_close(call_ended_by=call_ended_by)

    # ── tree walk ──────────────────────────────────────────────────────────

    async def _walk(self, flow: IvrModeFlow) -> str:
        """Drive the node loop. Returns who ended the call (for call_ended_by)."""
        current = flow.initial_node
        transitions = 0

        while True:
            if transitions > MAX_NODE_TRANSITIONS:
                logger.error(
                    f"[IVR] Exceeded {MAX_NODE_TRANSITIONS} transitions; "
                    "aborting to prevent a loop"
                )
                track_error(self.errors, "IVR transition limit exceeded")
                self._persist_outcome("IVR_LOOP_GUARD", {})
                await self._play(DEFAULT_TIMEOUT_GOODBYE, wait=True)
                return "system"

            node = flow.nodes.get(current)
            if node is None:
                logger.error(f"[IVR] Node '{current}' not found in template")
                track_error(self.errors, f"IVR node '{current}' not found")
                self._persist_outcome("IVR_NODE_MISSING", {})
                return "system"

            self.context.record_node_entry(current)
            kind, target = await self._run_node(
                node,
                # Greeting fallback applies to the initial node on EVERY entry
                # (incl. return-to-main); skip-replay only on the very first
                # entry, where send_initial_greeting already played it.
                is_initial_node=(current == flow.initial_node),
                external_greeting_pending=(transitions == 0),
            )

            if kind == "transition" and target:
                # Intermediate node finished — record exit and move on. Any
                # intermediate outcome was already persisted in _apply_option.
                self.context.record_node_exit()
                current = target
                transitions += 1
                continue

            # Terminal (end / hangup / timeout): leave the node "open" so
            # end_conversation's node_traversal finaliser closes it.
            return "customer" if kind == _HANGUP else "agent"

    async def _run_node(
        self,
        node: IvrNode,
        is_initial_node: bool = False,
        external_greeting_pending: bool = False,
    ) -> Tuple[str, Optional[str]]:
        """Play a node's prompt and collect a digit, with retries.

        Returns ``("transition", target)`` to move to another node, or
        ``(_END | _HANGUP | _TIMEOUT, None)`` for a terminal result.

        ``is_initial_node``: this node is ``flow.initial_node``. When it has no
        ``prompt`` of its own it falls back to the configured initial greeting
        (``self.greeting_text``) — on every entry, including return-to-main.
        Sub-nodes are never initial, so they never fall back and must define
        their own ``prompt``.

        ``external_greeting_pending``: True only on the very first node entry,
        where ``send_initial_greeting`` already played the greeting at call
        start. On that first iteration the walker measures the greeting's
        duration WITHOUT replaying it; every later iteration (no-input retry, or
        any re-entry) speaks it normally.
        """
        # Prompt-less initial node speaks the greeting; a prompt-less sub-node
        # has nothing to say (authoring error -> warn, then just listen).
        prompt_text = (
            node.prompt or (self.greeting_text if is_initial_node else None) or ""
        )
        if not prompt_text:
            logger.warning(
                f"[IVR] Node '{node.name}' has no prompt and no greeting fallback; "
                "it will listen without speaking"
            )

        by_digit: Dict[str, IvrOption] = {opt.digit: opt for opt in node.options}
        attempts = 0
        invalid_presses = 0
        first_iteration = True

        while attempts < max(1, node.max_retries):
            if first_iteration and external_greeting_pending and not node.prompt:
                # Greeting already audible from call start: measure only, don't
                # replay it (avoids greeting the caller twice).
                prompt_secs = await self._play(prompt_text, send=False)
            else:
                prompt_secs = await self._play(prompt_text)
            first_iteration = False

            try:
                # timeout_secs is the window to wait for a digit AFTER the prompt
                # finishes speaking. Add the prompt's own playback time (+grace)
                # so we never replay it on top of itself. Barge-in still works:
                # _wait_for_digit listens for the entire window, so a press during
                # the prompt is handled immediately.
                digit = await asyncio.wait_for(
                    self._wait_for_digit(),
                    timeout=prompt_secs + node.timeout_secs + PLAYBACK_GRACE_SECS,
                )
            except asyncio.TimeoutError:
                attempts += 1
                self._add_turn("user", "(no key pressed)")
                logger.info(
                    f"[IVR] No input on node '{node.name}' "
                    f"(attempt {attempts}/{node.max_retries})"
                )
                continue

            if digit is None:
                # stop event / socket closed -> customer hung up
                self._add_turn("user", "(caller hung up)")
                return (_HANGUP, None)

            option = by_digit.get(digit)
            if option is None:
                # Option A: a wrong key does NOT consume a no-input retry. Play
                # the invalid prompt, then loop to replay the menu and wait again
                # so the caller always gets another chance. Only true timeouts
                # (silence) count toward max_retries. A separate generous cap
                # bounds endless wrong-key mashing.
                invalid_presses += 1
                self.context.record_ivr_input(digit, invalid=True)
                self._add_turn("user", f"Pressed {digit} (not a valid option)")
                logger.info(
                    f"[IVR] Unmapped digit '{digit}' on node '{node.name}' "
                    f"(invalid press {invalid_presses}/{MAX_INVALID_PRESSES}; "
                    "retries not consumed)"
                )
                if invalid_presses >= MAX_INVALID_PRESSES:
                    logger.warning(
                        f"[IVR] Node '{node.name}' exceeded {MAX_INVALID_PRESSES} "
                        "invalid presses; ending call"
                    )
                    break
                if node.invalid_prompt:
                    await self._play(node.invalid_prompt, wait=True)
                continue

            self.context.record_ivr_input(
                digit,
                label=option.label,
                action=option.action.value,
                target_node=option.target_node,
                outcome=option.outcome,
            )
            self._add_turn(
                "user",
                f"Pressed {digit}" + (f" — {option.label}" if option.label else ""),
            )
            return await self._apply_option(option)

        # Retries exhausted. Persist the timeout outcome ONLY when the node sets
        # one; if it's unset, leave the outcome null so _finalize_and_close's
        # null -> BUSY default applies (a no-input timeout then retries, just like
        # a hangup). Explicit values (e.g. NO_RESPONSE, CANCELLED) are respected.
        logger.info(f"[IVR] Node '{node.name}' exhausted retries; ending call")
        if node.on_timeout_outcome:
            self._persist_outcome(node.on_timeout_outcome, {})
        await self._play(node.on_timeout_message or DEFAULT_TIMEOUT_GOODBYE, wait=True)
        return (_TIMEOUT, None)

    async def _apply_option(self, option: IvrOption) -> Tuple[str, Optional[str]]:
        """Apply a selected option: persist state, fire hooks, then act."""
        # Persist outcome/metadata immediately (in-memory + background DB write)
        # so a drop inside a sub-menu still records the last intent. Valid on
        # both transition and end.
        if option.outcome or option.metadata:
            self._persist_outcome(option.outcome, option.metadata)

        # External side-effect hooks: fire-and-forget, never block the menu.
        if option.hooks:
            self._spawn(self._run_hooks(option.hooks))

        if option.action == IvrAction.TRANSITION:
            if not option.target_node:
                logger.error(
                    f"[IVR] transition option for digit '{option.digit}' has no "
                    "target_node; ending call"
                )
                track_error(self.errors, "IVR transition missing target_node")
                return (_END, None)
            return ("transition", option.target_node)

        # END: optionally speak a closing message, then hang up.
        if option.message:
            await self._play(option.message, wait=True)
        return (_END, None)

    # ── audio + dtmf I/O (reuses ivr/selection.py primitives) ───────────────

    async def _play(self, text: str, wait: bool = False, send: bool = True) -> float:
        """Synthesise (cached) and send a prompt/message over the websocket.

        Returns the estimated playback duration in seconds (0.0 if nothing was
        synthesised) so the caller can start the input-wait window only AFTER
        the prompt has finished speaking.

        ``wait=False`` (menu prompts): return immediately so we can listen for a
        digit while the carrier plays it (barge-in supported). ``wait=True``
        (closing messages): sleep ~the audio's duration so a following hangup
        doesn't truncate it.

        ``send=False``: synthesise (cache-warm) and return the duration WITHOUT
        writing to the socket. Used for the opening node's first iteration, where
        send_initial_greeting already played this audio at call start — we only
        need its length to size the wait window, not a second playback.
        """
        if not text:
            return 0.0
        audio = await prepare_ivr_menu_audio(self.provider, text, self.voice_config)
        if not audio:
            logger.warning(f"[IVR] Failed to synthesise audio for: {text!r}")
            return 0.0
        if send:
            await _send_audio(self.ws, self.stream_sid, audio, self.provider)
            # Record what the agent spoke (send=False is the opening-greeting
            # measure, already logged once at walker start — don't double-log).
            self._add_turn("assistant", text)
        duration = self._audio_duration_secs(audio)
        if wait:
            await asyncio.sleep(duration + PLAYBACK_GRACE_SECS)
        return duration

    async def _wait_for_digit(self) -> Optional[str]:
        """Read the websocket until a DTMF digit arrives.

        Returns the pressed digit (any, so the caller can re-prompt on an
        unmapped key), or ``None`` if the call stopped / the socket closed.
        """
        provider_str = self._provider_str()
        async for message in self.ws.iter_text():
            try:
                data = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                continue

            event = data.get("event")
            if event == "dtmf":
                digit = (data.get("dtmf") or {}).get("digit")
                if digit:
                    logger.info(f"[IVR] DTMF digit received: {digit}")
                    # Barge-in: stop any in-flight prompt audio on Plivo.
                    if provider_str == "plivo":
                        await send_message(
                            ws=self.ws,
                            message={
                                "event": "clearAudio",
                                "streamId": self.stream_sid,
                            },
                        )
                    return str(digit)
            elif event == "stop":
                logger.info("[IVR] Call stopped (customer hangup)")
                return None
        return None

    # ── persistence + hooks (fire-and-forget, mirrors flow/direct mode) ─────

    def _persist_outcome(
        self, outcome: Optional[str], metadata: Dict[str, Any]
    ) -> None:
        """Apply outcome/metadata in-memory now; flush to DB in the background."""
        if not self.lead:
            return
        if outcome:
            self.lead.outcome = outcome
        if self.lead.metaData is None:
            self.lead.metaData = {}
        if metadata:
            self.lead.metaData.update(metadata)

        if self.lead.id:
            # Non-blocking: never stall the menu on a DB write (same principle
            # as hook persistence in transition.py). Drained in _finalize_and_close
            # before the final write so it can't clobber the full-metaData finaliser.
            self._spawn(
                self._flush_outcome(
                    self.lead.id, self.lead.outcome, dict(self.lead.metaData)
                )
            )

    async def _flush_outcome(
        self, lead_id: str, outcome: Optional[str], meta_data: Dict[str, Any]
    ) -> None:
        try:
            await update_lead_call_completion_details(
                id=lead_id,
                status=None,
                outcome=outcome,
                meta_data=meta_data,
                call_end_time=None,
            )
            logger.debug(
                f"[IVR] Persisted intermediate outcome '{outcome}' for {lead_id}"
            )
        except Exception as e:
            logger.error(
                f"[IVR] Background outcome flush failed for {lead_id}: {e}",
                exc_info=True,
            )

    async def _run_hooks(self, hook_dicts: list) -> None:
        """Execute option hooks (external side-effects). Mirrors _execute_hooks_async."""
        for hook_dict in hook_dicts:
            try:
                hook_config = HookConfig.model_validate(hook_dict)
                hook = HookRegistry.get(hook_config.name)
                if hook:
                    await hook.safe_execute(self.context, {}, "ivr_option", hook_config)
                else:
                    logger.warning(f"[IVR] Hook '{hook_config.name}' not in registry")
            except Exception as e:
                logger.error(f"[IVR] Hook execution error: {e}", exc_info=True)

    # ── finalisation ────────────────────────────────────────────────────────

    async def _finalize_and_close(self, call_ended_by: str) -> None:
        """Reuse end_conversation for DB/outcome/callbacks, then close the socket."""
        try:
            if self.lead is not None:
                if self.lead.metaData is None:
                    self.lead.metaData = {}
                self.lead.metaData.setdefault("call_ended_by", call_ended_by)
                # Persist the synthetic transcript (end_conversation leaves
                # transcription untouched for IVR since context.context is None).
                if self.transcript:
                    self.lead.metaData["transcription"] = self.transcript
                # No option/timeout set an outcome (e.g. customer hung up before
                # choosing, or a no-input timeout on a node with no configured
                # outcome) -> default to BUSY so the retry pipeline re-dials,
                # mirroring end_conversation_global's default.
                if not self.lead.outcome:
                    self.lead.outcome = INCOMPLETE_OUTCOME
            # Drain in-flight background flushes/hooks BEFORE the final write.
            # Each intermediate flush carries a metaData snapshot taken at press
            # time (no transcription / final node_traversal); letting one land
            # after end_conversation's full-overwrite write would clobber it. By
            # waiting here, end_conversation's write is the last writer and wins.
            await self._drain_bg_tasks()
            # end_conversation skips transcription (context.context is None) and
            # the EndFrame (context.task is None); it persists outcome+metaData
            # via the completion callback and runs end_conversation_callbacks.
            await end_conversation(self.context, {})
        except Exception as e:
            logger.error(f"[IVR] Finalisation error: {e}", exc_info=True)
        finally:
            ws = self.agent.ws
            if ws is not None:
                await close_websocket_safely(ws)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _spawn(self, coro) -> None:
        """Fire-and-forget a coroutine while retaining a strong ref.

        asyncio keeps only weak refs to tasks, so an unreferenced task may be
        GC'd before it finishes. Holding it in ``_bg_tasks`` (with a done-callback
        to discard) prevents that and lets ``_finalize_and_close`` drain them.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _drain_bg_tasks(self) -> None:
        """Wait for in-flight background tasks (bounded) so they can't outlive
        the final write. Tasks self-remove from the set via their done-callback."""
        pending = [t for t in self._bg_tasks if not t.done()]
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=BG_DRAIN_TIMEOUT)
        if still_pending:
            logger.warning(
                f"[IVR] {len(still_pending)} background task(s) did not drain "
                f"within {BG_DRAIN_TIMEOUT}s before finalisation"
            )

    def _add_turn(self, role: str, content: str) -> None:
        """Append a transcript turn (pipeline-compatible {role, content})."""
        if content:
            self.transcript.append({"role": role, "content": content})

    def _provider_str(self) -> str:
        p = self.provider
        return p.lower() if hasattr(p, "lower") else str(p).lower()

    def _audio_duration_secs(self, audio: bytes) -> float:
        """Approximate playback duration. Twilio/Plivo = mu-law (1 byte/sample),
        Exotel = PCM16 (2 bytes/sample); both at 8 kHz."""
        provider_str = self._provider_str()
        samples = (
            len(audio)
            if provider_str in ("twilio", "plivo")
            else max(1, len(audio) // 2)
        )
        return samples / 8000.0
