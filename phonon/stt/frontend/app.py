"""The WebSocket route (Deepgram-compatible, /v1/listen). One handler task per
socket; it never writes to the socket.

`WebSocket` is imported at module level on purpose: imported inside a function
under `from __future__ import annotations`, FastAPI can't resolve it and the
route answers HTTP 403 (load-test mistake #10).
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from loguru import logger
from stt.config import Settings
from stt.frontend.model_link import ModelLink
from stt.frontend.session import Session
from stt.protocol import deepgram


def create_app(settings: Settings, link: ModelLink) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        link.start(asyncio.get_running_loop())
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.websocket(deepgram.PATH)
    async def listen(ws: WebSocket) -> None:
        await ws.accept()
        session = link.open_session(ws)
        session.attach_handler(asyncio.current_task())
        if link.lost:
            # The model is gone and this front-end is about to exit: refuse
            # cleanly instead of accepting a call that can never be served.
            session.fail(1011, "server_error", "transcription is unavailable")
            await session.finish()
            return
        try:
            await _serve(ws, session, settings)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            # Swallow only the cancel the session itself sent (it closed the
            # socket), and only if no other cancel (server shutdown) is pending.
            task = asyncio.current_task()
            if not (session.consume_own_cancel() and task and task.uncancel() == 0):
                raise
        except Exception:
            logger.exception(f"sid {session.sid}: handler failed")
            session.fail(1011, "internal", "internal error")
        finally:
            await session.finish()

    return app


async def _serve(ws: WebSocket, session: Session, settings: Settings) -> None:
    # The connection itself is the "start": its settings are in the URL.
    try:
        config = deepgram.parse_query(dict(ws.query_params))
    except deepgram.ProtocolError as exc:
        session.fail(1008, "bad_config", str(exc))
        return
    session.start(config)

    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        session.touch()
        data = message.get("bytes")
        if data is not None:
            session.on_audio(data)
            if session.state in ("closing", "closed"):
                return  # e.g. too_fast: stop reading a client we have closed
            continue
        try:
            msg = deepgram.parse_client(message.get("text") or "")
        except deepgram.ProtocolError as exc:
            session.fail(1008, "bad_request", str(exc))
            return
        if isinstance(msg, deepgram.Finalize):
            session.on_finalize(msg.seq)
            if session.state in ("closing", "closed"):
                return  # e.g. too_many_finalize
        elif isinstance(msg, deepgram.CloseStream):
            await session.close_when_finals_sent()
            return
        # KeepAlive: touch() above is all it needs.
