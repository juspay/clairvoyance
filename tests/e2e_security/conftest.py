"""Hermetic launch harness for the security E2E suites.

Launches the REAL application with Uvicorn on loopback and drives it over real
HTTP. Everything is sealed: an explicit allowlisted environment (no ``.env`` is
read), no Postgres, no Redis, no background workers, no provider hosts, and only
synthetic credentials invented here. Nothing in this package may contact a
non-loopback address.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Synthetic, non-secret values. Never reuse a real provider credential here.
TWILIO_TOKEN = "twilio-e2e-synthetic-token"
PLIVO_TOKEN = "plivo-e2e-synthetic-token"
EXOTEL_TOKEN = "exotel-e2e-synthetic-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def sealed_env(port: int, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Explicit allowlist. Inheriting the developer's environment is forbidden:
    it can carry a real ``DATABASE_URL`` or provider credential into the run."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        "JWT_SECRET_KEY": "e2e-local-not-a-real-secret",
        "JWT_ALGORITHM": "HS256",
        "SKIP_KMS_DECRYPT": "true",
        "APP_BASE_URL": f"http://127.0.0.1:{port}",
        "TELEPHONY_WEBHOOK_PATH_PREFIX": "",
        "ENFORCE_TELEPHONY_WEBHOOK_SIGNATURES": "true",
        "TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
        "PLIVO_AUTH_TOKEN": PLIVO_TOKEN,
        "EXOTEL_WEBHOOK_AUTH_TOKEN": EXOTEL_TOKEN,
        "SSRF_ALLOW_PRIVATE_EGRESS": "false",
        # every stateful / background subsystem off
        "POSTGRES_HOST": "",
        "POSTGRES_PORT": "",
        "POSTGRES_DB": "",
        "POSTGRES_USER": "",
        "POSTGRES_PASSWORD": "",
        "REDIS_HOST": "",
        "REDIS_PORT": "",
        "REDIS_CLUSTER_NODES": "",
        "ENABLE_REDIS_DYNAMIC_CONFIG": "false",
        "ENABLE_BACKGROUND_TASKS": "false",
        "ENABLE_DISPATCHER": "false",
        "ENABLE_VOICE_AGENT_POOL": "false",
        "ENABLE_DAILY_ROOM_POOL": "false",
        "ENABLE_DRAGONTTS_KILL_SWITCH": "false",
        "ENABLE_TRACING": "false",
        "OTEL_SDK_DISABLED": "true",
        "UVICORN_RELOAD": "false",
    }
    if extra:
        env.update(extra)
    return env


class LaunchedApp:
    """A running Uvicorn process serving the real ASGI app on loopback."""

    def __init__(self, base: str, log_path: Path):
        self.base = base
        self._log_path = log_path

    @property
    def log(self) -> str:
        return self._log_path.read_text()


@pytest.fixture(scope="session")
def launched_app(tmp_path_factory) -> Iterator[LaunchedApp]:
    port = free_port()
    log_path = tmp_path_factory.mktemp("e2e") / "api-server.log"
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        cwd=str(REPO_ROOT),
        env=sealed_env(port),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        # Hand the child only the pipes above — not whatever descriptors this
        # pytest process happens to hold open.
        close_fds=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 120
        # One client for the whole probe loop; a per-iteration client leaks a
        # connection pool on every retry.
        with httpx.Client(timeout=2.0) as probe:
            while True:
                if proc.poll() is not None:
                    log_file.flush()
                    pytest.fail(
                        f"app exited rc={proc.returncode}\n{log_path.read_text()[-3000:]}"
                    )
                if time.time() > deadline:
                    pytest.fail(
                        f"app never became healthy\n{log_path.read_text()[-3000:]}"
                    )
                try:
                    if probe.get(f"{base}/health").status_code == 200:
                        break
                except Exception:
                    time.sleep(0.4)
        yield LaunchedApp(base, log_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


@pytest.fixture(scope="session")
def client(launched_app: LaunchedApp) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=launched_app.base, timeout=20.0) as c:
        yield c
