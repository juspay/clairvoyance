"""The WhatsApp adapter: what it posts, and what it makes of the answer.

No network anywhere. httpx.MockTransport stands in for Meta, so the entire
error matrix — including the ones that are painful to provoke for real, like
an expired token — is exercised on every test run.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
import pytest

from app.crm.connectivity.providers.whatsapp import adapter as whatsapp_module
from app.crm.connectivity.providers.whatsapp.adapter import MetaWhatsAppAdapter
from app.crm.connectivity.providers.whatsapp.classify import (
    CREDENTIAL_CODES,
    RETRYABLE_CODES,
    TERMINAL_CODES,
)
from app.crm.connectivity.providers.whatsapp.payload import (
    build_parameters,
    to_meta_recipient,
)
from app.crm.connectivity.schemas.connector import ChannelBinding, ConnectorInstallation
from app.crm.connectivity.schemas.message import (
    CredentialBundle,
    QueuedMessage,
    SendRoute,
)
from app.crm.connectivity.schemas.template import ApprovedTemplate
from tests.crm.doubles import stub_http

ACCEPTED_BODY = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": "919876543210", "wa_id": "919876543210"}],
    "messages": [{"id": "wamid.HBgMOTE5ODc2NTQzMjEw"}],
}


def _message(**overrides) -> QueuedMessage:
    """A queued message for tests; keyword overrides replace any field."""
    fields = dict(
        id="m-1",
        merchant_id="shop",
        customer_id="c-1",
        channel="whatsapp",
        sent_to_address="+919876543210",
        source_kind="transactional",
        purpose_key="order_update",
        template_id="order_update_v1",
        variables={"1": "Priya", "2": "ORD-42"},
        dedupe_key="evt-1",
        attempt=1,
        next_attempt_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return QueuedMessage(**fields)


def _binding(**overrides) -> ChannelBinding:
    """An active channel binding for tests; overrides replace any field."""
    fields = dict(
        id="b-1",
        merchant_id="shop",
        channel="whatsapp",
        installation_id="i-1",
        address="PHONE_NUMBER_ID",
        capabilities={},
        is_primary=True,
        status="active",
    )
    fields.update(overrides)
    return ChannelBinding(**fields)


def _bundle(**values) -> CredentialBundle:
    """A credential bundle holding a usable token."""
    return CredentialBundle(values={"system_user_token": "tok", **values})


def _installation(**overrides) -> ConnectorInstallation:
    """The door a route hangs off; overrides replace any field."""
    fields = dict(
        id="i-1",
        merchant_id="shop",
        connector_key="whatsapp",
        external_account_id="waba-1",
        credential_id="cred-1",
        status="healthy",
    )
    fields.update(overrides)
    return ConnectorInstallation(**fields)


def _approved(language: str) -> ApprovedTemplate:
    """The registry row the send path resolved, in ``language``."""
    return ApprovedTemplate(id="t-1", name="order_update_v1", language=language)


def _route(**overrides) -> SendRoute:
    """Everything send() resolves, handed to the adapter as one object.

    ``template`` defaults to the registry row an approved template would have
    supplied — the adapter reads its language from there, never from the
    binding.
    """
    fields = dict(
        installation=_installation(),
        binding=_binding(),
        bundle=_bundle(),
        template=_approved("en_US"),
    )
    fields.update(overrides)
    return SendRoute(**fields)


def _mocked(monkeypatch, handler) -> Dict[str, Any]:
    """Point the adapter's HTTP client at a canned responder (the shared
    stub, tests/crm/doubles.py) and keep the LAST request as the dict this
    suite reads: url, headers, body."""
    seen: Dict[str, Any] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read().decode()
        return handler(request)

    stub_http(monkeypatch, whatsapp_module, _record)
    return seen


def _responds(status: int, body: Optional[dict] = None, text: Optional[str] = None):
    """A canned HTTP responder with the given status and body."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Test double: canned provider response."""
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body or {})

    return handler


async def _deliver(
    monkeypatch, handler, message=None, binding=None, bundle=None, route=None
):
    """Run deliver() against a mocked transport; return (outcome, request seen)."""
    seen = _mocked(monkeypatch, handler)
    if route is None:
        overrides = {}
        if binding is not None:
            overrides["binding"] = binding
        if bundle is not None:
            overrides["bundle"] = bundle
        route = _route(**overrides)
    outcome = await MetaWhatsAppAdapter().deliver(message or _message(), route)
    return outcome, seen


# --- the request ------------------------------------------------------------


