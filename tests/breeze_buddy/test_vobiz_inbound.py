"""
Tests for inbound Vobiz calls on a number's own channels.

A Vobiz number now carries inbound calls the way a Plivo one does, and every
piece of that bargain can fail silently:

- the answer-time gate must take a channel on a Vobiz number (and refuse the
  caller busy when there is none), the call-end release must give exactly
  that one channel back, and the reconciler's in-flight count must see the
  same leads — three places that must name the same providers, or free
  capacity is miscounted;
- a Vobiz number resolving to 2+ inbound templates is refused loudly, because
  the keypad menu is not built for Vobiz — while a Plivo number keeps its menu;
- a policy REDIRECT on Vobiz must present the called number as caller ID
  (Vobiz India rejects any other with hangup 3030), while Plivo's refusal XML
  stays byte-for-byte what it was;
- the websocket side (agent/inbound.py, the IVR helpers) must treat a Vobiz
  stream as the Plivo-dialect mu-law stream it is.

Accessors, Redis, TTS and the pod router are patched: nothing here touches
Postgres, Redis or Vobiz.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List, Optional, cast
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import Response, WebSocket

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and only this order resolves it (the order the app itself loads them
# in). "...breeze_buddy" sorts before "...breeze_buddy.<sub>", so isort keeps
# this line above the next ones on its own.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.agent import inbound as ws_inbound
from app.ai.voice.agents.breeze_buddy.ivr import (
    selection as sel_mod,
    walker as walker_mod,
)
from app.ai.voice.agents.breeze_buddy.managers import (
    calls as calls_mod,
    inbound_channel as ic_mod,
)
from app.ai.voice.agents.breeze_buddy.services.inbound_policy import PolicyResult
from app.api.routers.breeze_buddy.telephony.answer import handlers as ans_mod
from app.core.logger import logger
from app.database.queries.breeze_buddy.dispatch import (
    count_processing_by_telephony_number_query,
)
from app.schemas import (
    CallDirection,
    CallProvider,
    ExecutionMode,
    InboundBlockAction,
    LeadCallStatus,
)
from app.schemas.breeze_buddy.core import (
    LeadCallTracker,
    TelephonyNumber,
    TelephonyNumberStatus,
)

CALLED = "918000000901"
CALLER = "919000000001"
REDIRECT_TO = "919000000777"
NOT_CONFIGURED = "Sorry, this number is not configured to receive calls. Goodbye."
BLOCK_MESSAGE = "Hold on & we will connect you"

# What release (f46d7449) answered a blocked Plivo caller with — literal bytes,
# so any drift in Plivo's refusal XML fails here.
PLIVO_REDIRECT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Response>\n"
    "    <Speak>Hold on &amp; we will connect you</Speak>\n"
    "    <Dial>\n"
    "        <Number>919000000777</Number>\n"
    "    </Dial>\n"
    "</Response>"
)
PLIVO_REJECT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Response>\n"
    "    <Speak>Hold on &amp; we will connect you</Speak>\n"
    "    <Hangup/>\n"
    "</Response>"
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_inbound_lead(
    status: LeadCallStatus = LeadCallStatus.PROCESSING,
    outcome: Optional[str] = None,
) -> LeadCallTracker:
    return LeadCallTracker(
        id="lead-in-1",
        telephony_number_id="num-1",
        reseller_id="res-1",
        template="support",
        merchant_id="merchant-1",
        status=status,
        outcome=outcome,
        call_id="CALL-1",
        call_direction=CallDirection.INBOUND,
        execution_mode=ExecutionMode.TELEPHONY,
    )


def make_number(provider: CallProvider = CallProvider.VOBIZ) -> TelephonyNumber:
    return TelephonyNumber(
        id="num-1",
        number=CALLED,
        provider=provider,
        status=TelephonyNumberStatus.AVAILABLE,
        channels=1,
        maximum_channels=5,
    )


def make_template(n: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"tmpl-{n}",
        name=f"support-{n}",
        reseller_id="res-1",
        merchant_id="merchant-1",
        telephony_number_id="num-1",
        configurations=None,
    )


def xml_of(response: Response) -> ET.Element:
    return ET.fromstring(bytes(response.body))


def stream_query(response: Response) -> dict:
    """The query of the websocket URL inside the answer XML's <Stream>."""
    stream = xml_of(response).find("Stream")
    assert stream is not None, f"no <Stream> in {bytes(response.body)!r}"
    url = urlparse((stream.text or "").strip())
    assert url.path.endswith("/callback/ws/v2"), url.path
    return {k: v[0] for k, v in parse_qs(url.query).items()}


