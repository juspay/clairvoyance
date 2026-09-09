"""The WhatsApp adapter: what it posts, and what it makes of the answer.

No network anywhere. httpx.MockTransport stands in for Meta, so the entire
error matrix — including the ones that are painful to provoke for real, like
an expired token — is exercised on every test run.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
    build_send_body,
    flow_button_indexes,
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


def _approved(language: str, **overrides) -> ApprovedTemplate:
    """The registry row the send path resolved, in ``language``."""
    return ApprovedTemplate(
        id="t-1", name="order_update_v1", language=language, **overrides
    )


def _buttons(*types: str) -> List[Dict[str, Any]]:
    """A registered BUTTONS component whose buttons have these Meta types —
    the components blob a flow template's registry row carries."""
    return [
        {"type": "BODY", "text": "Hello {{1}}"},
        {"type": "BUTTONS", "buttons": [{"type": t, "text": t} for t in types]},
    ]


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


async def test_a_flow_button_template_carries_its_action_component(
    monkeypatch,
) -> None:
    """A FLOW button is not decoration: Meta refuses the WHOLE send with
    131009 ("Components sub_type invalid at index: N") when the button
    component is missing, so nothing reaches the customer. The index is the
    button's position among the template's buttons, taken from the registry
    row — this adapter cannot know it any other way."""
    route = _route(
        template=_approved(
            "en_US", components=_buttons("QUICK_REPLY", "QUICK_REPLY", "FLOW")
        )
    )
    _, seen = await _deliver(monkeypatch, _responds(200, ACCEPTED_BODY), route=route)
    body = json.loads(seen["body"])
    button = body["template"]["components"][-1]
    assert button["type"] == "button"
    assert button["sub_type"] == "flow"
    # Meta wants the position as a string, like every other button component.
    assert button["index"] == "2"
    assert button["parameters"][0]["type"] == "action"


async def test_the_flow_token_is_the_message_that_opened_the_form(
    monkeypatch,
) -> None:
    """Meta echoes flow_token back verbatim inside her submission, so the
    send stamps the manifest row's own id: the answer then names its own
    question, with no wamid to wait for and no second lookup."""
    route = _route(template=_approved("en_US", components=_buttons("FLOW")))
    _, seen = await _deliver(
        monkeypatch,
        _responds(200, ACCEPTED_BODY),
        message=_message(id="m-42"),
        route=route,
    )
    action = json.loads(seen["body"])["template"]["components"][-1]["parameters"][0]
    assert action["action"] == {"flow_token": "m-42"}


async def test_every_flow_button_is_named_not_only_the_first(monkeypatch) -> None:
    """Meta refuses the send for ANY unnamed flow button, so naming the first
    and stopping would lose the whole message to the second. The token is the
    same on both: it identifies the send, not which button she pressed."""
    route = _route(
        template=_approved("en_US", components=_buttons("FLOW", "QUICK_REPLY", "FLOW"))
    )
    _, seen = await _deliver(
        monkeypatch,
        _responds(200, ACCEPTED_BODY),
        message=_message(id="m-42"),
        route=route,
    )
    buttons = [
        c
        for c in json.loads(seen["body"])["template"]["components"]
        if c["type"] == "button"
    ]
    assert [b["index"] for b in buttons] == ["0", "2"]
    assert {b["parameters"][0]["action"]["flow_token"] for b in buttons} == {"m-42"}


async def test_a_template_without_a_flow_button_posts_what_it_always_did(
    monkeypatch,
) -> None:
    """The overwhelming majority of sends name no flow. Their body must be
    byte-identical to before this feature, or every template on the platform
    is a new shape at once."""
    _, seen = await _deliver(monkeypatch, _responds(200, ACCEPTED_BODY))
    components = json.loads(seen["body"])["template"]["components"]
    assert [c["type"] for c in components] == ["body"]


