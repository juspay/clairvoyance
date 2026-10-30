"""Phonon STT config and builder: Clairvoyance's client for Phonon.

Phonon is our self-hosted STT server (its own repo). Its protocol follows
Deepgram's live format:

    connect  wss://<phonon>/v1/listen?encoding=linear16&sample_rate=8000&channels=1
                 [&call_id=…][&language=…][&resume=true]
             header ``Authorization: Token <key>`` (none when no key is set)
    C→S <binary PCM16>
    C→S {"type":"Finalize"}       on VADUserStoppedSpeakingFrame (end of the turn)
    C→S {"type":"KeepAlive"}      while no audio flows, so Phonon's idle timeout
                                  never closes a live call
    C→S {"type":"CloseStream"}    at call end: Phonon sends owed finals, then closes
    S→C {"type":"Results", "is_final": …, "from_finalize": …,
         "channel": {"alternatives": [{"transcript": …}]}, …}
    S→C {"type":"Metadata", …}    after CloseStream

Each Results message carries the WHOLE utterance text so far (partials grow,
the final repeats it in full) and Phonon sends exactly one final per
utterance, so a final maps to one TranscriptionFrame with no merging here.

Close codes (reason is ``"<code>: <message>"``) decide what happens next, see
:func:`resume_delay`: draining/server errors/overload reconnect with
``resume=true`` and replay the unfinished turn; anything else, or a retry
budget spent, gives up. Giving up pushes one ErrorFrame carrying
:class:`PhononUnavailable`: behind :class:`PhononWithFallback` that moves
the rest of the call to the fallback STT (Sarvam); without one it is fatal.

Turn-taking stays Clairvoyance's job (pipeline VAD / turn detection); Phonon
only transcribes.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urlencode
from uuid import uuid4

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.service_switcher import (
    ServiceSwitcher,
    ServiceSwitcherStrategyFailover,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService, WebsocketSTTService
from pipecat.utils.time import time_now_iso8601
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.protocol import State

from app.core.logger import logger

__all__ = [
    "PhononConfig",
    "PhononSTTService",
    "PhononUnavailable",
    "PhononWithFallback",
    "build_phonon_stt",
    "resume_delay",
]

_LISTEN_PATH = "/v1/listen"
_FINALIZE = json.dumps({"type": "Finalize"})
_KEEPALIVE = json.dumps({"type": "KeepAlive"})
_CLOSE_STREAM = json.dumps({"type": "CloseStream"})

# Replay after a reconnect covers at most this much of the unfinished turn.
REPLAY_MAX_SECONDS = 30.0
# A slow upgrade is a failed one: the fallback is better than a long gap.
_OPEN_TIMEOUT_SECS = 5.0
# How long stop() waits for Phonon's owed finals after CloseStream (Phonon
# itself waits up to 5 s for them).
_CLOSE_STREAM_WAIT_SECS = 5.0

# Close codes with a reconnect budget: code -> the delay before each attempt.
# One attempt = one connect; a refused connect or HTTP 503 (server restarting
# or its model gone) moves on to the next attempt instead of giving up.
# 1001 draining: back off 0.5, 1, 2, 4 s (the next server, or this one back).
# 1011 server error: after 1 s, then 2 s (not at once: the dying server may
#   still answer for a moment). 1013 overloaded: once, after 0.5 s.
# 1006 (no close frame: network drop) and an unexpected 1000: at once, then 1 s.
# Every other code (1008 policy incl. unsupported_language, 1009 too big,
# unknown) gets no retry. Spent attempts are forgotten after a healthy
# _HEALTHY_RESET_SECS, so a long call can survive several blips.
_ABNORMAL = 1006
_RESUME_BUDGET: dict[int, tuple[float, ...]] = {
    1001: (0.5, 1.0, 2.0, 4.0),
    1011: (1.0, 2.0),
    1013: (0.5,),
    _ABNORMAL: (0.0, 1.0),
}
_HEALTHY_RESET_SECS = 60.0
# Speech end → final, p99, told to the pipeline (Pipecat's STTMetadataFrame).
# Measured on the GPU: 0.10 s at 100 calls, 0.51 s at 400.
_TTFS_P99_SECS = 0.5
_sleep = asyncio.sleep  # the resume back-off; tests make it instant
# HTTP statuses on a resume's upgrade worth another attempt (others give up).
_RETRY_HTTP = {502, 503, 504}
# Phonon accepts call ids of these characters, up to 128 (log safety).
_CALL_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")


def resume_delay(code: Optional[int], used: dict[int, int]) -> Optional[float]:
    """PURE: seconds to wait before the next resume attempt, or None to give up.

    ``used`` counts the attempts already spent per budget code; the caller
    records the spend. 1000 reaches here only when the client did not ask to
    close, so it counts as an abnormal close.
    """
    key = _budget_key(code)
    delays = _RESUME_BUDGET.get(key)
    spent = used.get(key, 0)
    if delays is None or spent >= len(delays):
        return None
    return delays[spent]


def safe_call_id(call_id: Optional[str]) -> str:
    """PURE: a call id Phonon accepts. Other characters become "-", at most
    128 long; an empty result gets a generated id."""
    cleaned = _CALL_ID_UNSAFE.sub("-", call_id or "")[:128]
    return cleaned if cleaned.strip("-") else f"bb-{uuid4().hex}"


def _budget_key(code: Optional[int]) -> int:
    return _ABNORMAL if code in (None, 1000, _ABNORMAL) else code


def _reason_code(reason: str) -> str:
    """Phonon's reasons read "<code>: <message>"; only the code is a contract."""
    return reason.split(":", 1)[0].strip()