class _Request:
    """The form-POST the provider sends to /{provider}/answer."""

    method = "POST"

    def __init__(self, to_number: str) -> None:
        self._data = {"CallUUID": "CALL-1", "From": CALLER, "To": to_number}

    async def form(self) -> dict:
        return self._data

    @property
    def query_params(self) -> dict:
        return {}


class AnswerRig:
    """Drives ``_handle_provider_answer`` for one inbound call and records
    every side effect that matters: a channel taken, a lead created, a keypad
    menu prepared, a refusal logged."""

    def __init__(
        self,
        provider: CallProvider,
        templates: int = 1,
        admits: bool = True,
        policy: Optional[PolicyResult] = None,
    ) -> None:
        self.number = make_number(provider)
        self.templates = [make_template(n) for n in range(1, templates + 1)]
        self.admits = admits
        self.policy = policy
        self.channel_taken_on: List[str] = []
        self.leads_for: List[str] = []
        self.menus_for: List[str] = []
        self.blocked_logged: List[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "AnswerRig":
        async def resolve(call_sid, from_number, to_number):
            return {
                "is_outbound": False,
                "templates": list(self.templates),
                "template_list": [{"id": t.id, "name": t.name} for t in self.templates],
                "reseller_id": "res-1",
                "telephony_number": self.number,
                "ivr_greeting": "Press 1 for support, 2 for sales.",
                "ivr_goodbye": None,
                "ivr_voice_config": None,
            }

        async def config(_template_id):
            return object() if self.policy else None

        async def policy(_config, _from_number, skip_rate_limit=False):
            return self.policy

        async def log_blocked(**kwargs):
            self.blocked_logged.append(kwargs["call_id"])

        async def increment(number_id):
            self.channel_taken_on.append(number_id)
            return self.number if self.admits else None

        async def create_lead(call_id, from_number, templates):
            self.leads_for.append(call_id)
            return True

        async def no_pod(**kwargs):
            return None

        async def menu_audio(provider, *args, **kwargs):
            self.menus_for.append(provider)
            return b"\xff" * 160

        async def noise_off():
            return False

        async def get_redis():
            async def setex(key, value, ttl):
                return True

            return SimpleNamespace(setex=setex)

        def spawn(coro, **kwargs):
            coro.close()

        monkeypatch.setattr(ans_mod, "resolve_call_templates", resolve)
        monkeypatch.setattr(ans_mod, "get_call_execution_config_by_template_id", config)
        monkeypatch.setattr(ans_mod, "check_inbound_policy", policy)
        monkeypatch.setattr(ans_mod, "log_blocked_call", log_blocked)
        monkeypatch.setattr(ans_mod, "spawn_background_task", spawn)
        monkeypatch.setattr(
            ans_mod, "_create_inbound_lead_in_answer_handler", create_lead
        )
        monkeypatch.setattr(ans_mod, "safe_allocate_pod", no_pod)
        monkeypatch.setattr(ans_mod, "prepare_ivr_menu_audio", menu_audio)
        monkeypatch.setattr(ans_mod, "prepare_goodbye_audio", menu_audio)
        monkeypatch.setattr(ans_mod, "get_redis_service", get_redis)
        monkeypatch.setattr(ans_mod, "BB_NOISE_CANCELLATION_ENABLED", noise_off)
        monkeypatch.setattr(ans_mod, "BB_NOISE_CANCELLATION_LEVEL", noise_off)
        # The gate's own module: admit_inbound_call stays real.
        monkeypatch.setattr(ic_mod, "increment_telephony_number_channels", increment)
        return self

    async def answer(self, path: str, to_number: str = CALLED) -> Response:
        response = await ans_mod._handle_provider_answer(
            cast(Any, _Request(to_number)), path
        )
        await asyncio.sleep(0)  # let the fire-and-forget blocked-call log run
        return response


class ReleaseSpy:
    """Records what ``_release_call_resources`` gave back for an inbound lead."""

    def __init__(self, number: TelephonyNumber) -> None:
        self.number = number
        self.decremented: List[str] = []
        self.tokens: List[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "ReleaseSpy":
        async def get_number(_number_id):
            return self.number

        async def decrement(number_id):
            self.decremented.append(number_id)

        async def release_token(number_id, token=None):
            self.tokens.append(number_id)
            return True

        # Patch the collaborators, not release_inbound_channel itself, so the
        # inbound_holds_channel predicate stays in the loop.
        monkeypatch.setattr(ic_mod, "get_telephony_number_by_id", get_number)
        monkeypatch.setattr(ic_mod, "decrement_telephony_number_channels", decrement)
        monkeypatch.setattr(calls_mod, "release_channel_token", release_token)
        return self


@pytest.fixture
def error_log():
    """ERROR lines logged while the test runs (loguru, so not caplog)."""
    lines: List[str] = []
    sink = logger.add(lines.append, level="ERROR", format="{message}")
    yield lines
    logger.remove(sink)


class _Socket:
    """A telephony websocket that replays frames and carries URL params."""

    def __init__(self, *frames: dict, query_params: Optional[dict] = None) -> None:
        self._frames = [json.dumps(f) for f in frames]
        self.query_params = query_params or {}

    async def iter_text(self):
        for frame in self._frames:
            yield frame


def make_walker(provider: Any, ws: Optional[_Socket] = None) -> walker_mod.IvrWalker:
    """An IvrWalker with only the fields the audio/keypad helpers read."""
    walker = object.__new__(walker_mod.IvrWalker)
    walker.provider = provider
    walker.stream_sid = "stream-1"
    walker.ws = cast(WebSocket, ws or _Socket())
    return walker


# ---------------------------------------------------------------------------
# Which calls hold a channel: gate, release and reconciler agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,number_provider,gated",
    [
        ("vobiz", CallProvider.VOBIZ, True),
        ("plivo", CallProvider.PLIVO, True),
        # A number answered on another provider's path is never gated: the
        # path and the stored provider must agree.
        ("vobiz", CallProvider.PLIVO, False),
        ("plivo", CallProvider.VOBIZ, False),
        ("exotel", CallProvider.VOBIZ, False),
        ("exotel", CallProvider.EXOTEL, False),
        ("twilio", CallProvider.TWILIO, False),
    ],
)
def test_only_plivo_and_vobiz_numbers_on_their_own_path_take_a_channel(
    path, number_provider, gated
):
    number = make_number(number_provider)
    got = ans_mod._gated_inbound_number(path, {"telephony_number": number})
    assert (got is number) is gated


def test_a_call_with_no_resolved_number_takes_no_channel():
    assert ans_mod._gated_inbound_number("vobiz", {}) is None


@pytest.mark.parametrize(
    "provider,status,owed",
    [
        (CallProvider.VOBIZ, LeadCallStatus.PROCESSING, True),
        (CallProvider.VOBIZ, LeadCallStatus.FINISHED, False),
        (CallProvider.VOBIZ, LeadCallStatus.BACKLOG, False),
        (CallProvider.PLIVO, LeadCallStatus.PROCESSING, True),
        (CallProvider.EXOTEL, LeadCallStatus.PROCESSING, False),
        (CallProvider.TWILIO, LeadCallStatus.PROCESSING, False),
    ],
)
def test_a_vobiz_inbound_lead_owes_its_channel_only_while_processing(
    provider, status, owed
):
    lead = make_inbound_lead(status=status)
    assert ic_mod.inbound_holds_channel(lead, provider) is owed
    # The call-end callback's predicate delegates to the same rule.
    assert calls_mod._releases_capacity(lead, provider) is owed


def test_gate_release_and_reconciler_name_the_same_providers():
    """The reconciler mints Redis tokens for M - in_flight channels. If its
    count names a provider the gate does not (or misses one it does), free
    capacity is miscounted on every number of that provider — silently."""
    gated = {p.value for p in ans_mod._GATED_INBOUND_PROVIDERS.values()}
    released = {
        p.value
        for p in CallProvider
        if ic_mod.inbound_holds_channel(make_inbound_lead(), p)
    }
    text, _ = count_processing_by_telephony_number_query()
    inbound = re.search(
        r"""l\."call_direction" = 'INBOUND'\s+AND n\."provider" IN \(([^)]*)\)""",
        text,
    )
    assert inbound is not None, f"no inbound provider list in:\n{text}"
    counted = set(re.findall(r"'([A-Z]+)'", inbound.group(1)))

    assert gated == released == counted == {"PLIVO", "VOBIZ"}


# ---------------------------------------------------------------------------
# Admission at the real answer call site
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row,admitted", [(make_number(), True), (None, False)])
async def test_admission_is_granted_only_when_the_increment_returns_a_row(
    monkeypatch, row, admitted
):
    """The accessor collapses 'at capacity' and 'UPDATE failed' into None, so
    both refuse the caller: admission fails closed."""

    async def increment(_number_id):
        return row

    monkeypatch.setattr(ic_mod, "increment_telephony_number_channels", increment)
    assert await ic_mod.admit_inbound_call("num-1") is admitted


async def test_a_vobiz_inbound_call_takes_a_channel_and_streams_to_its_template(
    monkeypatch,
):
    rig = AnswerRig(CallProvider.VOBIZ).install(monkeypatch)

    response = await rig.answer("vobiz")

    assert rig.channel_taken_on == ["num-1"], "the Vobiz call was not gated"
    assert rig.leads_for == ["CALL-1"]
    query = stream_query(response)
    assert query["template_id"] == "tmpl-1"
    assert query["to_number"] == CALLED
    assert "ivr_mode" not in query


async def test_a_full_vobiz_number_turns_the_caller_away_busy(monkeypatch):
    """No free channel: the caller hears the busy message and hangs up, and no
    PROCESSING lead is created that would later 'return' a channel never
    taken. Speak + Hangup is Vobiz XML: https://www.vobiz.ai/docs/xml/response
    """
    rig = AnswerRig(CallProvider.VOBIZ, admits=False).install(monkeypatch)

    root = xml_of(await rig.answer("vobiz"))

    assert rig.channel_taken_on == ["num-1"]
    assert [child.tag for child in root] == ["Speak", "Hangup"]
    assert root.findtext("Speak") == ans_mod._INBOUND_CAPACITY_MESSAGE
    assert rig.leads_for == []


# ---------------------------------------------------------------------------
# Release: one channel back per admitted call, never more
# ---------------------------------------------------------------------------


async def test_an_admitted_vobiz_call_returns_exactly_one_channel(monkeypatch):
    """Inbound never borrowed a dispatch token, so none goes back either."""
    spy = ReleaseSpy(make_number(CallProvider.VOBIZ)).install(monkeypatch)

    await calls_mod._release_call_resources(make_inbound_lead())

    assert spy.decremented == ["num-1"]
    assert spy.tokens == []


async def test_a_duplicate_vobiz_status_callback_releases_nothing_more(monkeypatch):
    """The second webhook sees the FINISHED row the first one wrote."""
    spy = ReleaseSpy(make_number(CallProvider.VOBIZ)).install(monkeypatch)

    await calls_mod._release_call_resources(
        make_inbound_lead(LeadCallStatus.PROCESSING)
    )
    await calls_mod._release_call_resources(make_inbound_lead(LeadCallStatus.FINISHED))

    assert spy.decremented == ["num-1"]


async def test_a_vobiz_call_refused_at_the_gate_never_releases(monkeypatch):
    """A capacity-rejected call wrote a FINISHED row and never held a channel;
    releasing for it would invent capacity the trunk does not have."""
    spy = ReleaseSpy(make_number(CallProvider.VOBIZ)).install(monkeypatch)
    lead = make_inbound_lead(
        status=LeadCallStatus.FINISHED, outcome=ans_mod.CAPACITY_REJECTED_OUTCOME
    )

    await calls_mod._release_call_resources(lead)

    assert spy.decremented == []


# ---------------------------------------------------------------------------
# D1: one inbound template per Vobiz number (no keypad menu on Vobiz yet)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("templates", [2, 3])
async def test_a_vobiz_number_with_several_inbound_templates_is_refused_loudly(
    monkeypatch, error_log, templates
):
    rig = AnswerRig(CallProvider.VOBIZ, templates=templates).install(monkeypatch)

    root = xml_of(await rig.answer("vobiz"))

    assert [child.tag for child in root] == ["Speak", "Hangup"]
    assert root.findtext("Speak") == NOT_CONFIGURED
    # Refused before the gate: no channel taken, no lead, no menu.
    assert rig.channel_taken_on == []
    assert rig.leads_for == []
    assert rig.menus_for == []
    refusals = [line for line in error_log if f"Vobiz number {CALLED}" in line]
    assert len(refusals) == 1, error_log
    assert f"{templates} inbound templates" in refusals[0]


async def test_a_plivo_number_with_several_templates_still_plays_the_keypad_menu(
    monkeypatch, error_log
):
    rig = AnswerRig(CallProvider.PLIVO, templates=2).install(monkeypatch)

    response = await rig.answer("plivo")

    assert stream_query(response)["ivr_mode"] == "true"
    assert rig.menus_for == ["plivo", "plivo"]  # menu + goodbye audio
    assert rig.channel_taken_on == ["num-1"]
    assert not [line for line in error_log if "not supported on Vobiz" in line]


# ---------------------------------------------------------------------------
# Refusal XML: Vobiz redirect presents the called number; Plivo unchanged
# ---------------------------------------------------------------------------


async def test_a_vobiz_policy_redirect_dials_out_as_the_called_number(monkeypatch):
    """Without callerId Vobiz derives the B-leg caller ID from the A-leg — the
    caller's own mobile — and India rejects a non-Vobiz caller ID with hangup
    3030 "Unknown Caller ID". So the refusal must present the called Vobiz
    number, escaped as an XML attribute.
    https://www.vobiz.ai/docs/xml/dial ("If omitted, Vobiz derives the value
    from the A-leg") and https://www.vobiz.ai/docs/concepts/hangup-causes (3030).
    """
    called = '918000000901&"x'
    policy = PolicyResult(
        allowed=False,
        action=InboundBlockAction.REDIRECT,
        message=BLOCK_MESSAGE,
        redirect_number=REDIRECT_TO,
        reason="outside_business_hours",
    )
    rig = AnswerRig(CallProvider.VOBIZ, policy=policy).install(monkeypatch)

    response = await rig.answer("vobiz", to_number=called)
    body = bytes(response.body).decode()
    dial = xml_of(response).find("Dial")

    assert dial is not None, body
    assert dial.get("callerId") == called
    assert dial.findtext("Number") == REDIRECT_TO
    assert 'callerId="918000000901&amp;&quot;x"' in body
    # Everything but the Dial tag is Plivo's redirect, byte for byte.
    assert (
        body.replace('<Dial callerId="918000000901&amp;&quot;x">', "<Dial>")
        == PLIVO_REDIRECT_XML
    )
    # A policy refusal takes no channel, and is logged as blocked.
    assert rig.channel_taken_on == []
    assert rig.blocked_logged == ["CALL-1"]


def test_a_vobiz_reject_carries_no_dial_even_with_a_caller_id():
    response = ans_mod._build_block_response(
        "vobiz", BLOCK_MESSAGE, InboundBlockAction.REJECT, None, caller_id=CALLED
    )
    assert bytes(response.body).decode() == PLIVO_REJECT_XML


@pytest.mark.parametrize("caller_id", [None, CALLED])
@pytest.mark.parametrize(
    "action,redirect,expected",
    [
        (InboundBlockAction.REDIRECT, REDIRECT_TO, PLIVO_REDIRECT_XML),
        (InboundBlockAction.REJECT, None, PLIVO_REJECT_XML),
    ],
)
async def test_plivo_refusal_xml_is_byte_identical_to_release(
    action, redirect, expected, caller_id
):
    """The refusal call site now passes the called number for every
    Plivo-dialect provider; Plivo's XML must not change because of it."""
    direct = ans_mod._build_block_response(
        "plivo", BLOCK_MESSAGE, action, redirect, caller_id=caller_id
    )
    assert bytes(direct.body).decode() == expected

    via_refusal = await ans_mod._refuse_inbound_call(
        provider="plivo",
        call_id="CALL-1",
        from_number=CALLER,
        to_number=CALLED,
        templates=[make_template(1)],
        block_message=BLOCK_MESSAGE,
        block_action=action,
        block_redirect=redirect,
        reseller_id="res-1",
        merchant_id="merchant-1",
        tag="test",
    )
    assert bytes(via_refusal.body).decode() == expected


# ---------------------------------------------------------------------------
# The websocket side: agent/inbound.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider,accepted",
    [
        (CallProvider.VOBIZ, True),
        (CallProvider.PLIVO, True),
        (CallProvider.EXOTEL, True),
        (CallProvider.TWILIO, False),
    ],
)
async def test_the_websocket_accepts_inbound_calls_from_each_inbound_provider(
    monkeypatch, provider, accepted
):
    existing = make_inbound_lead()

    async def lead_from_answer_handler(_call_sid):
        return existing

    monkeypatch.setattr(ws_inbound, "get_lead_by_call_id", lead_from_answer_handler)

    lead, error = await ws_inbound.handle_inbound_call(
        call_sid="CALL-1",
        call_data={},
        call_initiated_time=datetime.now(timezone.utc),
        provider=provider,
    )

    if accepted:
        assert (lead, error) == (existing, None)
    else:
        assert (lead, error) == (None, "Inbound calls not supported")


