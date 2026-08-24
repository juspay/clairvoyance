"""The CRM_* consent windows: what a bad flag value may not do.

These are compliance windows, live-tunable through DevCycle, and a wrong one
is silent — the deadline is stored concretely on the row, so fixing the flag
does not repair rows already written.
"""

from typing import Any, Awaitable, Callable, Dict

import pytest

from app.core.config import dynamic
from app.crm.permission.consent import load_policy

GetConfig = Callable[..., Awaitable[Any]]


def _flags(values: Dict[str, Any]) -> GetConfig:
    """Signature mirrors the real get_config; a drift here would hide one
    there."""

    async def get_config(key: str, default_value: Any, return_type: type = str) -> Any:
        return values.get(key, default_value)

    return get_config


async def test_the_defaults_are_the_documented_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_config swallows a flag-store outage and falls through to these, so a
    Redis failure never blocks recording a STOP."""
    monkeypatch.setattr(dynamic, "get_config", _flags({}))
    policy = await load_policy()
    assert (policy.marketing_grant_days, policy.reask_embargo_days) == (7, 90)
    assert policy.pending_confirm_hours == 24


async def test_a_sane_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dynamic, "get_config", _flags({"CRM_REASK_EMBARGO_DAYS": 30}))
    assert (await load_policy()).reask_embargo_days == 30


@pytest.mark.parametrize("bad", [0, -1, "", "soon", None, True, False])
async def test_a_window_that_would_invert_the_rule_is_refused(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """Zero or negative is not a shorter rule, it is the absence of one: a -1
    day embargo lifted yesterday.

    `True` is the regression case. convert_type(True, int) returns 1, so
    picking DevCycle's default Boolean variable type instead of Number would
    turn the 90-day embargo into 24 hours — and take the success path, so not
    even a warning would be logged.
    """
    monkeypatch.setattr(dynamic, "get_config", _flags({"CRM_REASK_EMBARGO_DAYS": bad}))
    assert await dynamic.CRM_REASK_EMBARGO_DAYS() == 90