class PhononUnavailable(Exception):
    """Phonon cannot serve the rest of this call (the fallback trigger)."""


@dataclass
class PhononConfig:
    """Configuration for the Phonon STT client.

    url: Phonon's base WebSocket address, e.g. ``wss://phonon.stt.svc:8098``
    (the ``/v1/listen`` path and query are added here).
    api_key: sent as ``Authorization: Token <key>``; empty = no header (local
    dev). Never in the URL, never logged (kept out of repr).
    language: one language code, passed through as-is; None = server's auto.
    call_id: sent to Phonon for log correlation and resume (letters, digits,
    . _ : -).
    """

    url: str
    api_key: str = field(default="", repr=False)
    language: Optional[str] = None
    call_id: Optional[str] = None


class PhononSTTService(WebsocketSTTService):
    """Streams one call's audio to Phonon and turns its Results into frames."""

    def __init__(
        self,
        *,
        url: str,
        api_key: str = "",
        language: Optional[str] = None,
        call_id: Optional[str] = None,
        fatal_on_failure: bool = True,
        **kwargs: Any,
    ):
        # keepalive_timeout: after this long without audio, Pipecat calls
        # _send_keepalive (a KeepAlive message, not silence) every interval.
        # Complete settings, or Pipecat logs an ERROR per call. The language
        # goes to Phonon in the URL; Pipecat's own field stays None.
        kwargs.setdefault("settings", STTSettings(model="phonon", language=None))
        kwargs.setdefault("ttfs_p99_latency", _TTFS_P99_SECS)
        super().__init__(keepalive_timeout=5.0, keepalive_interval=5.0, **kwargs)
        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._language = language
        # Resume needs the same call_id on every connection of this call; a
        # caller that names none gets a generated one.
        self._call_id = safe_call_id(call_id)
        # No fallback behind us: giving up must fail the call, not go silent.
        self._fatal_on_failure = fatal_on_failure
        self._receive_task = None
        # Audio of the current unfinished turn (since the last final), as
        # (seq, chunk), bounded to REPLAY_MAX_SECONDS; replayed on resume.
        self._turn: deque[tuple[int, bytes]] = deque()
        self._turn_bytes = 0
        self._seq = 0
        # One entry per Finalize whose final hasn't come back: the seq of the
        # last chunk before it. A final clears the buffer only up to its own
        # Finalize (later audio is the next turn), and a replay re-sends each
        # Finalize at its place in the audio, so turn boundaries survive.
        self._finalize_marks: deque[int] = deque()
        self._resumes_used: dict[int, int] = {}
        self._live_since: Optional[float] = None  # monotonic, current socket
        # VAD: the caller is mid-utterance (started, not yet stopped). Only then
        # does a hang-up need its own Finalize; trailing silence after the last
        # turn does not.
        self._user_speaking = False
        self._closing = False
        self._failed = False
        self._receive_done = asyncio.Event()

    def can_generate_metrics(self) -> bool:
        return True

    @property
    def failed(self) -> bool:
        """True once this call has given up on Phonon."""
        return self._failed

    # ---- lifecycle ---------------------------------------------------------------

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        """Ask Phonon for the finals already owed, wait briefly, then close."""
        await super().stop(frame)
        if self._websocket is not None and self._websocket.state is State.OPEN:
            self._closing = True
            if self._user_speaking and self._has_unfinalized_audio():
                # A hang-up mid-sentence (VAD never said the caller stopped):
                # finish that turn so its words arrive. Silence after the last
                # turn needs no Finalize: it would only produce an empty final.
                self._finalize_marks.append(self._seq)
                await self._send(_FINALIZE)
            await self._send(_CLOSE_STREAM)
            try:
                await asyncio.wait_for(
                    self._receive_done.wait(), _CLOSE_STREAM_WAIT_SECS
                )
            except asyncio.TimeoutError:
                logger.warning(f"{self}: Phonon did not close after CloseStream")
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    # ---- audio and turn events ---------------------------------------------------

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Send audio only; results arrive on the receive task."""
        if not self._failed:
            self._remember(audio)
            await self._send(audio)
        yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._user_speaking = True
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_speaking = False
        if isinstance(frame, VADUserStoppedSpeakingFrame) and not self._failed:
            # End of the caller's turn: Phonon returns the final at once instead
            # of waiting for more audio.
            self.request_finalize()
            self._finalize_marks.append(self._seq)
            await self._send(_FINALIZE)

    async def _send_keepalive(self, silence: bytes):
        # Phonon's keepalive is a message, not silent audio (silence would be
        # transcribed and would move the stream's clock).
        await self._send(_KEEPALIVE)

    def _remember(self, audio: bytes) -> None:
        """Keep the unfinished turn's audio, dropping what is older than 30 s."""
        self._seq += 1
        self._turn.append((self._seq, audio))
        self._turn_bytes += len(audio)
        cap = int(REPLAY_MAX_SECONDS * self.sample_rate * 2)  # PCM16 mono
        while self._turn and self._turn_bytes > cap:
            self._turn_bytes -= len(self._turn.popleft()[1])

    def _turn_finished(self) -> None:
        self._turn.clear()
        self._turn_bytes = 0
        self._finalize_marks.clear()

    def _has_unfinalized_audio(self) -> bool:
        last_mark = self._finalize_marks[-1] if self._finalize_marks else 0
        return bool(self._turn) and self._turn[-1][0] > last_mark

    def _turn_finished_up_to(self, seq: int) -> None:
        """A Finalize's final arrived: its audio is done; later audio (the
        caller's next turn) stays buffered for a replay."""
        while self._turn and self._turn[0][0] <= seq:
            self._turn_bytes -= len(self._turn.popleft()[1])

    # ---- connection --------------------------------------------------------------

    def connect_url(self, *, resume: bool = False) -> str:
        """Phonon's /v1/listen URL with this call's audio settings."""
        params: dict[str, str | int] = {
            "encoding": "linear16",
            "sample_rate": self.sample_rate,
            "channels": 1,
        }
        if self._call_id:
            params["call_id"] = self._call_id
        if self._language:
            params["language"] = self._language
        if resume:
            params["resume"] = "true"
        return f"{self._base_url}{_LISTEN_PATH}?{urlencode(params)}"

    async def _open(self, *, resume: bool) -> Any:
        headers = {"Authorization": f"Token {self._api_key}"} if self._api_key else {}
        return await websocket_connect(
            self.connect_url(resume=resume),
            additional_headers=headers,
            open_timeout=_OPEN_TIMEOUT_SECS,
        )

    async def _connect(self):
        await super()._connect()
        if self._failed:
            return
        await self._connect_websocket()
        if self._websocket and not self._receive_task:
            self._receive_done.clear()
            self._receive_task = self.create_task(
                self._receive_task_handler(self._report_error)
            )

    async def _disconnect(self):
        await super()._disconnect()
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        await self._disconnect_websocket()

    async def _connect_websocket(self):
        if self._websocket and self._websocket.state is State.OPEN:
            return
        logger.debug(f"{self}: connecting to Phonon at {self._base_url}")
        try:
            self._websocket = await self._open(resume=False)
        except Exception as e:
            # Rejected key (HTTP 401), refused, unreachable or too slow: none
            # gets better by retrying within a call.
            await self._give_up(f"connect failed: {type(e).__name__}: {e}")
            return
        self._live_since = time.monotonic()
        await self._call_event_handler("on_connected")

    async def _disconnect_websocket(self):
        try:
            if self._websocket:
                await self._websocket.close()
        except Exception as e:
            logger.warning(f"{self}: error closing Phonon websocket: {e}")
        finally:
            self._websocket = None
            await self._call_event_handler("on_disconnected")

    async def _send(self, data: bytes | str) -> None:
        if self._websocket and self._websocket.state is State.OPEN:
            try:
                await self._websocket.send(data)
            except Exception as e:
                # The receive loop sees the close and decides what follows.
                logger.warning(f"{self}: send to Phonon failed: {e}")

    # ---- receive loop, close policy, resume ------------------------------------

    async def _receive_task_handler(self, report_error: Any) -> None:
        """Receive until the call ends; on a close, resume or give up.

        Replaces Pipecat's generic retry loop: Phonon's close codes say what
        to do (see :func:`resume_delay`), and a resume must replay the turn.
        """
        try:
            while True:
                ws = self._websocket
                if ws is None:
                    return
                try:
                    await self._receive_messages()
                except ConnectionClosed:
                    pass  # close code/reason are on the socket
                except Exception as e:
                    logger.warning(f"{self}: error receiving from Phonon: {e}")
                if self._disconnecting or self._closing:
                    return
                code = getattr(ws, "close_code", None)
                reason = getattr(ws, "close_reason", None) or ""
                if not await self._after_close(code, reason):
                    return
        finally:
            self._receive_done.set()

    async def _after_close(self, code: Optional[int], reason: str) -> bool:
        """Resume after an unexpected close; False once the call gave up."""
        label = f"close {code} ({_reason_code(reason) or 'no reason'})"
        if (
            self._live_since is not None
            and time.monotonic() - self._live_since >= _HEALTHY_RESET_SECS
        ):
            self._resumes_used.clear()  # a healthy stretch: a fresh budget
        self._live_since = None
        self._websocket = None  # audio meanwhile is kept for the replay only
        key = _budget_key(code)
        why = label
        while True:
            delay = resume_delay(code, self._resumes_used)
            if delay is None:
                await self._give_up(why)
                return False
            self._resumes_used[key] = self._resumes_used.get(key, 0) + 1
            logger.warning(f"{self}: Phonon {why}; resuming in {delay:.1f}s")
            if delay:
                await _sleep(delay)
            try:
                ws = await self._open(resume=True)
            except InvalidStatus as e:
                status = e.response.status_code
                if status not in _RETRY_HTTP:  # e.g. 401: retrying can't help
                    await self._give_up(f"{label}, resume refused: HTTP {status}")
                    return False
                why = f"{label}, resume got HTTP {status}"
                continue
            except Exception as e:  # refused / timed out: maybe restarting
                why = f"{label}, resume failed: {type(e).__name__}: {e}"
                continue
            try:
                await self._replay(ws)
            except Exception as e:
                why = f"{label}, replay failed: {type(e).__name__}: {e}"
                continue
            self._live_since = time.monotonic()
            return True

    async def _replay(self, ws: Any) -> None:
        """Resend the unfinished turn on a resumed socket, then go live.

        Audio arriving during the replay joins the buffer and is sent here
        too, in order; the socket becomes live only once nothing is pending,
        with no await in between, so live audio never overtakes the replay.
        """
        sent = 0  # seq of the last chunk sent on this socket
        finalizes_sent = 0  # of the pending Finalizes, how many re-sent
        while True:
            # Re-read every pass: audio and Finalizes keep arriving during the
            # awaits below, and must go out here, in order, before going live.
            marks = list(self._finalize_marks)
            limit = marks[finalizes_sent] if finalizes_sent < len(marks) else None
            pending = [
                (seq, chunk)
                for seq, chunk in self._turn
                if seq > sent and (limit is None or seq <= limit)
            ]
            if pending:
                sent = pending[-1][0]
                for _, chunk in pending:
                    await ws.send(chunk)
                continue
            if limit is not None:
                # The turn's audio is out: its Finalize, then the next turn's.
                sent = max(sent, limit)
                finalizes_sent += 1
                await ws.send(_FINALIZE)
                continue
            # A full pass sent nothing, and nothing awaits between this check
            # and going live: live audio can't overtake the replay.
            self._websocket = ws
            return

    async def _give_up(self, reason: str) -> None:
        """Stop using Phonon for this call and say so once, upstream."""
        if self._failed:
            return
        self._failed = True
        self._turn_finished()
        target = "the call fails" if self._fatal_on_failure else "using the fallback"
        message = f"Phonon STT unavailable ({reason}); {target}"
        logger.warning(f"{self}: {message}")
        ws, self._websocket = self._websocket, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        await self._cancel_keepalive_task()
        # Raised and caught so the ErrorFrame's exception carries a traceback
        # (pipecat's error logging reads it).
        try:
            raise PhononUnavailable(message)
        except PhononUnavailable as e:
            await self.push_error(message, exception=e, fatal=self._fatal_on_failure)

    # ---- results -------------------------------------------------------------------

    async def _receive_messages(self):
        if self._websocket is None:
            return
        async for raw in self._websocket:
            if isinstance(raw, bytes):
                continue  # Phonon sends only text messages
            try:
                message = json.loads(raw)
            except ValueError:
                logger.warning(f"{self}: unreadable message from Phonon")
                continue
            await self.handle_message(message)

    async def handle_message(self, message: dict) -> None:
        """Turn one Phonon message into frames. Only Results carry text."""
        if message.get("type") == "Warning":
            # e.g. audio_dropped: this call's transcript is missing words.
            logger.warning(
                f"{self}: Phonon warning {message.get('code')} "
                f"(dropped {message.get('dropped_s', '?')}s): {message.get('message')}"
            )
            return
        if message.get("type") != "Results":
            return
        is_final = bool(message.get("is_final"))
        if is_final:
            if message.get("from_finalize") and self._finalize_marks:
                # Its turn is transcribed; audio after its Finalize is not.
                self._turn_finished_up_to(self._finalize_marks.popleft())
            else:
                # A forced cut (max utterance) or an unasked final: the turn so
                # far is transcribed; nothing left to replay.
                self._turn.clear()
                self._turn_bytes = 0
        alternatives = (message.get("channel") or {}).get("alternatives") or []
        transcript = alternatives[0].get("transcript", "") if alternatives else ""
        if not transcript:
            return  # e.g. a finalize with no speech: nothing to pass on
        if is_final:
            if message.get("from_finalize"):
                self.confirm_finalize()
            await self.push_frame(
                TranscriptionFrame(
                    transcript,
                    self._user_id,
                    time_now_iso8601(),
                    result=message,
                    # Phonon's final is the whole utterance, never a fragment.
                    finalized=True,
                )
            )
            await self.stop_processing_metrics()
        else:
            await self.push_frame(
                InterimTranscriptionFrame(
                    transcript, self._user_id, time_now_iso8601(), result=message
                )
            )


