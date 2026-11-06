import json
import os
import signal
import socket

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from tests.conftest import Server, free_port
from tests.test_end_to_end import send_audio, text


def test_killed_frontend_is_replaced_and_its_sockets_close(server):
    fe_pid = server.pid_of("fe-1")
    with connect(server.listen_url()) as ws:
        send_audio(ws, 0.2)
        os.kill(fe_pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            while True:
                ws.recv(timeout=5)
    server.wait_log(r"started fe-1 pid=", count=2)
    server.wait_log(r"conn 1 closed; 1 streams dropped")
    server.wait_listening()
    with connect(server.listen_url()) as ws:  # the replacement serves new calls
        send_audio(ws, 0.6)
        assert text(json.loads(ws.recv(timeout=5))).endswith("c0")


def test_killed_model_closes_sockets_with_1011_and_exits(server):
    with connect(server.listen_url()) as ws:
        send_audio(ws, 0.2)
        os.kill(server.pid_of("model"), signal.SIGKILL)
        with pytest.raises(ConnectionClosed) as closed:
            while True:
                ws.recv(timeout=5)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1011
    assert server.proc.wait(15) == 1


def test_frontend_crash_loop_exits_the_pod():
    port = free_port()
    blocker = socket.socket()  # holds the port without SO_REUSEPORT
    blocker.bind(("127.0.0.1", port))
    blocker.listen()
    try:
        server = Server(port, STT_RESTART_LIMIT="3")
        assert server.proc.wait(30) == 1
        assert any("died 3 times" in line for line in server.lines)
    finally:
        blocker.close()
