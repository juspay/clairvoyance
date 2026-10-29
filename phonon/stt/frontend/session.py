"""One WebSocket's state. Runs on the event loop only.

    new → starting → ready → closing → closed

It speaks Phonon's Deepgram-style protocol (stt.protocol.deepgram): results go
out as `Results` messages, errors as a close code with a reason.

Its sender task is the only writer to the socket, so a slow client delays
nothing but itself, and two writers can never interleave frames.
"""

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any, Coroutine

import orjson
from fastapi import WebSocket
from loguru import logger
from stt import ipc
from stt.protocol import deepgram

if TYPE_CHECKING:
    from stt.config import Settings
    from stt.frontend.model_link import ModelLink


class _CloseFrame:
    def __init__(self, code: int, reason: str = "") -> None:
        self.code = code
        self.reason = reason


class Session:
    SENDER_DRAIN_S = 2.0

    def __init__(
        self, ws: WebSocket, sid: int, link: "ModelLink", settings: "Settings"
    ) -> None:
        self.ws = ws
        self.sid = sid
        self.state = "new"
        self._link = link
        self._settings = settings
        # Unbounded queue with the limit enforced in _push for normal messages,
        # so an error frame and the close frame always fit behind a full backlog.
        self._outbound: asyncio.Queue[Any] = asyncio.Queue()
        self._outbound_max = settings.socket_outbound_max
        # Rate limit: audio received vs. time since the stream started.
        self._audio_bytes = 0
        self._bytes_per_s = 0
        self._started_at = 0.0
        self._open_sent = False
        self._close_sent = False
        self._close_queued = False
        self._close_code = 1000
        self._close_reason = ""
        # Where the current utterance starts on the stream clock (for Results).
        self._utt_start_s = 0.0
        self._last_seen = time.monotonic()
        self._idle_timer: asyncio.TimerHandle | None = None
        # Finalizes sent whose final hasn't come back yet. A client `close` waits
        # for them, and too many at once is refused (a finalize flood).
        self._finals_owed = 0
        self._no_finals_owed = asyncio.Event()
        self._no_finals_owed.set()
        self._sender = asyncio.create_task(self._send_loop())
        self._handler: asyncio.Task | None = None
        self._own_cancel_pending = False  # set when WE cancel the handler
        self._ready_timer: asyncio.TimerHandle | None = None
        self._log = logger
        # The loop holds tasks weakly; keep our own references until they finish.
        self._tasks: set[asyncio.Task] = set()

    def consume_own_cancel(self) -> bool:
        """True (once) if the handler's cancellation came from this session."""
        own, self._own_cancel_pending = self._own_cancel_pending, False
        return own

    def attach_handler(self, task: asyncio.Task | None) -> None:
        """The socket's handler task, cancelled if the server closes the socket
        while the handler waits on the client (so it never waits forever)."""
        self._handler = task

    # ---- from the socket handler -------------------------------------------------

    @property
    def stream_id(self) -> str:
        """Unique within the pod and across restarts; sent as request_id."""
        return f"{self._link.label}-{self.sid}"

    def start(self, config: deepgram.StreamConfig) -> None:
        self.state = "starting"
        self._bytes_per_s = config.sample_rate * 2
        self._started_at = time.monotonic()
        self._log = logger.bind(call_id=config.call_id)
        self._schedule_idle_check()
        payload = {"sample_rate": config.sample_rate}
        self._link.enqueue((ipc.OPEN, self.sid, payload))
        self._open_sent = True
        self._ready_timer = asyncio.get_running_loop().call_later(
            self._settings.model_open_timeout_s, self._check_ready
        )

    def touch(self) -> None:
        """Any client message (audio, Finalize, KeepAlive) proves it is alive."""
        self._last_seen = time.monotonic()

    def on_audio(self, data: bytes) -> None:
        # A client may send up to ~1.5x real time plus a burst of max_pending_s;
        # beyond that it is closed alone. Real-time clients never trip this,
        # even when the model stalls (that is caught as a lost model, 1011).
        self._audio_bytes += len(data)
        elapsed = time.monotonic() - self._started_at
        allowed = self._bytes_per_s * (
            self._settings.max_audio_rate * elapsed + self._settings.max_pending_s
        )
        if self._audio_bytes > allowed:
            self.fail(1008, "too_fast", "audio sent much faster than real time")
            return
        self._forward((ipc.AUDIO, self.sid, data))

    def on_finalize(self, seq: int | None) -> None:
        if self._finals_owed >= self._settings.max_pending_finals:
            self.fail(1008, "too_many_finalize", "finalize sent faster than finals")
            return
        self._finals_owed += 1
        self._no_finals_owed.clear()
        # The receive time travels with it, so server_ms covers the front-end and
        # IPC too (time.monotonic is one clock for every process on the machine).
        self._forward((ipc.FINALIZE, self.sid, {"seq": seq, "t": time.monotonic()}))

    async def close_when_finals_sent(self) -> None:
        """The client said `close`: deliver the finals it already asked for,
        then close. Closing at once would drop the call's last utterance."""
        if self.state not in ("starting", "ready"):
            self.close(1000)
            return
        # Also while starting: the finalize is already on its way to the model,
        # and its final comes once the model opens the stream.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._no_finals_owed.wait(), self._settings.close_drain_s
            )
        if self._finals_owed:
            # Don't let a lost last utterance look like a clean close.
            self.fail(1011, "finals_lost", "finals still owed when the stream closed")
        else:
            # Deepgram ends a stream with a Metadata summary, then closes.
            duration_s = (
                self._audio_bytes / self._bytes_per_s if self._bytes_per_s else 0
            )
            self._push(deepgram.metadata(self.stream_id, duration_s), reserved=True)
            self.close(1000)

    def fail(self, close_code: int, code: str, message: str) -> None:
        if self.state in ("closing", "closed"):
            return
        self._log.info(f"sid {self.sid}: closing {close_code} ({code})")
        # Deepgram reports errors as a close code with a reason, not a message.
        self._shut(
            close_code, graceful=True, reason=deepgram.close_reason(code, message)
        )

    def close(self, code: int = 1000) -> None:
        self._shut(code, graceful=True)

    async def finish(self) -> None:
        """Called once when the handler exits, whatever the reason."""
        self._cancel_ready_timer()  # e.g. the client hung up while starting
        self._cancel_idle_timer()
        self._notify_model_closed()
        self.state = "closed"
        if self._close_queued:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._sender), self.SENDER_DRAIN_S
                )
            except asyncio.TimeoutError:
                # The client is too slow to drain: skip what's queued but still
                # close with the code we meant, not uvicorn's default.
                self._sender.cancel()
                await self._close_now(self._close_code, self._close_reason)
            except asyncio.CancelledError:
                self._sender.cancel()
                raise  # a real cancellation (server shutdown) must propagate
        else:
            self._sender.cancel()

    # ---- from the model, via ModelLink (event loop) -------------------------------

    def on_result(self, kind: str, payload: Any) -> None:
        if kind == ipc.OPENED:
            self._on_opened()
        elif kind == ipc.PARTIAL:
            self._push(
                deepgram.results(
                    self.stream_id,
                    payload["text"],
                    self._utt_start_s,
                    payload["audio_end_s"],
                    is_final=False,
                    speech_final=False,
                    from_finalize=False,
                    utt=payload["utt"],
                )
            )
        elif kind == ipc.FINAL:
            forced = payload["forced"]
            self._push(
                deepgram.results(
                    self.stream_id,
                    payload["text"],
                    self._utt_start_s,
                    payload["speech_end_s"],
                    is_final=True,
                    # A forced cut (max utterance length) is not end of speech.
                    speech_final=not forced,
                    from_finalize=not forced,
                    utt=payload["utt"],
                    seq=payload["seq"],
                    server_ms=payload["server_ms"],
                )
            )
            self._utt_start_s = payload["speech_end_s"]
            if not forced:  # forced finals answer no finalize
                self._finals_owed = max(0, self._finals_owed - 1)
                if self._finals_owed == 0:
                    self._no_finals_owed.set()
        elif kind == ipc.WARNING:
            # Deepgram has no warning message; the client can't act on it anyway.
            self._log.warning(
                f"sid {self.sid}: {payload['code']}: {payload['message']}"
            )
        elif kind == ipc.ERROR:
            self.fail(1011, payload["code"], payload["message"])

    # ---- internals ---------------------------------------------------------------

    def _shut(self, code: int, graceful: bool, reason: str = "") -> None:
        """The one way a session closes. Graceful sends what is queued, then the
        close frame; otherwise (client not reading) the socket is closed now."""
        if self.state in ("closing", "closed"):
            return
        self.state = "closing"
        self._close_code = code
        self._close_reason = reason
        self._cancel_ready_timer()
        self._cancel_idle_timer()
        self._notify_model_closed()
        if graceful:
            self._outbound.put_nowait(_CloseFrame(code, reason))  # always fits
            self._close_queued = True
        if not graceful:
            self._sender.cancel()
            self._spawn(self._close_now(code, reason))
        if self._handler is not None and self._handler is not asyncio.current_task():
            self._own_cancel_pending = True
            self._handler.cancel()  # stop waiting on a client we have closed

    def _on_opened(self) -> None:
        if self.state != "starting":
            return
        self._cancel_ready_timer()
        self.state = "ready"  # Deepgram has no ready message: nothing to send

    def _forward(self, event: ipc.Event) -> None:
        # Sent at once, even before `ready`: the model applies events in order,
        # so OPEN is filed first. Holding audio here and releasing it in one burst
        # at `ready` would let the model's pending cap drop the call's first words.
        if self.state in ("starting", "ready"):
            self._link.enqueue(event)

    def _cancel_ready_timer(self) -> None:
        if self._ready_timer is not None:
            self._ready_timer.cancel()
            self._ready_timer = None

    def _schedule_idle_check(self) -> None:
        # One check every half timeout per socket, not a timer per message.
        self._idle_timer = asyncio.get_running_loop().call_later(
            self._settings.idle_timeout_s / 2, self._check_idle
        )

    def _check_idle(self) -> None:
        self._idle_timer = None
        if self.state not in ("starting", "ready"):
            return
        if time.monotonic() - self._last_seen > self._settings.idle_timeout_s:
            # Deepgram closes silent streams too; a client's KeepAlive (e.g.
            # every 5 s) keeps a live call open even while nobody speaks.
            self.fail(1011, "idle", "no audio or KeepAlive received")
            return
        self._schedule_idle_check()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _check_ready(self) -> None:
        if self.state == "starting":
            self.fail(
                1011, "model_unavailable", "the model did not open the stream in time"
            )

    def _notify_model_closed(self) -> None:
        if self._open_sent and not self._close_sent:
            self._link.enqueue((ipc.CLOSE, self.sid, None))
            self._close_sent = True
        self._link.forget(self.sid)

    def _push(self, msg: dict, reserved: bool = False) -> None:
        """Queue a message for the client. Normal messages respect the limit;
        `reserved` ones (the closing Metadata) always go through."""
        if self.state == "closed":
            return
        if not reserved and self._outbound.qsize() >= self._outbound_max:
            # The client stopped reading; don't let its backlog grow.
            self._log.warning(f"sid {self.sid}: client not reading; closing")
            self._shut(1008, graceful=False)
            return
        self._outbound.put_nowait(msg)

    async def _send_loop(self) -> None:
        try:
            while True:
                msg = await self._outbound.get()
                if isinstance(msg, _CloseFrame):
                    await self.ws.close(msg.code, msg.reason or None)
                    return
                await self.ws.send_text(orjson.dumps(msg).decode())
        except Exception:
            return  # the socket is gone; the handler's finish() cleans up

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _close_now(self, code: int, reason: str = "") -> None:
        with contextlib.suppress(Exception):
            await self.ws.close(code, reason or None)
