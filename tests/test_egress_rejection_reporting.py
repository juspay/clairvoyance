"""How an egress refusal is reported: to the log in full, to the model not at all.

Pins down: the guard used to run twice per request (double DNS resolution);
the pre-loop check sat outside the inner `try`, so its SSRFError fell into
the generic `except Exception`; and the refusal message never names the
resolved address, so it cannot be used to probe hosts.
"""

from __future__ import annotations

import socket

import pytest

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester as requester
import app.core.network.aiohttp_request as net_mod
import app.core.network.egress as ssrf_mod
from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpMethod,
    HttpRequestConfig,
)
from app.core.network import SSRFError

INTERNAL = "10.1.2.3"

# Sourced from the guard, not restated here: the point of the assertions below
# is that the executor hands out what the refusal itself carries, not its own copy.
EGRESS_REFUSAL = SSRFError.outward


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


class _Log:
    """What was logged, and at what level."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.levels: list[str] = []

    def _at(self, level):
        def _record(msg, *a, **k):
            self.levels.append(level)
            self.lines.append(str(msg))

        return _record

    def __getattr__(self, name):
        return self._at(name)

    @property
    def blob(self) -> str:
        return "\n".join(self.lines)

    def levels_for(self, needle: str) -> list[str]:
        return [lv for lv, ln in zip(self.levels, self.lines) if needle in ln]


@pytest.fixture
def logged(monkeypatch):
    """Capture the log across the executor AND the guard.

    The app logger is loguru, which does not feed pytest's caplog, so module
    loggers are swapped for a recorder — the pattern the rest of this suite
    uses. Both modules are recorded because a refusal is logged by the guard
    now, not by the caller; an operator still has to be able to find it.
    """
    log = _Log()
    monkeypatch.setattr(requester, "logger", log)
    monkeypatch.setattr(net_mod, "logger", log)
    return log


def _cfg() -> HttpRequestConfig:
    return HttpRequestConfig(
        url="https://merchant.example.com/api/order",
        method=HttpMethod.GET,
        timeout=5,
        max_retries=3,
    )


async def test_the_model_is_not_told_which_address_was_refused(resolves_internal):
    result = await HttpRequestExecutor().execute(_cfg(), fire_and_forget=False)

    assert result is not None
    status, message = result
    assert status == 0
    assert message == EGRESS_REFUSAL
    # The things an oracle would need, none of which may appear.
    assert INTERNAL not in message
    assert "private" not in message.lower()
    assert "merchant.example.com" not in message


async def test_the_reason_is_still_logged_in_full(resolves_internal, logged):
    await HttpRequestExecutor().execute(_cfg(), fire_and_forget=False)

    assert INTERNAL in logged.blob, "an operator must still be able to diagnose this"


async def test_a_policy_block_is_not_reported_as_an_unexpected_error(
    resolves_internal, logged
):
    await HttpRequestExecutor().execute(_cfg(), fire_and_forget=False)

    assert "HTTP GET egress refused" in logged.blob
    # A policy refusal is an error; the level IS the retryability.
    assert logged.levels_for("egress refused") == ["error"]
    # Generic-handler wording; its presence means the raise escaped the branch again.
    assert "HTTP request execution failed" not in logged.blob


async def test_the_host_is_resolved_once_per_request(resolves_internal):
    await HttpRequestExecutor().execute(_cfg(), fire_and_forget=False)

    assert resolves_internal == ["merchant.example.com"], (
        f"expected exactly one DNS lookup, got {len(resolves_internal)}: "
        f"{resolves_internal}"
    )


async def test_a_refused_request_is_not_retried(resolves_internal):
    # max_retries=3, but a security refusal must abort on the first attempt.
    await HttpRequestExecutor().execute(_cfg(), fire_and_forget=False)

    assert len(resolves_internal) == 1, "the refusal was retried"


async def test_fire_and_forget_still_returns_none(resolves_internal):
    assert await HttpRequestExecutor().execute(_cfg(), fire_and_forget=True) is None


async def test_a_transient_dns_failure_is_retried_not_reported_as_a_refusal(
    monkeypatch, logged
):
    """A resolver hiccup is not a policy decision.

    Collapsing EgressResolutionError into SSRFError would abort on the first
    attempt instead of retrying like any other transport blip.
    """
    import socket

    attempts: list[str] = []

    async def flaky(hostname: str, port: int):
        attempts.append(hostname)
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(ssrf_mod, "_resolve_host", flaky)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)

    result = await HttpRequestExecutor().execute(_cfg(), fire_and_forget=False)

    assert len(attempts) == 3, f"expected all 3 attempts, got {len(attempts)}"
    # Same line as a refusal, logged at warning instead of error — the level is
    # what separates "try again" from "this will never work".
    assert logged.levels_for("egress refused") == ["warning"] * 3
    assert result is not None and result[1] != EGRESS_REFUSAL


async def test_the_model_hears_why_when_every_attempt_was_refused(monkeypatch, logged):
    """An exhausted retry used to report an empty string.

    The executor knows what happened — the refusal carries text written to be
    safe to hand out — and then dropped it on the one path where the model has
    nothing else to go on. "" is indistinguishable from a 500 or a timeout.
    """
    import socket

    from app.core.network import EgressResolutionError

    async def flaky(hostname: str, port: int):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(ssrf_mod, "_resolve_host", flaky)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)

    result = await HttpRequestExecutor().execute(_cfg(), fire_and_forget=False)

    assert result is not None
    assert result == (0, EgressResolutionError.outward)
    # and still not the resolved address or the hostname
    assert "merchant.example.com" not in result[1]


class _Answers:
    """A session whose every request answers with one fixed status."""

    timeout = None

    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, *a, **kw):
        outer = self

        class _Resp:
            status = outer.status
            headers = {"Content-Type": "application/json"}

            class content:
                @staticmethod
                async def read(n):
                    return b""

            def release(self):
                pass

        return _Resp()


async def test_a_blip_then_a_real_answer_is_not_still_reported_as_unreachable(
    monkeypatch, logged
):
    """The refusal text must not outlive the attempt it came from.

    Attempt 1 cannot resolve; attempts 2 and 3 reach the server and get 500s.
    Reporting "could not reach the server" then would tell the model — and the
    merchant, via failureReason — to retry something that answered twice.
    """
    import app.core.network.aiohttp_request as net

    attempts = {"n": 0}

    async def blip_then_public(hostname: str, port: int):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_mod, "_resolve_host", blip_then_public)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)
    monkeypatch.setattr(net, "create_aiohttp_session", lambda **kw: _Answers(500))

    result = await HttpRequestExecutor().execute(_cfg(), fire_and_forget=False)

    assert result == (0, "")


async def test_an_ordinary_failure_still_reports_nothing_in_particular(
    monkeypatch, logged
):
    """The refusal text is carried only when there WAS a refusal — a run of
    500s must not be dressed up as an egress problem."""
    import app.core.network.aiohttp_request as net

    async def _public(hostname: str, port: int):
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_mod, "_resolve_host", _public)

    class _Resp:
        status = 500
        headers = {"Content-Type": "application/json"}

        class content:
            @staticmethod
            async def read(n):
                return b""

        def release(self):
            pass

    class _Session:
        timeout = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(net, "create_aiohttp_session", lambda **kw: _Session())

    result = await HttpRequestExecutor().execute(_cfg(), fire_and_forget=False)

    assert result == (0, "")


async def test_an_mcp_tool_call_connects_to_the_validated_address(monkeypatch):
    """The direct MCP handler posts with httpx, which resolves the name again.

    Between validate_egress_url and that post, a rebinding host can change its
    answer — and this path carries decrypted tenant credentials. So the request
    must go to an address that was checked, with the name kept only for routing
    and TLS.
    """
    import app.ai.voice.agents.breeze_buddy.mcp as mcp_mod
    import app.core.network.httpx_request as httpx_mod

    sent: dict = {}

    class _Spy:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None, **kwargs):
            sent["url"] = url
            sent["headers"] = headers or {}
            sent["extensions"] = kwargs.get("extensions") or {}

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "jsonrpc": "2.0",
                        "id": "1",
                        "result": {"content": [{"type": "text", "text": "{}"}]},
                    }

            return _R()

    async def resolves_to(hostname: str, port: int):
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_mod, "_resolve_host", resolves_to)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)
    monkeypatch.setattr(httpx_mod.httpx, "AsyncClient", _Spy)

    from mcp.client.session_group import StreamableHttpParameters

    handler = mcp_mod._create_direct_http_tool_handler(
        StreamableHttpParameters(
            url="https://merchant.example/mcp",
            headers={"Authorization": "Bearer TENANT-SECRET"},
        ),
        "probe",
    )
    result = await handler({}, None)

    assert result["status"] == "success"
    # the request went to the validated ADDRESS, not the name
    assert sent["url"] == "https://93.184.216.34/mcp"
    # while routing and TLS still carry the real host
    assert sent["headers"]["Host"] == "merchant.example"
    assert sent["extensions"]["sni_hostname"] == "merchant.example"
    assert sent["headers"]["Authorization"] == "Bearer TENANT-SECRET"


async def test_a_dns_blip_does_not_drop_a_declared_schemas_servers_tools(
    monkeypatch, logged
):
    """Flow build is not the gate for a server whose tools are declared.

    Such a server used to make no network call at build time. Now it resolves
    there, and dropping it on a failure costs its tools for the rest of the
    call — in voice, that is the whole conversation — over a blip. Every tool
    call re-checks and pins, so tolerating the blip costs nothing.
    """
    import app.ai.voice.agents.breeze_buddy.mcp as mcp_mod
    from app.ai.voice.agents.breeze_buddy.template.types import McpServerConfig

    async def flaky(hostname: str, port: int):
        raise OSError("temporary failure in name resolution")

    monkeypatch.setattr(ssrf_mod, "_resolve_host", flaky)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)

    params = await mcp_mod._build_server_params(
        McpServerConfig(
            name="erp",
            url="https://merchant.example/mcp",
            enabled=True,
            tool_schemas=[{"name": "stock", "description": "d", "properties": {}}],
        ),
        {},
    )

    assert params.url == "https://merchant.example/mcp"


async def test_a_dns_blip_refuses_when_nothing_checks_the_url_again(monkeypatch):
    """A server with no declared schemas goes out through pipecat's MCPClient,
    which resolves and follows redirects itself. Nothing re-checks that URL, so
    the build-time check is the whole policy — and an attacker running its
    authoritative nameserver could answer SERVFAIL here and 169.254.169.254 at
    connect, collecting the decrypted tenant credential. Fail closed instead.
    """
    import app.ai.voice.agents.breeze_buddy.mcp as mcp_mod
    from app.ai.voice.agents.breeze_buddy.template.types import McpServerConfig
    from app.core.network import EgressResolutionError

    async def flaky(hostname: str, port: int):
        raise OSError("temporary failure in name resolution")

    monkeypatch.setattr(ssrf_mod, "_resolve_host", flaky)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)

    with pytest.raises(EgressResolutionError):
        await mcp_mod._build_server_params(
            McpServerConfig(
                name="erp", url="https://merchant.example/mcp", enabled=True
            ),
            {},
        )


async def test_a_blip_does_not_abandon_a_call_from_a_pre_check(monkeypatch):
    """A pre-check failure is not a dropped tool — it is a call never placed.

    The pre-check path always goes through the direct handler, which validates
    and pins again on the call itself, so a resolver blip at build time is as
    harmless here as it is for a declared-schemas server. Keying the decision
    on tool_schemas instead of on what the caller actually does would fail a
    schema-less pre-check server closed: lead FINISHED, failure webhook sent,
    and the customer never rung.
    """
    import app.ai.voice.agents.breeze_buddy.mcp as mcp_mod
    from app.ai.voice.agents.breeze_buddy.template.types import McpServerConfig

    async def flaky(hostname: str, port: int):
        raise OSError("temporary failure in name resolution")

    monkeypatch.setattr(ssrf_mod, "_resolve_host", flaky)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)

    params = await mcp_mod._build_server_params(
        McpServerConfig(name="erp", url="https://merchant.example/mcp", enabled=True),
        {},
        rechecked_at_use=True,
    )

    assert params.url == "https://merchant.example/mcp"


async def test_the_pre_check_call_site_says_it_rechecks(monkeypatch):
    """Pins the wiring, not just the parameter: the flag has to be passed."""
    import app.ai.voice.agents.breeze_buddy.managers.pre_checks.http as pre_http

    seen: dict = {}

    async def _spy(server, template_vars, *, rechecked_at_use=None):
        seen["rechecked_at_use"] = rechecked_at_use
        raise ValueError("stop here — only the argument matters")

    monkeypatch.setattr(pre_http, "_build_server_params", _spy)

    from app.ai.voice.agents.breeze_buddy.template.types import McpServerConfig
    from app.schemas.breeze_buddy.core import PreCheckConfig, PreCheckType

    await pre_http._fetch_mcp_response(
        pre_check=PreCheckConfig(
            name="stock",
            type=PreCheckType.EXTERNAL_API,
            mcp=McpServerConfig(
                name="erp", url="https://merchant.example/mcp", enabled=True
            ),
            mcp_tool="check",
        ),
        context={},
        executor=HttpRequestExecutor(),
    )

    assert seen["rechecked_at_use"] is True


async def test_a_dns_blip_is_not_reported_to_the_model_as_a_refusal(monkeypatch):
    """`blocked by egress policy` tells the model to give up. A resolver blip
    is worth another try, and the other call sites already keep the two apart.
    """
    import app.ai.voice.agents.breeze_buddy.mcp as mcp_mod

    async def flaky(hostname: str, port: int):
        raise OSError("temporary failure in name resolution")

    monkeypatch.setattr(ssrf_mod, "_resolve_host", flaky)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)

    from mcp.client.session_group import StreamableHttpParameters

    handler = mcp_mod._create_direct_http_tool_handler(
        StreamableHttpParameters(url="https://merchant.example/mcp", headers={}),
        "probe",
    )
    result = await handler({}, None)

    assert result["status"] == "error"
    assert "egress policy" not in result["data"]
    assert "try again" in result["data"]


async def test_a_dead_first_address_falls_over_to_the_next(monkeypatch):
    """pinned_targets promises failover; the direct handler must honour it.

    Before pinning, httpx tried each resolved address. Using only the first
    turns one dead A record into a failed tool call — dead air on a voice call.
    """
    import httpx

    import app.ai.voice.agents.breeze_buddy.mcp as mcp_mod
    import app.core.network.httpx_request as httpx_mod

    tried: list = []

    class _Spy:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None, **kwargs):
            tried.append(url)
            if len(tried) == 1:
                raise httpx.ConnectError("no route to host")

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "jsonrpc": "2.0",
                        "id": "1",
                        "result": {"content": [{"type": "text", "text": "{}"}]},
                    }

            return _R()

    async def two_addresses(hostname: str, port: int):
        return ["93.184.216.34", "93.184.216.35"]

    monkeypatch.setattr(ssrf_mod, "_resolve_host", two_addresses)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)
    monkeypatch.setattr(httpx_mod.httpx, "AsyncClient", _Spy)

    from mcp.client.session_group import StreamableHttpParameters

    handler = mcp_mod._create_direct_http_tool_handler(
        StreamableHttpParameters(url="https://merchant.example/mcp", headers={}),
        "probe",
    )
    result = await handler({}, None)

    assert result["status"] == "success"
    assert tried == [
        "https://93.184.216.34/mcp",
        "https://93.184.216.35/mcp",
    ]
