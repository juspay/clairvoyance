"""Front-end process entry: shared port, model connection, uvicorn."""

import os
import socket

import uvicorn
from loguru import logger
from stt import ipc, log, proc
from stt.config import Settings
from stt.frontend.app import create_app
from stt.frontend.model_link import ModelLink


def listen_socket(host: str, port: int) -> socket.socket:
    """Every front-end binds the same port; the Linux kernel spreads connections.

    macOS accepts SO_REUSEPORT but does not spread TCP connections the same way,
    so local runs use one front-end.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((host, port))
    sock.listen(1024)
    return sock


def run(
    settings: Settings, endpoint: ipc.Endpoint, index: int, parent_pid: int
) -> None:
    name = f"fe-{index}"
    log.setup(name, settings.log_level, settings.log_json)
    proc.install_crash_handlers(name)
    proc.watch_parent(parent_pid)

    sock = listen_socket(settings.host, settings.port)
    link = ModelLink(ipc.connect(endpoint), settings, label=f"fe{index}-{os.getpid()}")
    config = uvicorn.Config(
        create_app(settings, link),
        loop="uvloop",
        ws="websockets-sansio",
        ws_max_size=settings.max_message_bytes,
        lifespan="on",
        log_config=None,
        access_log=False,
    )
    logger.info(f"serving ws://{settings.host}:{settings.port}/v1/audio/stream")
    uvicorn.Server(config).run(sockets=[sock])