async def test_the_send_goes_to_this_bindings_number(monkeypatch) -> None:
    """The send goes to this bindings number."""
    # The endpoint is per-endpoint, not per-merchant: two numbers under one
    # account must not share a URL.
    _, seen = await _deliver(monkeypatch, _responds(200, ACCEPTED_BODY))
    assert seen["url"].endswith("/PHONE_NUMBER_ID/messages")
    assert seen["headers"]["authorization"] == "Bearer tok"


async def test_the_recipient_is_sent_without_its_plus(monkeypatch) -> None:
    """The recipient is sent without its plus."""
    # Stored E.164, posted Meta-style. The stripped form is never persisted.
    _, seen = await _deliver(monkeypatch, _responds(200, ACCEPTED_BODY))
    assert '"to":"919876543210"' in seen["body"].replace(" ", "")


async def test_the_body_names_a_template_and_never_a_rendered_string(
    monkeypatch,
) -> None:
    """The body names a template and never a rendered string."""
    _, seen = await _deliver(monkeypatch, _responds(200, ACCEPTED_BODY))
    body = seen["body"].replace(" ", "")
    assert '"type":"template"' in body
    assert '"name":"order_update_v1"' in body
    # Values are posted as parameters; we never assemble the sentence.
    assert '"text":"Priya"' in body


def test_numeric_keys_become_positional_parameters_in_numeric_order() -> None:
    """Numeric keys become positional parameters in numeric order."""
    # Sorting as strings would put "10" before "2" and silently swap two
    # values in a customer's message. Ten consecutive keys, because a
    # template's placeholders run 1..N with no gaps — see the next test.
    values = {str(n): f"v{n}" for n in (2, 10, 1, 7, 3, 9, 4, 8, 5, 6)}
    params = build_parameters(values)
    assert isinstance(params, list)
    assert [p["text"] for p in params] == [f"v{n}" for n in range(1, 11)]
    assert all("parameter_name" not in p for p in params)


def test_gapped_positional_keys_are_refused_rather_than_compacted() -> None:
    """Meta reads body parameters BY POSITION, so a gap renumbers everything
    after it: {"1": name, "3": order} would send the order as {{2}} and leave
    a template reading {{3}} a parameter short. The message still looks
    delivered, with the wrong values in it — so the defect is named before
    anything is posted."""
    defect = build_parameters({"1": "Priya", "3": "ORD-42"})
    assert isinstance(defect, str)
    assert "no gaps" in defect


def test_positional_keys_must_start_at_one() -> None:
    """A set starting at 2 is the same corruption from the other end."""
    defect = build_parameters({"2": "Priya", "3": "ORD-42"})
    assert isinstance(defect, str)
    assert "no gaps" in defect


def test_named_keys_become_named_parameters() -> None:
    """Named keys become named parameters."""
    params = build_parameters({"customer_name": "Priya"})
    assert params == [
        {"type": "text", "parameter_name": "customer_name", "text": "Priya"}
    ]


def test_no_variables_means_no_components() -> None:
    """No variables means no components."""
    assert build_parameters({}) == []


def test_mixed_key_styles_are_refused_not_guessed() -> None:
    """Mixed key styles are refused not guessed."""
    # Meta takes positional OR named per request, never both. The old
    # behaviour guessed named — emitting parameter_name='1' — and spent a
    # network round trip to receive the refusal this defect already states.
    defect = build_parameters({"1": "x", "otp": "y"})
    assert isinstance(defect, str)
    assert "mixes" in defect


def test_untextable_values_are_refused_not_coerced() -> None:
    """Untextable values are refused not coerced."""
    # str() rendered a JSON null as the literal word 'None' inside the
    # customer's message — corruption that LOOKS delivered. Numbers keep
    # their one obvious text form; everything else is a producer bug this
    # refusal surfaces.
    ok = build_parameters({"1": "Priya", "2": 42, "3": 9.5})
    assert ok == [
        {"type": "text", "text": "Priya"},
        {"type": "text", "text": "42"},
        {"type": "text", "text": "9.5"},
    ]
    for bad in (None, True, ["a"], {"a": 1}):
        defect = build_parameters({"1": "Priya", "2": bad})
        assert isinstance(defect, str), bad
        assert "'2'" in defect


def test_a_variable_defect_names_the_key_and_type_never_the_value() -> None:
    """A variable defect names the key and type never the value."""
    # Variable values can be personal data, and the defect string is
    # destined for a log line.
    defect = build_parameters({"otp": ["123456"]})
    assert isinstance(defect, str)
    assert "otp" in defect and "list" in defect
    assert "123456" not in defect