async def test_a_vobiz_websocket_lead_is_made_from_the_url_and_owns_the_channel(
    monkeypatch,
):
    """A Vobiz start event carries no to/from, so the numbers come off the
    stream URL; the lead it creates is the INBOUND PROCESSING row on the
    called number that returns the channel at call end."""
    looked_up: List[str] = []
    created: dict = {}

    async def no_lead(_call_sid):
        return None

    async def number_by_number(number):
        looked_up.append(number)
        return make_number(CallProvider.VOBIZ)

    async def inbound_template(_number_id, enable_inbound_only=False):
        return make_template(1)

    async def create(**kwargs):
        created.update(kwargs)
        return make_inbound_lead()

    monkeypatch.setattr(ws_inbound, "get_lead_by_call_id", no_lead)
    monkeypatch.setattr(ws_inbound, "get_telephony_number_by_number", number_by_number)
    monkeypatch.setattr(
        ws_inbound, "get_template_by_telephony_number_id", inbound_template
    )
    monkeypatch.setattr(ws_inbound, "create_lead_call_tracker", create)

    lead, error = await ws_inbound.handle_inbound_call(
        call_sid="CALL-1",
        call_data={"stream_id": "stream-1", "call_id": "CALL-1"},
        call_initiated_time=datetime.now(timezone.utc),
        provider=CallProvider.VOBIZ,
        url_query_params={"to_number": CALLED, "from_number": CALLER},
    )

    assert lead is not None and error is None
    assert looked_up == [CALLED]
    assert created["call_direction"] == CallDirection.INBOUND
    assert created["status"] == LeadCallStatus.PROCESSING
    assert created["telephony_number_id"] == "num-1"
    assert created["payload"] == {"customer_mobile_number": CALLER}
    assert calls_mod._releases_capacity(lead, CallProvider.VOBIZ) is True


