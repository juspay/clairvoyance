"""DAILY_BOT_ZYGOTE_AT_BOOT decides whether run.py forks the Daily bot zygote.

Only the API server launches Daily bots, so the zygote is opt-in: off unless
DAILY_BOT_ZYGOTE_AT_BOOT is "true". Elsewhere it would cost ~240Mi per pod.
"""

import importlib

import pytest

import app.core.config.static as static


@pytest.mark.parametrize(
    "value, expected",
    [(None, False), ("true", True), ("TRUE", True), ("false", False), ("False", False)],
)
def test_daily_bot_zygote_at_boot_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("DAILY_BOT_ZYGOTE_AT_BOOT", raising=False)
    else:
        monkeypatch.setenv("DAILY_BOT_ZYGOTE_AT_BOOT", value)
    try:
        assert importlib.reload(static).DAILY_BOT_ZYGOTE_AT_BOOT is expected
    finally:
        monkeypatch.undo()
        importlib.reload(static)