class _FailoverOnceFromPhonon(ServiceSwitcherStrategyFailover):
    """Pipecat's failover, one way: only Phonon giving up moves the call.

    Stock failover switches on any non-fatal error and cycles back, so a
    Sarvam hiccup would hand the call back to a dead Phonon.
    """

    async def handle_error(self, error: ErrorFrame) -> FrameProcessor | None:
        if self.active_service is not self.services[0]:
            return None
        if not isinstance(error.exception, PhononUnavailable):
            return None
        return await super().handle_error(error)


class PhononWithFallback(ServiceSwitcher):
    """Phonon first; when it gives up, the rest of the call goes to the fallback.

    A stock Pipecat ServiceSwitcher: both services sit in the pipeline, only
    the active one receives frames. The fallback should not hold a provider
    session it may never use (Sarvam's ``hold_connection_until_active``).
    """

    def __init__(self, primary: PhononSTTService, fallback: STTService):
        super().__init__(
            services=[primary, fallback], strategy_type=_FailoverOnceFromPhonon
        )

        @self.strategy.event_handler("on_service_switched")
        async def _on_switched(_strategy: Any, service: FrameProcessor) -> None:
            logger.warning(f"Phonon STT fell back to {service}")

    def set_self_interrupt(self, enabled: bool) -> None:
        """Forward a node-level interruption override to the services that
        take one (Sarvam), so it holds after a fallback too."""
        for service in self.services:
            setter = getattr(service, "set_self_interrupt", None)
            if setter is not None:
                setter(enabled)


def build_phonon_stt(
    config: PhononConfig, fallback: Optional[STTService] = None
) -> PhononSTTService | PhononWithFallback:
    """Create a Phonon STT service pointed at Phonon, behind the fallback
    when one is given."""
    logger.info(
        f"Using Phonon STT (at {config.url}, "
        f"fallback={type(fallback).__name__ if fallback else 'none'}) for Breeze Buddy"
    )
    service = PhononSTTService(
        url=config.url,
        api_key=config.api_key,
        language=config.language,
        call_id=config.call_id,
        fatal_on_failure=fallback is None,
    )
    if fallback is None:
        return service
    return PhononWithFallback(service, fallback)
