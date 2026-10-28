"""Runs the real server (supervisor + model + front-ends) as a subprocess."""

import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, port: int, **env: str) -> None:
        self.port = port
        self.base_url = f"ws://127.0.0.1:{port}"
        self.lines: list[str] = []
        full_env = os.environ | {
            "STT_PORT": str(port),
            "STT_FRONTENDS": "1",
            "STT_LOG_JSON": "false",
            "STT_LOG_LEVEL": "DEBUG",
            **env,
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "stt"],
            # Run outside the project so a developer's phonon/.env can't change
            # the test server's settings; PYTHONPATH still finds the package.
            cwd=tempfile.gettempdir(),
            env=full_env | {"PYTHONPATH": str(PROJECT_ROOT)},
            stderr=subprocess.PIPE,
            text=True,
        )
        threading.Thread(target=self._read_logs, daemon=True).start()

    def listen_url(self, sample_rate: int | str = 8000, **params: str) -> str:
        """A Deepgram-style connect URL, as Phonon's clients build it."""
        query = {"encoding": "linear16", "sample_rate": sample_rate, "channels": 1}
        query.update(params)
        return f"{self.base_url}/v1/listen?{urlencode(query)}"

    def _read_logs(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.lines.append(line.rstrip())

    def wait_listening(self, timeout_s: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited:\n" + "\n".join(self.lines))
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.1)
        raise TimeoutError("server never listened:\n" + "\n".join(self.lines))

    def pid_of(self, name: str, occurrence: int = -1) -> int:
        """PIDs come from the supervisor's own 'started <name> pid=N' log lines."""
        pids = [
            int(m.group(1))
            for line in self.lines
            if (m := re.search(rf"started {name} pid=(\d+)", line))
        ]
        return pids[occurrence]

    def wait_log(self, pattern: str, timeout_s: float = 15.0, count: int = 1) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if sum(1 for line in self.lines if re.search(pattern, line)) >= count:
                return
            time.sleep(0.05)
        raise TimeoutError(f"no log matching {pattern!r}:\n" + "\n".join(self.lines))

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def server_factory():
    servers: list[Server] = []

    def make(**env: str) -> Server:
        server = Server(free_port(), **env)
        servers.append(server)
        server.wait_listening()
        return server

    yield make
    for server in servers:
        server.stop()


@pytest.fixture
def server(server_factory) -> Server:
    return server_factory()
