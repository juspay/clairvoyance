"""merchant_http (enh A/03): a merchant's own endpoint as a connector — the
door validates the address once, the one verb `request` carries the run's
facts out and the declared answer back onto the run, and every hop crosses
the egress guard. No network: the guard's DNS and the request itself are
stubbed at the seam actions.py calls.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import aiohttp
import pytest
from pydantic import ValidationError

import app.crm.connectivity.providers.merchant_http.actions as face_module
from app.core.security import ssrf
from app.crm.connectivity.accounts import AccountError
from app.crm.connectivity.connectors import CONNECTORS
from app.crm.connectivity.contracts import action_declares, action_names
from app.crm.connectivity.providers.base import ActionError, ConnectorHandshakeError
from app.crm.connectivity.providers.merchant_http.actions import (
    MAX_RESPONSE_BYTES,
    Request,
    RequestArgs,
)
from app.crm.connectivity.providers.merchant_http.onboard import (
    MerchantHttpOnboarder,
    OnboardMerchantHttpRequest,
    normalised_base_url,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.connectivity.schemas.message import CredentialBundle
from app.crm.outreach import plans
from app.crm.outreach.catalog_laws import Catalogs
from app.crm.outreach.nodes import action as action_node
from app.crm.outreach.nodes.context import run_facts
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition
from app.crm.record import catalog

# --- the registry ------------------------------------------------------------


def test_the_connector_is_registered_with_one_verb_and_no_channel() -> None:
    spec = CONNECTORS["merchant_http"]
    assert spec.channel is None and spec.templates is None
    assert spec.exclusive_account is False
    assert action_names("merchant_http") == ["request"]
    assert action_declares(
        "merchant_http",
        "request",
        {"path": "/x", "facts": {"payment_link": "data.link"}},
    ) == ["payment_link"]
    assert action_declares("merchant_http", "request", {"path": "/x"}) == []
    assert action_declares("shopify", "add_tag", {"order_id": "1", "tags": ["a"]}) == []


# --- the door ----------------------------------------------------------------


def _onboard_request(**overrides: Any) -> OnboardMerchantHttpRequest:
    fields: Dict[str, Any] = {
        "merchant_id": "m1",
        "account_name": "credit-line",
        "base_url": "https://api.example.com/v1",
    }
    fields.update(overrides)
    return OnboardMerchantHttpRequest(**fields)


def test_the_base_url_is_normalised_and_only_https() -> None:
    assert (
        normalised_base_url("HTTPS://Api.Example.com/v1/")
        == "https://api.example.com/v1"
    )
    for bad, why in (
        ("http://api.example.com", "https"),
        ("https://api.example.com/x?y=1", "query"),
        ("https://api.example.com#", "fragment"),  # an EMPTY fragment
        ("https://api.example.com?", "query"),  # an EMPTY query
        ("https://api.example.com/v1/#", "fragment"),
        ("https://user:pw@api.example.com", "credentials"),
        ("https://{host}/x", "placeholders"),
        ("https:///x", "host"),
    ):
        with pytest.raises(ValueError, match=why):
            normalised_base_url(bad)


def test_the_account_name_is_a_plain_token() -> None:
    assert _onboard_request(account_name=" credit-line ").account_name == "credit-line"
    for bad in ("", "a b", "../x", "x" * 65):
        with pytest.raises(ValidationError):
            _onboard_request(account_name=bad)


def test_the_auth_header_and_value_come_together() -> None:
    _onboard_request()
    _onboard_request(auth_header="Authorization", auth_value="Bearer t")
    with pytest.raises(ValidationError, match="together"):
        _onboard_request(auth_header="X-Key")


@pytest.mark.parametrize(
    "header", ["Host", "accept", "Idempotency-Key", "Bad Header", "X:Key"]
)
def test_the_auth_header_may_not_be_one_the_request_sets(header: str) -> None:
    with pytest.raises(ValidationError):
        _onboard_request(auth_header=header, auth_value="v")


def test_the_auth_value_never_reaches_a_repr() -> None:
    assert "s3cret" not in repr(
        _onboard_request(auth_header="X-Key", auth_value="s3cret")
    )


def _public_dns(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (ip, port or 443))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(ssrf, "_ALLOW_PRIVATE_EGRESS", False)


def test_onboarding_refuses_an_address_the_guard_would_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cheap check, at the console, so a merchant hears it while typing
    rather than parking every run that names the connector."""
    onboarder = MerchantHttpOnboarder()
    _public_dns(monkeypatch, ip="10.0.0.5")
    with pytest.raises(ConnectorHandshakeError, match="base_url refused"):
        asyncio.run(
            onboarder.gather(_onboard_request(base_url="https://internal.example.com"))
        )