@pytest.mark.parametrize(
    "provider,from_url",
    [
        ("vobiz", True),
        (CallProvider.VOBIZ, True),  # what the agent actually passes
        ("plivo", True),
        ("twilio", False),
    ],
)
async def test_a_vobiz_stream_reads_its_template_from_the_websocket_url(
    provider, from_url
):
    template_id = "0b6f7a3e-5d1c-4c2e-9a51-3f0d8e2b7c11"
    ws = _Socket(query_params={"template_id": template_id})

    got, error, was_ivr = await sel_mod.get_template_id_from_call(
        ws=cast(WebSocket, ws),
        stream_sid="stream-1",
        call_sid="CALL-1",
        call_data={},
        provider=provider,
    )

    assert (got, error, was_ivr) == ((template_id if from_url else None), None, False)


# ---------------------------------------------------------------------------
# IVR audio: a Vobiz stream is the Plivo-dialect mu-law stream
# ---------------------------------------------------------------------------

MULAW = b"\xff" * 160


@pytest.mark.parametrize(
    "provider,stays_mu_law",
    [
        ("vobiz", True),
        (CallProvider.VOBIZ, True),
        ("plivo", True),
        ("twilio", True),
        ("exotel", False),  # PCM16: two bytes a sample
    ],
)
def test_vobiz_prompts_stay_mu_law(provider, stays_mu_law):
    """The stream is audio/x-mulaw;rate=8000 (https://www.vobiz.ai/docs/xml/stream)."""
    audio = sel_mod._convert_audio_for_provider(MULAW, provider)
    assert (audio == MULAW) is stays_mu_law
    assert len(audio) == len(MULAW) * (1 if stays_mu_law else 2)


