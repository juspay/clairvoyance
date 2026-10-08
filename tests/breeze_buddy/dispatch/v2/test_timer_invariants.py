"""v2 settings are checked at load: an acceptor BLPOP that could outlast the dialler
client's socket timeout fails, and so does any size or interval out of range.
"""

import os
import subprocess
import sys
from typing import Dict

import pytest


def _load_static(**env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "import app.core.config.static"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_defaults_load():
    assert _load_static().returncode == 0


@pytest.mark.parametrize(
    "env",
    [
        {"BB_V2_TICKET_BLPOP_TIMEOUT_S": "10"},  # as long as the socket timeout
        {"BB_V2_TICKET_BLPOP_TIMEOUT_S": "1", "BB_V2_REDIS_SOCKET_TIMEOUT_S": "0.5"},
        {"BB_V2_TICKET_BLPOP_TIMEOUT_S": "0"},  # BLPOP 0 blocks forever
    ],
)
def test_a_blpop_outlasting_the_socket_timeout_fails_at_load(env):
    # The client gives up while the server may still pop: a lost ticket
    loaded = _load_static(**env)
    assert loaded.returncode != 0
    assert "BB_V2_TICKET_BLPOP_TIMEOUT_S" in loaded.stderr


@pytest.mark.parametrize(
    "env",
    [
        {"BB_V2_DUE_BATCH": "0"},
        {"BB_V2_DUE_FULL_PASS_TICKS": "0"},
        {"BB_V2_DUE_RECHECK_S": "0.5"},  # every tick, as before bb:due
        {"BB_V2_DUE_RECHECK_S": "nan"},
        {"BB_V2_LEDGER_CHUNK": "0"},
        {"BB_V2_PRUNE_CHUNK": "0"},
        {"BB_V2_MONITOR_SCAN_CHUNK": "0"},
        {"BB_V2_MONITOR_SCAN_MAX": "0"},
        {"BB_V2_ROUTES_REFRESH_S": "5"},
        {"BB_V2_DIAL_MEMO_TTL_S": "-1"},
        {"BB_V2_DIAL_MEMO_TTL_S": "120"},  # an edit would wait 2 min to reach a dial
        {"BB_V2_ACCEPT_BATCH": "0"},
        {"BB_V2_MAX_INFLIGHT_PER_POD": "0"},  # the acceptor would never pop
        {"BB_V2_ACCEPT_ERROR_BACKOFF_S": "0"},  # a Redis outage would spin the loop
        {"BB_V2_ACCEPT_ERROR_BACKOFF_S": "nan"},
        {"BB_V2_ACCEPT_ERROR_BACKOFF_S": "61"},  # a minute without popping per error
        {"BB_V2_ACCEPT_DISABLED_SLEEP_S": "0"},  # pop, push back, pop: a busy loop
        {"BB_V2_ACCEPT_DISABLED_SLEEP_S": "61"},
        {"BB_V2_ACCEPT_FULL_SLEEP_S": "0"},
        {"BB_V2_ACCEPT_FULL_SLEEP_S": "61"},
    ],
)
def test_a_size_or_interval_out_of_range_fails_at_load(env: Dict[str, str]) -> None:
    loaded = _load_static(**env)
    assert loaded.returncode != 0
    assert next(iter(env)) in loaded.stderr