def test_the_door_is_the_account_name_with_its_base_url_in_door_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account name is the account id AND the door_name a plan picks it
    by; where the API lives is door_data; the secret is the bundle only."""
    _public_dns(monkeypatch)
    onboarder = MerchantHttpOnboarder()
    request = _onboard_request(
        auth_header="Authorization",
        auth_value="Bearer t",
        display_label="Credit line API",
    )
    result = asyncio.run(onboarder.gather(request))
    assert result.external_account_id == "credit-line"
    assert result.door_name == "credit-line"
    assert result.door_data == {"base_url": "https://api.example.com/v1"}
    assert result.display_label == "Credit line API"
    assert result.address is None and result.health_level == "healthy"
    assert result.bundle == {"auth_header": "Authorization", "auth_value": "Bearer t"}
    assert onboarder.identify(request) == ("credit-line", None)


# --- the verb's contract -----------------------------------------------------


def test_the_path_is_a_path_and_facts_are_plain_names() -> None:
    RequestArgs(path="/credit-line/link", body={"customer_id": "{customer_id}"})
    for bad in ("credit-line", "https://x.com/a", "//x"):
        with pytest.raises(ValidationError):
            RequestArgs(path=bad)
    with pytest.raises(ValidationError, match="identifier"):
        RequestArgs(path="/x", facts={"payment link": "data.link"})
    assert RequestArgs(path="/x").method == "POST"


# --- performing it -----------------------------------------------------------


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.chunks_read = 0

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._raw), size):
            self.chunks_read += 1
            yield self._raw[start : start + size]


class _Response:
    def __init__(self, status: int, raw: bytes) -> None:
        self.status = status
        self.content = _Content(raw)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    status: int = 200,
    text: Any = '{"data": {"link": "https://pay.example.com/abc", "n": 3}}',
    bundle: Optional[Dict[str, Any]] = None,
    raises: Optional[BaseException] = None,
    bundle_raises: Optional[BaseException] = None,
) -> List[Dict[str, Any]]:
    """Stub the seam: the guarded request (no network) and the vault read."""
    sent: List[Dict[str, Any]] = []
    raw = text if isinstance(text, bytes) else text.encode("utf-8")

    @asynccontextmanager
    async def fake_request(session, method, url, **kwargs):
        response = _Response(status, raw)
        sent.append({"method": method, "url": url, "response": response, **kwargs})
        if raises is not None:
            raise raises
        yield response

    @asynccontextmanager
    async def fake_session(**kwargs):
        yield object()

    async def fake_bundle(installation):
        if bundle_raises is not None:
            raise bundle_raises
        return CredentialBundle(values=bundle or {})

    monkeypatch.setattr(face_module, "ssrf_safe_request", fake_request)
    monkeypatch.setattr(face_module, "create_aiohttp_session", fake_session)
    monkeypatch.setattr(face_module, "bundle_for", fake_bundle)
    return sent


def _door(
    credential: Optional[str] = "cred-1",
    door_data: Optional[Dict[str, Any]] = None,
) -> ConnectorInstallation:
    return ConnectorInstallation(
        id="inst-1",
        merchant_id="m1",
        connector_key="merchant_http",
        external_account_id="credit-line",
        display_label="Credit line API",
        credential_id=credential,
        status="healthy",
        door_data=(
            door_data
            if door_data is not None
            else {"base_url": "https://api.example.com/v1"}
        ),
        door_name="credit-line",
    )


_CTX = {"run_id": "run-1", "node_id": "get-link", "visit": 1}
_ARGS = RequestArgs(
    path="/credit-line/link",
    body={"customer_id": "FK1"},
    facts={"payment_link": "data.link", "count": "data.n"},
)


def _perform(door=None, args=_ARGS, ctx=_CTX) -> Dict[str, Any]:
    return asyncio.run(Request().perform("m1", door or _door(), args, ctx))


def test_the_request_goes_to_door_data_with_auth_and_the_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(
        monkeypatch, bundle={"auth_header": "Authorization", "auth_value": "Bearer t"}
    )
    assert _perform() == {
        "ok": True,
        "status": 200,
        "facts": {"payment_link": "https://pay.example.com/abc", "count": 3},
    }
    (call,) = sent
    assert call["method"] == "POST"
    assert call["url"] == "https://api.example.com/v1/credit-line/link"
    assert call["json"] == {"customer_id": "FK1"}
    assert call["headers"]["Accept"] == "application/json"
    assert call["headers"]["Authorization"] == "Bearer t"
    assert call["headers"]["Idempotency-Key"] == "run-1:get-link:1"
    # Only the base URL's host, and redirects only back to it (finding 6).
    assert call["allowed_host_suffixes"] == ["api.example.com"]
    assert call["same_origin_only"] is True


def test_a_revisit_is_a_new_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _install(monkeypatch)
    _perform(ctx={**_CTX, "visit": 2})
    assert sent[0]["headers"]["Idempotency-Key"] == "run-1:get-link:2"


def test_a_door_without_door_data_base_url_parks(monkeypatch) -> None:
    _install(monkeypatch)
    with pytest.raises(ActionError, match="door_data.base_url"):
        _perform(door=_door(door_data={}))


def test_a_door_without_a_credential_sends_no_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _install(monkeypatch)
    _perform(door=_door(credential=None))
    assert "Authorization" not in sent[0]["headers"]


@pytest.mark.parametrize("header", ["Host", "ACCEPT", "idempotency-key"])
def test_a_vaulted_auth_header_never_overwrites_ours(monkeypatch, header) -> None:
    """Onboarding refuses these; a hand-edited vault row is still stopped
    before a request goes out."""
    sent = _install(monkeypatch, bundle={"auth_header": header, "auth_value": "x"})
    with pytest.raises(ActionError, match="overwrite"):
        _perform()
    assert sent == []


def test_a_dead_credential_parks_instead_of_retrying(monkeypatch) -> None:
    """Finding 3: deactivated or undecryptable is a defect — the retry ladder
    would read the same dead row every time."""
    sent = _install(
        monkeypatch, bundle_raises=AccountError("credential is deactivated")
    )
    with pytest.raises(ActionError, match="credential unusable"):
        _perform()
    assert sent == []


def test_a_get_sends_query_and_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _install(monkeypatch, text='{"data": {"link": "L"}}')
    args = RequestArgs(
        path="/link",
        method="GET",
        query={"c": "FK1"},
        facts={"payment_link": "data.link"},
    )
    _perform(args=args)
    assert sent[0]["params"] == {"c": "FK1"} and sent[0]["json"] is None


def test_no_door_is_a_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    with pytest.raises(ActionError, match="no merchant_http endpoint"):
        asyncio.run(Request().perform("m1", None, _ARGS, _CTX))


@pytest.mark.parametrize(
    "status,text,match",
    [
        (404, "no such customer", "refused \\(404\\)"),
        (200, '{"data": {}}', "no single value at 'data.link'"),
        (200, '{"data": {"link": {"nested": 1}, "n": 3}}', "no single value"),
        (200, '{"data": {"link": ["a"], "n": 3}}', "no single value"),
        (200, '{"data": {"link": true, "n": 3}}', "no single value"),
        (200, "<html>", "not JSON"),
    ],
)
def test_defects_park(monkeypatch, status, text, match) -> None:
    _install(monkeypatch, status=status, text=text)
    with pytest.raises(ActionError, match=match):
        _perform()


def test_an_egress_refusal_is_a_defect(monkeypatch) -> None:
    _install(monkeypatch, raises=ssrf.SSRFError("Blocked egress to private address"))
    with pytest.raises(ActionError, match="egress refused"):
        _perform()


@pytest.mark.parametrize(
    "raises,match",
    [
        (asyncio.TimeoutError(), "unreachable"),
        (aiohttp.ClientConnectionError("reset"), "unreachable"),
        # Finding 2: a DNS hiccup is a bad moment, not a blocked host.
        (ssrf.EgressResolutionError("Could not resolve host"), "did not resolve"),
    ],
)
def test_bad_moments_retry(monkeypatch, raises, match) -> None:
    _install(monkeypatch, raises=raises)
    with pytest.raises(RuntimeError, match=match) as caught:
        _perform()
    assert not isinstance(caught.value, ActionError)


@pytest.mark.parametrize("status", [429, 502])
def test_a_429_or_5xx_retries(monkeypatch, status) -> None:
    _install(monkeypatch, status=status, text="later")
    with pytest.raises(RuntimeError, match=str(status)):
        _perform()


def test_an_oversize_answer_is_refused_without_reading_it_all(monkeypatch) -> None:
    """Finding 5: the read stops at the cap — a 64 MB answer is never held."""
    huge = b"x" * (64 * 1024 * 1024)
    sent = _install(monkeypatch, text=huge)
    with pytest.raises(ActionError, match=f"more than {MAX_RESPONSE_BYTES} bytes"):
        _perform()
    content = sent[0]["response"].content
    assert content.chunks_read * face_module._READ_CHUNK_BYTES <= 2 * MAX_RESPONSE_BYTES


def test_a_body_just_under_the_cap_is_read(monkeypatch) -> None:
    padding = "y" * (MAX_RESPONSE_BYTES - 100)
    _install(
        monkeypatch, text='{"data": {"link": "L", "n": 1}, "p": "' + padding + '"}'
    )
    assert _perform()["facts"] == {"payment_link": "L", "count": 1}


def test_a_non_utf8_body_is_a_defect_not_a_retry(monkeypatch) -> None:
    """Finding 7: a UnicodeDecodeError would read as a bad moment and re-send
    a POST that may already have succeeded."""
    _install(monkeypatch, text=b"\xff\xfe not json")
    with pytest.raises(ActionError, match="not JSON"):
        _perform()


def test_an_answer_with_no_declared_facts_is_just_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fire-and-forget notification: no facts asked, the body not parsed."""
    _install(monkeypatch, status=204, text="")
    assert _perform(args=RequestArgs(path="/nudged")) == {
        "ok": True,
        "status": 204,
        "facts": {},
    }