def test_index_zero_is_a_position_not_an_absence() -> None:
    """The first button is index 0, which is falsy — a truthiness check
    anywhere on this path would silently drop the component for every
    template whose flow button comes first, and Meta would refuse those
    sends."""
    body = build_send_body("t", "en_US", "919876543210", [], flow_button_indexes=[0])
    assert body["template"]["components"][0]["index"] == "0"


def test_a_send_naming_no_token_sends_no_placeholder() -> None:
    """Naming the button is enough for Meta; the token is the one optional
    part. A caller without one sends nothing rather than a stand-in — Meta
    then records its own word, 'unused', which the read side discards. A
    placeholder of ours would instead become a real-looking join key that
    every tokenless send shares."""
    body = build_send_body("t", "en_US", "919876543210", [], flow_button_indexes=[1])
    button = body["template"]["components"][0]
    assert button == {"type": "button", "sub_type": "flow", "index": "1"}


# --- reading the flow positions off the registered components -----------------
#
# The walk lives in THIS face, not the registry decoder: BUTTONS and FLOW are
# Meta's component vocabulary, and the route carries the row whole so each
# adapter reads its own words out of it (the #1050 rule, both directions).


def test_the_flow_positions_are_read_off_the_registered_components() -> None:
    """Meta refuses the whole send (131009) when a FLOW button arrives
    unnamed, and the component names it by POSITION — the one send-time
    fact inside the registered structure."""
    positions = flow_button_indexes(_buttons("QUICK_REPLY", "QUICK_REPLY", "FLOW"))
    assert positions == [2]


def test_every_flow_button_is_found_not_only_the_first() -> None:
    """Whether Meta caps a template at one flow button is Meta's rule to
    change. Finding them all needs no such rule to hold: a second button
    left unnamed would have its whole send refused, and nobody receives a
    message because of a cap we assumed."""
    assert flow_button_indexes(_buttons("FLOW", "QUICK_REPLY", "FLOW")) == [0, 2]


def test_a_template_with_no_flow_button_says_so_rather_than_guessing() -> None:
    """Empty means "post no button component", which is what every template
    on the platform needs today. A wrong position here would break sends
    that work; an empty list cannot."""
    assert flow_button_indexes(_buttons("QUICK_REPLY")) == []
    assert flow_button_indexes([{"type": "BODY", "text": "no buttons"}]) == []


def test_a_malformed_component_answers_empty_rather_than_raising() -> None:
    """The row's junk is filtered at the decoder, but this walk runs per
    message inside a claimed batch and keeps the same totality anyway —
    a BUTTONS component with no buttons list must not strand the send."""
    assert flow_button_indexes([]) == []
    assert flow_button_indexes([{"type": "BUTTONS"}]) == []
    assert flow_button_indexes([{"type": "BUTTONS", "buttons": "nope"}]) == []
    assert flow_button_indexes(
        [{"type": "BUTTONS", "buttons": [{"type": "FLOW"}]}]
    ) == [0], "index 0 is a position, not an absence"


def test_the_key_the_adapter_stamps_is_the_key_the_extractor_strips() -> None:
    """The wire key is spelled in two modules (rule 12 forbids the import
    either way); only a test may hold both. A drift means the stamped key
    comes back unrecognised and our uuid sits beside her address in a
    merchant's order note."""
    from app.crm.connectivity.providers.whatsapp import payload as sender
    from app.crm.record.extractors.whatsapp import flow as reader

    assert sender.FLOW_TOKEN_KEY == reader.FLOW_TOKEN_KEY


def test_a_second_buttons_component_never_restarts_the_count() -> None:
    """Meta registers ONE buttons component; positions in a hypothetical
    second would restart at 0 and name the wrong button. The first wins
    and the walk stops."""
    two_components = [
        {"type": "BUTTONS", "buttons": [{"type": "FLOW", "text": "a"}]},
        {"type": "BUTTONS", "buttons": [{"type": "FLOW", "text": "b"}]},
    ]
    assert flow_button_indexes(two_components) == [0]


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