@pytest.mark.parametrize(
    "provider,expected",
    [
        (
            "vobiz",
            {
                "event": "playAudio",
                "streamId": "stream-1",
                "media": {
                    "contentType": "audio/x-mulaw",
                    "sampleRate": 8000,
                    "payload": base64.b64encode(MULAW).decode(),
                },
            },
        ),
        (
            "plivo",
            {
                "event": "playAudio",
                "streamId": "stream-1",
                "media": {
                    "contentType": "audio/x-mulaw",
                    "sampleRate": 8000,
                    "payload": base64.b64encode(MULAW).decode(),
                },
            },
        ),
        (
            "twilio",
            {
                "event": "media",
                "streamSid": "stream-1",
                "media": {"payload": base64.b64encode(MULAW).decode()},
            },
        ),
        (
            "exotel",
            {
                "event": "media",
                "streamSid": "stream-1",
                "media": {"payload": base64.b64encode(MULAW).decode()},
            },
        ),
    ],
)
async def test_vobiz_prompts_go_down_the_stream_as_play_audio(
    monkeypatch, provider, expected
):
    """Vobiz plays audio sent as a playAudio event (mu-law, 8 kHz);
    https://www.vobiz.ai/docs/xml/stream/stream-events"""
    sent: List[dict] = []

    async def send(ws, message):
        sent.append(message)
        return True

    monkeypatch.setattr(sel_mod, "send_message", send)
    await sel_mod._send_audio(cast(WebSocket, _Socket()), "stream-1", MULAW, provider)

    assert sent == [expected]