def test_a_unicode_digit_key_is_a_name_not_a_crash() -> None:
    """A unicode digit key is a name not a crash."""
    # '²'.isdigit() is True but int('²') raises: sorting by int() turned this
    # legal jsonb key into a mid-send exception that burned every attempt as
    # 'send_error'. As a (doomed) NAME, Meta's refusal is a classified,
    # terminal answer instead.
    assert build_parameters({"²": "x"}) == [
        {"type": "text", "parameter_name": "²", "text": "x"}
    ]


async def test_mixed_variables_are_blocked_before_posting(monkeypatch) -> None:
    """Mixed variables are blocked before posting — OUR refusal, not Meta's."""
    seen = _mocked(monkeypatch, _responds(200, ACCEPTED_BODY))
    outcome = await MetaWhatsAppAdapter().deliver(
        _message(variables={"1": "x", "otp": "y"}), _route()
    )
    assert outcome.status == "blocked"
    assert outcome.reason == "template_variables_invalid"
    assert outcome.retryable is False
    # Nothing was posted: no rendering of a mixed dict is the right one.
    assert seen == {}


async def test_a_null_variable_is_blocked_before_posting(monkeypatch) -> None:
    """A null variable is blocked before posting — OUR refusal, not Meta's."""
    seen = _mocked(monkeypatch, _responds(200, ACCEPTED_BODY))
    outcome = await MetaWhatsAppAdapter().deliver(
        _message(variables={"1": "Priya", "2": None}), _route()
    )
    assert outcome.status == "blocked"
    assert outcome.reason == "template_variables_invalid"
    assert outcome.retryable is False
    # Nothing was posted: 'Hi Priya, your order None…' must never exist.
    assert seen == {}


def test_the_language_comes_from_the_template_registry() -> None:
    """The language comes from the route, which took it from the registry."""
    # Which locale a template was APPROVED in is a fact about the template,
    # not about the endpoint — the binding's capabilities blob used to answer
    # this, and could disagree with what Meta actually approved.
    adapter = MetaWhatsAppAdapter()
    parameters = build_parameters(_message().variables)
    assert isinstance(parameters, list)
    payload = adapter.build_payload(
        _message(), "919876543210", _route(template=_approved("hi")), parameters
    )
    assert payload["template"]["language"]["code"] == "hi"


def test_a_route_without_a_registry_row_falls_back_rather_than_crashing() -> None:
    """A route carrying no template row still renders — the T23 lookup makes
    this unreachable on WhatsApp (the door refuses first), so it exists only
    so a misrouted call cannot take the worker down."""
    adapter = MetaWhatsAppAdapter()
    parameters = build_parameters(_message().variables)
    assert isinstance(parameters, list)
    default = adapter.build_payload(
        _message(), "919876543210", _route(template=None), parameters
    )
    assert default["template"]["language"]["code"] == "en_US"


# --- refusals that never reach the network ----------------------------------


async def test_a_bundle_without_a_token_is_blocked(monkeypatch) -> None:
    """A missing bundle key is OUR refusal — 'blocked', the same status this
    reason carries from resolve_send_route, never Meta's word 'failed'."""
    seen = _mocked(monkeypatch, _responds(200, ACCEPTED_BODY))
    outcome = await MetaWhatsAppAdapter().deliver(
        _message(), _route(bundle=CredentialBundle(values={"app_secret": "x"}))
    )
    assert outcome.status == "blocked"
    assert outcome.reason == "connector_credential_missing"
    assert outcome.retryable is False
    # Nothing was posted: a bundle missing its key cannot be fixed by asking
    # Meta about it.
    assert seen == {}


async def test_a_message_without_a_template_is_blocked(monkeypatch) -> None:
    """A message without a template is blocked — terminally, before posting."""
    seen = _mocked(monkeypatch, _responds(200, ACCEPTED_BODY))
    outcome = await MetaWhatsAppAdapter().deliver(_message(template_id=None), _route())
    assert outcome.status == "blocked"
    assert outcome.reason == "template_missing"
    assert outcome.retryable is False
    assert seen == {}


@pytest.mark.parametrize(
    "address", ["", "+1234", "not-a-number", "+" + "9" * 20, "+0123456789"]
)
async def test_an_unusable_address_is_blocked_before_posting(
    monkeypatch, address
) -> None:
    """An unusable address is blocked before posting — WE refused, Meta never
    saw it, so the manifest must not show the word reserved for Meta's no."""
    seen = _mocked(monkeypatch, _responds(200, ACCEPTED_BODY))
    outcome = await MetaWhatsAppAdapter().deliver(
        _message(sent_to_address=address), _route()
    )
    assert outcome.status == "blocked"
    assert outcome.reason == "recipient_address_invalid"
    assert seen == {}