# --- the headline: a declared fact reaches a later square (finding 1) --------

_ACTION = {
    "id": "get-link",
    "type": "action",
    "connector": "merchant_http",
    "action": "request",
    "door_name": "credit-line",
    "args": {
        "path": "/credit-line/link",
        "body": {"customer_id": "{userId}"},
        "facts": {"payment_link": "data.link"},
    },
}
_SEND = {
    "id": "share-link",
    "type": "send",
    "channel": "whatsapp",
    "template": "t",
    "variables": {"1": "payment_link"},
}


def _catalogs() -> Catalogs:
    return {
        entry.topic: {f.path: f for f in entry.fields}
        for entry in catalog.code_entries()
    }


def _board(action: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entry": {"topic": "flow_action"},
        "nodes": [action, _SEND],
        "edges": [["get-link", "share-link"]],
        "goal": {"topics": ["order.placed"]},
        "purpose_key": "credit.line.link",
    }


def test_publish_admits_a_later_square_naming_a_declared_fact() -> None:
    assert plans.validate_definition(_board(_ACTION), catalogs=_catalogs()) == []


def test_publish_refuses_the_same_name_when_no_action_declares_it() -> None:
    undeclared = {**_ACTION, "args": {**_ACTION["args"], "facts": {}}}
    problems = plans.validate_definition(_board(undeclared), catalogs=_catalogs())
    assert any("payment_link" in p and "not a declared variable" in p for p in problems)