@pytest.mark.parametrize(
    "provider,seconds",
    [
        ("vobiz", 1.0),
        (CallProvider.VOBIZ, 1.0),
        ("plivo", 1.0),
        ("twilio", 1.0),
        ("exotel", 0.5),
    ],
)
def test_a_vobiz_prompt_is_timed_as_mu_law(provider, seconds):
    """8000 bytes is one second of 8 kHz mu-law; timing it as PCM16 would
    halve the wait and cut the prompt off."""
    walker = make_walker(provider)
    assert walker._audio_duration_secs(b"\xff" * 8000) == seconds


@pytest.mark.parametrize(
    "provider,clears",
    [("vobiz", True), (CallProvider.VOBIZ, True), ("plivo", True), ("exotel", False)],
)
async def test_a_keypress_on_a_vobiz_stream_cuts_the_prompt_short(
    monkeypatch, provider, clears
):
    """Barge-in on a Plivo-dialect stream is a clearAudio event."""
    sent: List[dict] = []

    async def send(ws, message):
        sent.append(message)
        return True

    monkeypatch.setattr(walker_mod, "send_message", send)
    walker = make_walker(provider, _Socket({"event": "dtmf", "dtmf": {"digit": "1"}}))

    assert await walker._wait_for_digit() == "1"
    expected = [{"event": "clearAudio", "streamId": "stream-1"}] if clears else []
    assert sent == expected


@pytest.mark.parametrize(
    "provider,clears",
    [("vobiz", True), ("plivo", True), ("exotel", False)],
)
async def test_a_menu_choice_on_a_vobiz_stream_cuts_the_menu_short(
    monkeypatch, provider, clears
):
    sent: List[dict] = []

    async def send(ws, message):
        sent.append(message)
        return True

    monkeypatch.setattr(sel_mod, "send_message", send)
    ws = _Socket({"event": "dtmf", "dtmf": {"digit": "2"}})
    options: List[dict] = [{"id": "tmpl-1", "name": "a"}, {"id": "tmpl-2", "name": "b"}]

    chosen = await sel_mod._wait_for_valid_dtmf(
        cast(WebSocket, ws), cast(Any, options), "stream-1", provider
    )

    assert chosen == "tmpl-2"
    expected = [{"event": "clearAudio", "streamId": "stream-1"}] if clears else []
    assert sent == expected
