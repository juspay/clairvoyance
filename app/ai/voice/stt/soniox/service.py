"""Custom Soniox STT service with endpoint-recovery hardening.

Extends pipecat's SonioxSTTService with three production-incident hardenings
(2026-09-16, flipkart qwen templates: ~35-50s of dead air per affected turn):

- ``max_endpoint_delay_ms`` (pre-existing shim) — native endpoint-detection
  latency cap.
- **Endpoint watchdog** — pipecat pushes a final ``TranscriptionFrame`` ONLY
  when Soniox emits an ``<end>``/``<fin>`` token. The incident: the websocket
  session intermittently goes quiet right after an utterance's tokens are
  recognized — no ``<end>`` ever arrives, later utterances produce nothing,
  and the buffered finals flush only when the connection finally dies, so the
  user hears 35-50s of silence. The watchdog sends Soniox's
  ``{"type": "finalize"}`` when finalized tokens have sat un-endpointed for
  ``finalize_after_secs``; live-validated: finalize yields a ``<fin>`` token
  within ~300ms, which flushes the buffer and completes the user turn.
  The threshold is template-settable (``stt.soniox.finalize_after_secs``),
  falling back to the env default.
- **Tighter websocket liveness** — pipecat's ``websocket_connect`` call keeps the
  websockets library defaults (ping every 20s, 20s pong timeout, 10s close
  timeout), so a silently dead connection takes 20-40s to surface (the incident
  gaps sat exactly in that window). 3s/5s/3s cuts detection to <=11s worst
  case, after which the base class's automatic reconnect takes over.
- **Flush-on-disconnect** — pipecat flushes the finals buffer only on Soniox
  protocol events (``<end>``, error, finished); a silently dead connection
  produces none, so the caller's recognized words would be dropped with the
  session. The reconnect path runs through ``_disconnect_websocket``, which
  now emits any still-buffered finals first.
- **Observability** — every final transcript is INFO-logged with its
  utterance-to-final age (finals were previously invisible in prod), and each
  rescue path emits a greppable token — ``soniox_watchdog_rescue``,
  ``soniox_disconnect_flush``, ``soniox_late_final`` — with a per-connection
  ``soniox_call_stats`` summary line on disconnect (duration, finals,
  rescues). Call identification (call_sid/lead_id/conversation_id) rides
  along via the pipeline's loguru contextvars.

This remains a thin override — once pipecat supports these natively the module
can shrink back to the ``max_endpoint_delay_ms`` shim.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from loguru import logger
from pipecat.frames.frames import TranscriptionFrame
from pipecat.services.soniox.stt import (
    FINALIZE_MESSAGE,
    SonioxContextObject,
    SonioxSTTService,
    _prepare_language_hints,
)
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601
from websockets.asyncio.client import connect as websocket_connect
from websockets.protocol import State

#: Final age (time since Soniox's last output for the utterance) at which the
#: final-transcript INFO line escalates to WARNING. Healthy endpoints land at
#: ~0.5s and watchdog rescues at ~1.4s, so >= this means even finalize could
#: not save the turn in time (dead-socket path) — exactly what we want paged on.
_LATE_FINAL_WARN_SECS = 5.0


class SonioxSTTServiceWithEndpointDelay(SonioxSTTService):
    """SonioxSTTService with ``max_endpoint_delay_ms`` support and an
    endpoint watchdog that force-finalizes stale un-endpointed transcripts.

    When Soniox native endpoint detection is enabled
    (``vad_force_turn_endpoint=False``), ``max_endpoint_delay_ms`` controls
    the maximum delay (ms) between the end of speech and the returned
    ``<end>`` token. Allowed values: 500–3000 ms. Default from Soniox:
    2000 ms.

    ``finalize_after_secs`` arms the watchdog: when Soniox has finalized
    tokens buffered (speech recognized) but no endpoint token has arrived for
    that long, we send ``{"type": "finalize"}`` once per idle window to force
    the flush. ``None`` (or 0) disables the watchdog. With native endpoint
    detection on, the threshold is floored at ``2 * max_endpoint_delay_ms``
    (min 1.0s) so it can never preempt a healthy semantic endpoint.
    """

    def __init__(
        self,
        *,
        max_endpoint_delay_ms: Optional[int] = None,
        finalize_after_secs: Optional[float] = 1.0,
        ws_ping_interval: Optional[float] = 3.0,
        ws_ping_timeout: Optional[float] = 5.0,
        ws_close_timeout: Optional[float] = 3.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._max_endpoint_delay_ms = max_endpoint_delay_ms
        self._finalize_after_secs = finalize_after_secs
        self._ws_ping_interval = ws_ping_interval
        self._ws_ping_timeout = ws_ping_timeout
        self._ws_close_timeout = ws_close_timeout
        self._finalize_watchdog_task: Optional[asyncio.Task] = None
        self._finalize_sent_this_idle = False
        self._watchdog_finalizes_sent = 0
        self._buffered_since: Optional[float] = None
        # Per-connection observability counters (summarized on disconnect).
        self._finals_pushed = 0
        self._late_finals = 0
        self._disconnect_flushes = 0
        self._connected_at: Optional[float] = None
        # Native endpointing guarantees an <end> within max_endpoint_delay_ms
        # of speech end, so a watchdog tighter than ~2x that budget would
        # preempt healthy semantic endpoints — raise it to the floor instead.
        if (
            not self._vad_force_turn_endpoint
            and self._max_endpoint_delay_ms
            and self._finalize_after_secs
        ):
            floor = max(2 * self._max_endpoint_delay_ms / 1000, 1.0)
            if self._finalize_after_secs < floor:
                logger.warning(
                    f"soniox: finalize_after_secs={self._finalize_after_secs}s "
                    f"is below the max_endpoint_delay_ms budget floor "
                    f"({floor:.1f}s); raising it"
                )
                self._finalize_after_secs = floor

    @property
    def requires_vad_analyzer(self) -> bool:
        """Whether turn endpoints depend on a VAD analyzer being in the pipeline.

        With vad_force_turn_endpoint the service disables Soniox native
        endpointing and finalizes on every VADUserStoppedSpeakingFrame; without
        a VAD upstream that frame never arrives and finals flush only via the
        endpoint watchdog.
        """
        return self._vad_force_turn_endpoint

    async def _connect_websocket(self):
        """Override to inject ``max_endpoint_delay_ms`` + liveness settings."""
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return

            logger.debug("Connecting to Soniox STT (enhanced)")

            if self._ws_ping_interval is not None and self._ws_ping_timeout is not None:
                # close_timeout matters as much as the pings: after a missed
                # pong the library waits close_timeout (default 10s) for a
                # graceful close that a dead path can never answer — measured
                # detection was interval+timeout+~close_timeout.
                if self._ws_close_timeout is not None:
                    self._websocket = await websocket_connect(
                        self._url,
                        ping_interval=self._ws_ping_interval,
                        ping_timeout=self._ws_ping_timeout,
                        close_timeout=self._ws_close_timeout,
                    )
                else:
                    self._websocket = await websocket_connect(
                        self._url,
                        ping_interval=self._ws_ping_interval,
                        ping_timeout=self._ws_ping_timeout,
                    )
            else:
                self._websocket = await websocket_connect(self._url)

            if not self._websocket:
                await self.push_error(
                    error_msg=f"Unable to connect to Soniox API at {self._url}"
                )
                raise Exception(f"Unable to connect to Soniox API at {self._url}")

            enable_endpoint_detection = not self._vad_force_turn_endpoint

            s = self._settings

            context = s.context
            if isinstance(context, SonioxContextObject):
                context = context.model_dump()

            config = {
                "api_key": self._api_key,
                "model": s.model,
                "audio_format": self._audio_format,
                "num_channels": self._num_channels,
                "enable_endpoint_detection": enable_endpoint_detection,
                "sample_rate": self.sample_rate,
                "language_hints": _prepare_language_hints(
                    s.language_hints if isinstance(s.language_hints, list) else None
                ),
                "language_hints_strict": s.language_hints_strict,
                "context": context,
                "enable_speaker_diarization": s.enable_speaker_diarization,
                "enable_language_identification": s.enable_language_identification,
                "client_reference_id": s.client_reference_id,
            }

            # Inject max_endpoint_delay_ms when native endpoint detection is on
            if enable_endpoint_detection and self._max_endpoint_delay_ms is not None:
                config["max_endpoint_delay_ms"] = self._max_endpoint_delay_ms

            await self._websocket.send(json.dumps(config))

            await self._call_event_handler("on_connected")
            logger.debug("Connected to Soniox STT (enhanced)")

            # Watchdog lifecycle follows the websocket session: reconnects go
            # through _disconnect_websocket + _connect_websocket, so spawning
            # here (idempotently) covers both the initial connect and every
            # automatic reconnect.
            self._connected_at = time.time()
            if self._finalize_after_secs and not self._watchdog_alive():
                self._spawn_finalize_watchdog()
        except Exception as e:
            await self.push_error(
                error_msg=f"Unable to connect to Soniox: {e}", exception=e
            )
            raise

    async def _disconnect_websocket(self):
        """Override to stop the endpoint watchdog and rescue any transcript
        still buffered when the session ends.

        Pipecat flushes the finals buffer only on Soniox protocol events
        (``<end>``, error, finished) — a silently dead network path dies
        without any of those, so the caller's recognized words would be
        dropped with the session. The reconnect path runs through here, so
        flushing at disconnect covers that family too.
        """
        await self._flush_buffered_final_transcript()
        try:
            await super()._disconnect_websocket()
        finally:
            await self._stop_finalize_watchdog()
            self._finalize_sent_this_idle = False
            self._buffered_since = None
            self._log_call_stats()

    def _log_call_stats(self) -> None:
        """One structured line per connection (call or reconnect leg) so
        rescues are attributable and countable in OpenObserve:
        ``str_match(message, 'soniox_call_stats')`` — call_sid/lead_id/
        conversation_id ride along via the pipeline's loguru contextvars.
        """
        duration = (
            f"{time.time() - self._connected_at:.0f}s" if self._connected_at else "n/a"
        )
        client_ref = getattr(self._settings, "client_reference_id", None)
        level = (
            logger.warning
            if (
                self._watchdog_finalizes_sent
                or self._disconnect_flushes
                or self._late_finals
            )
            else logger.info
        )
        level(
            f"soniox_call_stats: duration={duration} "
            f"finals={self._finals_pushed} "
            f"watchdog_rescues={self._watchdog_finalizes_sent} "
            f"disconnect_flushes={self._disconnect_flushes} "
            f"late_finals={self._late_finals}"
            + (f" client_ref={client_ref}" if client_ref else "")
        )

    async def _flush_buffered_final_transcript(self) -> None:
        """Emit un-endpointed finals as a final TranscriptionFrame, if any.

        Mirrors pipecat's ``send_endpoint_transcript`` (same frame shape and
        bookkeeping); a no-op whenever the buffer is already empty, so it
        can never double-emit over pipecat's own flushes.
        """
        if not self._final_transcription_buffer:
            return
        text = "".join(
            token.get("text", "") for token in self._final_transcription_buffer
        )
        self._disconnect_flushes += 1
        # No transcript excerpt here: caller speech stays out of WARNING-level
        # logs — the INFO final-transcript line carries it already.
        logger.warning(
            f"soniox_disconnect_flush: rescuing "
            f"{len(self._final_transcription_buffer)} un-endpointed "
            f"final tokens ({len(text)} chars) on disconnect"
        )
        try:
            await self.push_frame(
                TranscriptionFrame(
                    text=text,
                    user_id=self._user_id,
                    timestamp=time_now_iso8601(),
                    result=self._final_transcription_buffer,
                    finalized=True,
                )
            )
            await self._handle_transcription(text, is_final=True)
            await self.stop_processing_metrics()
        except Exception as exc:  # pipeline may itself be tearing down
            logger.warning(f"soniox: disconnect flush push failed: {exc}")
        finally:
            self._final_transcription_buffer = []
            self._buffered_since = None

    def _spawn_finalize_watchdog(self) -> None:
        """Spawn the watchdog on the processor's task manager when linked
        into a pipeline, else on the bare event loop (pre-link/test calls)."""
        if getattr(self, "_task_manager", None) is not None:
            self._finalize_watchdog_task = self.create_task(
                self._finalize_watchdog_loop()
            )
        else:
            self._finalize_watchdog_task = asyncio.create_task(
                self._finalize_watchdog_loop()
            )

    async def _stop_finalize_watchdog(self) -> None:
        task, self._finalize_watchdog_task = self._finalize_watchdog_task, None
        if task is None:
            return
        try:
            await self.cancel_task(task)
        except Exception:  # noqa: BLE001 — no task manager (pre-link): bare cancel
            task.cancel()

    def _watchdog_alive(self) -> bool:
        return (
            self._finalize_watchdog_task is not None
            and not self._finalize_watchdog_task.done()
        )

    async def _finalize_watchdog_loop(self) -> None:
        logger.debug(
            f"soniox: endpoint watchdog armed (finalize after "
            f"{self._finalize_after_secs:.1f}s of un-endpointed finals)"
        )
        try:
            while True:
                await asyncio.sleep(0.25)
                try:
                    await self._finalize_watchdog_tick()
                except Exception as exc:  # noqa: BLE001 — keep the loop alive
                    logger.warning(f"soniox: endpoint watchdog tick failed: {exc}")
        except asyncio.CancelledError:
            raise

    async def _finalize_watchdog_tick(self) -> None:
        """One watchdog pass: force-finalize finals left un-endpointed.

        Fires at most once per idle window: fresh tokens or a flush re-arm it.
        A failed send (dead socket) is left to the ping/reconnect machinery.
        """
        if not self._finalize_after_secs:
            return
        ws = self._websocket
        if ws is None or ws.state is not State.OPEN:
            self._finalize_sent_this_idle = False
            # _buffered_since deliberately survives: it feeds final-age
            # logging during the close wait and the disconnect flush after it.
            return

        buffered = bool(self._final_transcription_buffer)
        if not buffered:
            self._buffered_since = None
            self._finalize_sent_this_idle = False
            return
        if self._buffered_since is None:
            self._buffered_since = time.time()

        # pipecat 1.1.0 stamps this but never reads it; getattr keeps the
        # watchdog from crashing if a patch release stops maintaining it.
        last = getattr(self, "_last_tokens_received", None)
        idle = last is not None and (time.time() - last) > self._finalize_after_secs
        if not idle:
            self._finalize_sent_this_idle = False
            return
        if self._finalize_sent_this_idle:
            return

        buffered_text = "".join(
            token.get("text", "") for token in self._final_transcription_buffer
        )
        try:
            await ws.send(FINALIZE_MESSAGE)
        except Exception as exc:  # dead socket: ping timeout + reconnect own it
            logger.warning(f"soniox: endpoint watchdog finalize send failed: {exc}")
            return
        self._finalize_sent_this_idle = True
        self._watchdog_finalizes_sent += 1
        # No transcript text at WARNING level (PII); the INFO final line that
        # follows within ~0.4s carries it. Token makes OpenObserve queries
        # trivial: str_match(message, 'soniox_watchdog_rescue').
        logger.warning(
            f"soniox_watchdog_rescue: finalized "
            f"{len(self._final_transcription_buffer)} un-endpointed tokens "
            f"({len(buffered_text)} chars) after >{self._finalize_after_secs:.1f}s "
            f"idle (call total rescues: {self._watchdog_finalizes_sent})"
        )

    async def _handle_transcription(
        self, transcript: str, is_final: bool, language: Optional[Language] = None
    ) -> None:
        """Override purely to surface finals at INFO (they were DEBUG-only)."""
        await super()._handle_transcription(transcript, is_final, language)
        if not is_final:
            return
        self._finals_pushed += 1
        # Age = how long the transcript sat after Soniox's LAST output for it
        # (healthy <end> ~0.5s, watchdog rescue ~1.4s, dead-socket flush 10s+).
        # Anchored on _last_tokens_received so a long-running utterance — whose
        # final tokens buffer progressively during speech — is not mistaken
        # for a late one; _buffered_since is only a fallback for a pipecat
        # that stops stamping the token timestamp.
        last = getattr(self, "_last_tokens_received", None)
        anchor = last if last is not None else self._buffered_since
        age = time.time() - anchor if anchor is not None else 0.0
        line = (
            f"soniox final: chars={len(transcript)} age={age:.1f}s "
            f"watchdog_finalizes={self._watchdog_finalizes_sent} "
            f"text={transcript[:80]!r}"
        )
        if age >= _LATE_FINAL_WARN_SECS:
            self._late_finals += 1
            # No transcript text at WARNING (PII); the INFO line carries it.
            logger.warning(
                f"soniox_late_final: rescue still took {age:.1f}s "
                f"(chars={len(transcript)} "
                f"watchdog_finalizes={self._watchdog_finalizes_sent})"
            )
        logger.info(line)