def test_recipient_normalisation_accepts_only_plausible_numbers() -> None:
    """Recipient normalisation accepts only plausible numbers."""
    # An Indian mobile is 12 digits in E.164: +91 plus the 10 national ones.
    assert to_meta_recipient("+91 98765-43210") == "919876543210"
    assert to_meta_recipient("9" * 16) is None  # past E.164's 15-digit ceiling
    assert to_meta_recipient("+12345") is None  # 5 digits, below any country
    assert to_meta_recipient("") is None


def test_the_accepted_length_window_matches_what_this_system_stores() -> None:
    """The accepted length window matches what this system stores."""
    # normalize.py and the platform_identity CHECK both allow +[1-9][0-9]{6,14}
    # — 7 to 15 digits. A tighter bound here would reject a number the system
    # was happy to store, and report it as an invalid address rather than as
    # the mismatch it is.
    from app.crm.shared.normalize import _E164

    for length in range(4, 18):
        stored = "+" + "9" * length
        assert (_E164.match(stored) is not None) == (
            to_meta_recipient(stored) is not None
        ), length
    # The [1-9] half of the same parity: no country code starts with 0, and
    # normalize.py refuses to store one — so accepting it here would post a
    # number the system would never have stored, and report Meta's code
    # instead of our recipient_address_invalid.
    assert _E164.match("+0123456789") is None
    assert to_meta_recipient("+0123456789") is None
    assert to_meta_recipient("0123456789") is None


# --- reading Meta's answer ---------------------------------------------------


async def test_an_accepted_send_records_the_wamid(monkeypatch) -> None:
    """An accepted send records the wamid."""
    outcome, _ = await _deliver(monkeypatch, _responds(200, ACCEPTED_BODY))
    assert outcome.status == "accepted"
    assert outcome.provider_message_id == "wamid.HBgMOTE5ODc2NTQzMjEw"


async def test_a_2xx_without_a_wamid_is_still_accepted(monkeypatch) -> None:
    """A 2xx without a wamid is still accepted."""
    # Meta took it. Calling this a failure would retry a message the customer
    # may already have — losing the receipt link is the smaller harm.
    outcome, _ = await _deliver(monkeypatch, _responds(200, {"messages": []}))
    assert outcome.status == "accepted"
    assert outcome.provider_message_id is None


@pytest.mark.parametrize("code", sorted(RETRYABLE_CODES))
async def test_pacing_errors_are_retryable(monkeypatch, code) -> None:
    """Pacing errors are retryable."""
    outcome, _ = await _deliver(
        monkeypatch, _responds(400, {"error": {"code": int(code), "message": "slow"}})
    )
    assert outcome.status == "failed"
    assert outcome.reason == code
    assert outcome.retryable is True
    # Pacing is not a verdict on the connection.


@pytest.mark.parametrize("code", sorted(TERMINAL_CODES))
async def test_message_level_refusals_never_retry(monkeypatch, code) -> None:
    """Message level refusals never retry."""
    outcome, _ = await _deliver(
        monkeypatch, _responds(400, {"error": {"code": int(code), "message": "no"}})
    )
    assert outcome.status == "failed"
    # The provider's own code, verbatim: a merchant asking "why" gets an
    # answer that matches Meta's documentation.
    assert outcome.reason == code
    assert outcome.retryable is False


@pytest.mark.parametrize("code", sorted(CREDENTIAL_CODES))
async def test_credential_refusals_flag_the_connection(monkeypatch, code) -> None:
    """Credential refusals flag the connection."""
    outcome, _ = await _deliver(
        monkeypatch,
        _responds(401, {"error": {"code": int(code), "message": "bad token"}}),
    )
    assert outcome.status == "failed"
    assert outcome.retryable is False
    # The provider's code lands on the row verbatim. That IS the signal the
    # channel module watches to decide the connection needs re-authenticating
    # — the send path deliberately does not act on it itself.
    assert outcome.reason == code


async def test_a_429_without_a_code_is_still_retryable(monkeypatch) -> None:
    """A 429 without a code is still retryable."""
    outcome, _ = await _deliver(monkeypatch, _responds(429, {}))
    assert outcome.retryable is True