def test_publish_refuses_a_declared_fact_the_walker_keeps_for_itself() -> None:
    clash = {
        **_ACTION,
        "args": {**_ACTION["args"], "facts": {"phone": "data.phone"}},
    }
    problems = plans.validate_definition(_board(clash), catalogs=_catalogs())
    assert any("'phone'" in p and "walker keeps" in p for p in problems)


def _run(context: Dict[str, Any]) -> EnrollmentRun:
    now = datetime.now(timezone.utc)
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node="get-link",
        wake_at=now,
        entered_at=now - timedelta(minutes=5),
        exited_at=None,
        exit_reason=None,
        context=context,
        enrollment_key="c-1",
        attempts=1,
        last_error=None,
    )


async def test_a_later_square_reads_the_declared_fact(monkeypatch) -> None:
    """End to end over the walker's own pieces: the action writes the fact
    onto the run, and run_facts at the NEXT square offers it to a send."""
    seen: List[Dict[str, Any]] = []

    async def fake_perform(merchant_id, connector, action, args, context, door_name):
        seen.append({"context": context, "door_name": door_name, "args": args})
        return {
            "ok": True,
            "status": 200,
            # a walker key slipped past publish must still never be written
            "facts": {"payment_link": "https://pay.example.com/abc", "phone": "x"},
        }

    monkeypatch.setattr(action_node, "perform_action", fake_perform)
    definition = WorkflowDefinition.model_validate(_board(_ACTION))
    action, send = definition.nodes
    run = _run({"userId": "FK1", "phone": "+919876543210"})

    patch = await action_node.execute(run, action, definition)

    assert patch["payment_link"] == "https://pay.example.com/abc"
    assert "phone" not in patch
    assert patch["action_get-link"]["visit"] == 1
    assert seen[0]["door_name"] == "credit-line"
    assert seen[0]["context"]["visit"] == 1
    assert seen[0]["args"]["body"] == {"customer_id": "FK1"}

    merged = {**run.context, **patch}
    assert run_facts(merged, send)["payment_link"] == "https://pay.example.com/abc"

    # Coming back to the square is a new visit — a new idempotency key.
    await action_node.execute(_run(merged), action, definition)
    assert seen[1]["context"]["visit"] == 2
