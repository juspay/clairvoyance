"""How an egress refusal is reported: to the log in full, to the model not at all.

Three things this pins down, all introduced with the egress guard:

- The guard ran twice per request — once before the retry loop and once as hop 0
  inside ssrf_safe_request — so every call resolved the host twice.
- Because the pre-loop call sat outside the inner `try`, its SSRFError fell into
  the generic `except Exception`: a routine policy block was logged as
  "HTTP request execution failed" with a stack trace, and the `except SSRFError`
  handler written for exactly this case never ran for the initial URL.
- The refusal message names the address the host resolved to, and that string is
  tool output the model reads. Returning it turns the control into an oracle:
  probe hostnames, read the refusals, map the internal network from the answers.
"""

from __future__ import annotations

import aiohttp
import pytest

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester as requester
import app.core.security.ssrf as ssrf_mod
from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    EGRESS_REFUSAL,
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpMethod,
    HttpRequestConfig,
)

INTERNAL = "10.1.2.3"


@pytest.fixture
def resolves_internal(monkeypatch):
    """Any hostname resolves to an internal address, and count the lookups."""
    calls: list[str] = []

    async def fake_resolve(hostname: str, port: int):
        calls.append(hostname)
        return [INTERNAL]

    monkeypatch.setattr(ssrf_mod, "_resolve_host", fake_resolve)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)
    return calls


@pytest.fixture
def logged(monkeypatch):
    """Capture what the executor logs. The app logger is loguru, which does not
    feed pytest's caplog, so the module's logger is swapped for a recorder —
    the pattern the rest of this suite uses."""
    lines: list[str] = []

    class _Recorder:
        def _record(self, msg, *a, **k):
            lines.append(str(msg))

        error = warning = info = debug = _record

    monkeypatch.setattr(requester, "logger", _Recorder())
    return lines


def _cfg() -> HttpRequestConfig:
    return HttpRequestConfig(
        url="https://merchant.example.com/api/order",
        method=HttpMethod.GET,
        timeout=5,
        max_retries=3,
    )


async def test_the_model_is_not_told_which_address_was_refused(resolves_internal):
    async with aiohttp.ClientSession() as session:
        result = await HttpRequestExecutor(session).execute(
            _cfg(), fire_and_forget=False
        )

    assert result is not None
    status, message = result
    assert status == 0
    assert message == EGRESS_REFUSAL
    # The things an oracle would need, none of which may appear.
    assert INTERNAL not in message
    assert "private" not in message.lower()
    assert "merchant.example.com" not in message


async def test_the_reason_is_still_logged_in_full(resolves_internal, logged):
    async with aiohttp.ClientSession() as session:
        await HttpRequestExecutor(session).execute(_cfg(), fire_and_forget=False)

    blob = "\n".join(logged)
    assert INTERNAL in blob, "an operator must still be able to diagnose this"


async def test_a_policy_block_is_not_reported_as_an_unexpected_error(
    resolves_internal, logged
):
    async with aiohttp.ClientSession() as session:
        await HttpRequestExecutor(session).execute(_cfg(), fire_and_forget=False)

    blob = "\n".join(logged)
    assert "blocked by egress guard, not retrying" in blob
    # The generic handler's wording. Its presence means the raise escaped the
    # dedicated branch again, and operators see a stack trace for a routine
    # refusal.
    assert "HTTP request execution failed" not in blob


async def test_the_host_is_resolved_once_per_request(resolves_internal):
    async with aiohttp.ClientSession() as session:
        await HttpRequestExecutor(session).execute(_cfg(), fire_and_forget=False)

    assert resolves_internal == ["merchant.example.com"], (
        f"expected exactly one DNS lookup, got {len(resolves_internal)}: "
        f"{resolves_internal}"
    )


async def test_a_refused_request_is_not_retried(resolves_internal):
    # max_retries=3, but a security refusal must abort on the first attempt —
    # retrying replays the request at a target that is still blocked.
    async with aiohttp.ClientSession() as session:
        await HttpRequestExecutor(session).execute(_cfg(), fire_and_forget=False)

    assert len(resolves_internal) == 1, "the refusal was retried"


async def test_fire_and_forget_still_returns_none(resolves_internal):
    async with aiohttp.ClientSession() as session:
        assert (
            await HttpRequestExecutor(session).execute(_cfg(), fire_and_forget=True)
            is None
        )