async def test_an_unknown_5xx_is_retryable_and_an_unknown_4xx_is_not(
    monkeypatch,
) -> None:
    """An unknown 5xx is retryable and an unknown 4xx is not."""
    # Meta's problem may pass; ours will not, and three attempts would learn
    # nothing.
    server, _ = await _deliver(monkeypatch, _responds(503, {}))
    assert server.retryable is True
    assert server.reason == "http_503"

    client, _ = await _deliver(
        monkeypatch, _responds(400, {"error": {"code": 999999, "message": "?"}})
    )
    assert client.retryable is False
    assert client.reason == "999999"


async def test_a_provider_error_echoing_the_recipient_never_reaches_the_log(
    monkeypatch,
) -> None:
    """A provider error echoing the recipient never reaches the log."""
    # Meta's catalog strings carry no values today. This pins that even if
    # that contract breaks — or a proxy rewrites the body — the echoed
    # number dies at the log boundary, while the code survives for
    # classification and support.
    lines = []

    class _Recorder:
        def warning(self, msg):
            """Collect the log line."""
            lines.append(msg)

        def error(self, msg):
            """Collect the log line."""
            lines.append(msg)

        def info(self, msg):
            """Collect the log line."""
            lines.append(msg)

    monkeypatch.setattr(whatsapp_module, "logger", _Recorder())
    outcome, _ = await _deliver(
        monkeypatch,
        _responds(
            400,
            {"error": {"code": 100, "message": "Invalid parameter: to=919876543210"}},
        ),
    )
    assert outcome.reason == "100"
    joined = " ".join(lines)
    assert "919876543210" not in joined
    assert "code=100" in joined


async def test_a_non_json_response_does_not_crash_the_worker(monkeypatch) -> None:
    """A non json response does not crash the worker."""
    # A load balancer returning HTML must degrade to "failed, no detail",
    # not raise a JSONDecodeError that reads like a code bug.
    outcome, _ = await _deliver(
        monkeypatch, _responds(502, text="<html>bad gateway</html>")
    )
    assert outcome.status == "failed"
    assert outcome.retryable is True


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.PoolTimeout("x")],
)
async def test_a_transport_failure_is_retryable(monkeypatch, error) -> None:
    """A transport failure is retryable."""

    # "No answer" is not "no": the provider may have taken it.
    def handler(request: httpx.Request) -> httpx.Response:
        """Test double: canned provider response."""
        raise error

    outcome, _ = await _deliver(monkeypatch, handler)
    assert outcome.status == "failed"
    assert outcome.reason == "transport_error"
    assert outcome.retryable is True


# --- the classification table itself ----------------------------------------


def test_no_error_code_is_claimed_by_two_classes() -> None:
    """No error code is claimed by two classes."""
    # An overlap would make the outcome depend on the order of the branches
    # in read_response, which is exactly the kind of bug that shows up as
    # "sometimes it retries".
    assert RETRYABLE_CODES & TERMINAL_CODES == set()
    assert RETRYABLE_CODES & CREDENTIAL_CODES == set()
    assert TERMINAL_CODES & CREDENTIAL_CODES == set()


def test_the_endpoint_is_built_from_the_configured_dials() -> None:
    """The endpoint is built from the configured dials."""
    adapter = MetaWhatsAppAdapter(
        base_url="http://localhost:9999/", api_version="v99.0"
    )
    assert adapter.endpoint("PN1") == "http://localhost:9999/v99.0/PN1/messages"


def test_a_malformed_address_cannot_become_url_structure() -> None:
    """A malformed address cannot become url structure."""
    # The address column has no format CHECK and no writer validates it. A
    # '/' or '?' in a bad row must stay inside its one path segment — the
    # alternative posts the merchant's bearer token to whatever Graph path
    # the junk spells out.
    adapter = MetaWhatsAppAdapter(base_url="http://stub", api_version="v23.0")
    assert (
        adapter.endpoint("123/other?x=")
        == "http://stub/v23.0/123%2Fother%3Fx%3D/messages"
    )


async def test_a_control_character_address_does_not_escape_deliver(
    monkeypatch,
) -> None:
    """A control character address does not escape deliver."""
    # Unquoted, a '\n' in the address raised httpx.InvalidURL — which is not
    # an httpx.HTTPError — straight past the transport catch, and the row
    # burned every attempt as 'send_error'. Quoted, the request is made and
    # Meta's refusal comes back as a classified, terminal outcome.
    outcome, seen = await _deliver(
        monkeypatch,
        _responds(400, {"error": {"code": 100, "message": "no"}}),
        binding=_binding(address="PN\n1"),
    )
    assert outcome.status == "failed"
    assert outcome.reason == "100"
    assert outcome.retryable is False
    assert "/PN%0A1/messages" in seen["url"]
