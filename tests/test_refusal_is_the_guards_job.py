"""A refused request is worded and logged by the guard, not by its caller.

Every call site used to repeat the same three steps around an SSRFError — map
it to something safe to say, read off whether it was transient to pick a log
level, log it — and only the last part, shaping its own reply, was ever really
the caller's. Half of them got the level or the wording wrong at some point.
guarded_post_json and precheck_egress_url answer the refusal themselves now.
"""

from __future__ import annotations

import pytest

import app.core.network.aiohttp_request as aiohttp_mod
import app.core.network.egress as egress_mod
import app.core.network.httpx_request as httpx_mod
from app.core.network import (
    EgressResolutionError,
    SSRFError,
    guarded_post_json,
    guarded_stream,
    precheck_egress_url,
)

INTERNAL = "169.254.169.254"  # the cloud metadata address
PUBLIC = "93.184.216.34"


@pytest.fixture
def logged(monkeypatch):
    """The app logger is loguru, which does not feed caplog — swap and record."""
    lines: list[str] = []

    class _Recorder:
        def __init__(self) -> None:
            self.levels: list[str] = []

        def _at(self, level):
            def _record(msg, *a, **k):
                self.levels.append(level)
                lines.append(str(msg))

            return _record

        def __getattr__(self, name):
            return self._at(name)

    rec = _Recorder()
    # Every module that can log a refusal — the operation logs it, so the
    # recorder has to sit wherever the operation lives.
    monkeypatch.setattr(httpx_mod, "logger", rec)
    monkeypatch.setattr(aiohttp_mod, "logger", rec)
    monkeypatch.setattr(egress_mod, "logger", rec)
    return rec, lines


@pytest.fixture
def resolves(monkeypatch):
    def _to(addresses):
        async def _fake(hostname: str, port: int):
            return list(addresses)

        monkeypatch.setattr(egress_mod, "_resolve_host", _fake)

    monkeypatch.setattr(egress_mod, "_ALLOW_PRIVATE_EGRESS", False)
    return _to


@pytest.fixture
def blips(monkeypatch):
    def _install():
        async def _fake(hostname: str, port: int):
            raise OSError("temporary failure in name resolution")

        monkeypatch.setattr(egress_mod, "_resolve_host", _fake)
        monkeypatch.setattr(egress_mod, "_ALLOW_PRIVATE_EGRESS", False)

    return _install


async def test_a_refusal_comes_back_as_a_value_not_an_exception(resolves, logged):
    resolves([INTERNAL])

    response, refused = await guarded_post_json(
        "https://looks-fine.example/mcp", json={}, subject="[SUBJ] probe"
    )

    assert response is None
    assert refused == SSRFError.outward


async def test_the_resolved_address_never_reaches_the_caller(resolves, logged):
    """The reason is diagnosis in a log and an SSRF oracle anywhere else."""
    resolves([INTERNAL])

    _, refused = await guarded_post_json(
        "https://looks-fine.example/mcp", json={}, subject="[SUBJ] probe"
    )

    assert INTERNAL not in refused
    assert "looks-fine.example" not in refused


async def test_the_reason_is_logged_with_the_callers_subject(resolves, logged):
    rec, lines = logged
    resolves([INTERNAL])

    await guarded_post_json(
        "https://looks-fine.example/mcp", json={}, subject="[SUBJ] probe"
    )

    blob = "\n".join(lines)
    assert INTERNAL in blob, "an operator must still be able to diagnose this"
    assert "[SUBJ] probe" in blob, "the log line cannot say which caller it was"


async def test_a_policy_refusal_logs_at_error_a_blip_at_warning(
    resolves, blips, logged
):
    """The level IS the retryability. Getting it wrong pages someone over a
    DNS hiccup, or buries a blocked metadata fetch in the warning stream."""
    rec, _ = logged

    blips()
    await guarded_post_json("https://x.example/mcp", json={}, subject="s")
    # The last line is the guard's own; validate_egress_url logs its reason
    # first when it has one to give.
    assert rec.levels[-1] == "warning"

    rec.levels.clear()
    resolves([INTERNAL])
    await guarded_post_json("https://x.example/mcp", json={}, subject="s")
    assert rec.levels[-1] == "error"


async def test_a_blip_is_tolerated_only_when_something_checks_again(
    resolves, blips, logged
):
    """The tolerance is conditional, and that condition is the whole safety
    argument: a swallowed resolution failure is only harmless because a real
    gate runs later. Where none does, an attacker's own nameserver answers
    SERVFAIL here and an internal address at connect."""
    rec, lines = logged

    blips()
    await precheck_egress_url(
        "https://x.example/mcp", subject="[SUBJ] erp", rechecked_later=True
    )
    assert rec.levels == ["warning"]
    assert "[SUBJ] erp" in "\n".join(lines)

    with pytest.raises(EgressResolutionError):
        await precheck_egress_url(
            "https://x.example/mcp", subject="[SUBJ] erp", rechecked_later=False
        )


async def test_a_policy_refusal_raises_whatever_checks_later(resolves, logged):
    """A URL that may never be reached must not have credentials built for it,
    regardless of what a later check would have said."""
    resolves([INTERNAL])

    for rechecked in (True, False):
        with pytest.raises(SSRFError):
            await precheck_egress_url(
                "https://x.example/mcp", subject="[SUBJ] erp", rechecked_later=rechecked
            )


async def test_a_permitted_post_returns_the_response_and_an_empty_reason(
    resolves, monkeypatch
):
    resolves([PUBLIC])

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            return "the-response"

    monkeypatch.setattr(httpx_mod.httpx, "AsyncClient", _Client)

    response, refused = await guarded_post_json("https://x.example/mcp", json={})

    assert response == "the-response"
    assert refused == ""


async def test_a_stream_refusal_is_a_value_not_an_exception(resolves, logged):
    """guarded_stream keeps the same contract as its siblings.

    It is the one operation that cannot hand back a finished result — the
    caller has to hold the response open to read SSE and to stop a body
    mid-transfer. That is a reason to yield a live response; it is not a
    reason to make the caller learn the egress error types. A refusal arrives
    as a StreamAttempt with no response.
    """
    resolves([INTERNAL])

    async with guarded_stream(
        "GET", "https://looks-fine.example/x", timeout_seconds=5, subject="[S] probe"
    ) as attempted:
        assert attempted.response is None
        assert attempted.refusal == SSRFError.outward
        assert attempted.may_retry is False


async def test_a_transient_stream_refusal_says_so(blips, logged):
    """The caller retries on may_retry, without knowing why it may."""
    blips()

    async with guarded_stream(
        "GET", "https://x.example/y", timeout_seconds=5, subject="[S] probe"
    ) as attempted:
        assert attempted.response is None
        assert attempted.may_retry is True
        assert attempted.refusal == EgressResolutionError.outward


async def test_the_stream_refusal_is_logged_with_its_level(resolves, blips, logged):
    rec, lines = logged

    blips()
    async with guarded_stream(
        "GET", "https://x.example/y", timeout_seconds=5, subject="[S] probe"
    ):
        pass
    assert rec.levels[-1] == "warning"

    rec.levels.clear()
    resolves([INTERNAL])
    async with guarded_stream(
        "GET", "https://x.example/y", timeout_seconds=5, subject="[S] probe"
    ):
        pass
    assert rec.levels[-1] == "error"
    assert "[S] probe" in "\n".join(lines)
    assert INTERNAL in "\n".join(lines)
