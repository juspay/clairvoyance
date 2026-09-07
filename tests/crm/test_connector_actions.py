"""The fourth verb (modules/04-connectivity): a run asks a connector to DO
something. The generic root resolves the door, finds the action, types its
args and performs; every defect is an ActionError and every bad moment is
anything else, because the walker parks on the first and retries the second.

The Shopify face is the one connector that acts today, and its transport is
nautilus's existing order_action relay — chosen by a NULL credential_id, so
the migration to a direct Shopify client is per-shop and changes no plan."""

import json
from typing import Any, ClassVar, Dict, List, Optional, Type, cast
from unittest.mock import patch

import httpx
import pytest
from pydantic import BaseModel, ValidationError

import app.crm.connectivity.providers.shopify.actions as shopify_actions
import app.crm.connectivity.providers.shopify.via_nautilus as via_nautilus
from app.core.security.sha import calculate_hmac_sha256
from app.crm.connectivity import actions as actions_module
from app.crm.connectivity.actions import (
    action_names,
    perform_action,
    validate_action_args,
)
from app.crm.connectivity.connectors import CONNECTORS
from app.crm.connectivity.providers.base import ActionError, ConnectorAction
from app.crm.connectivity.providers.shopify.actions import (
    SHOPIFY_ACTIONS,
    AddNoteArgs,
    AddTagArgs,
    UpdateOrderArgs,
    _run_ref,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from tests.crm.doubles import stub_http

SECRET = "shhh"
RELAY = "https://nautilus.example/apps/breeze-buddy/webhooks/clairvoyance"


def _installation(credential_id: Optional[str] = None) -> ConnectorInstallation:
    return ConnectorInstallation(
        id="inst-1",
        merchant_id="m1",
        connector_key="shopify",
        external_account_id="acme.myshopify.com",
        display_label="Acme",
        credential_id=credential_id,
        status="healthy",
    )


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment's one relay address and the shared signing key."""
    monkeypatch.setattr(via_nautilus, "NAUTILUS_WEBHOOK_URL", RELAY)
    monkeypatch.setattr(via_nautilus, "ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY", SECRET)


def _door(monkeypatch: pytest.MonkeyPatch, installation: Any) -> None:
    async def _get(merchant_id: str, connector_key: str) -> Any:
        return installation

    monkeypatch.setattr(actions_module, "get_installation_for_connector", _get)


# --- the registry's read -------------------------------------------------------


def test_action_names_lists_what_a_connector_can_do() -> None:
    assert action_names("shopify") == ["add_note", "add_tag", "update_order"]
    # A connector that only sends, and one that does not exist, answer the
    # same way: [] — the caller writes "no such connector" in its own words.
    assert action_names("whatsapp") == []
    assert action_names("nope") == []


def test_validate_action_args_names_the_fields_that_do_not_fit() -> None:
    assert (
        validate_action_args("shopify", "add_tag", {"order_id": "1", "tags": ["a"]})
        == []
    )
    assert validate_action_args("shopify", "add_tag", {"tags": []}) == [
        "order_id",
        "tags",
    ]
    # An unknown action is the caller's sentence, not an argument problem.
    assert validate_action_args("shopify", "nope", {}) == []


# --- the four steps, and their refusals ----------------------------------------


async def test_an_unknown_connector_is_a_defect() -> None:
    with pytest.raises(ActionError, match="no connector 'zendesk'"):
        await perform_action("m1", "zendesk", "add_tag", {}, {})


async def test_an_unknown_action_names_the_alternatives() -> None:
    with pytest.raises(ActionError, match=r"has: add_note, add_tag"):
        await perform_action("m1", "shopify", "add_tags", {}, {})


async def test_a_connector_that_needs_a_door_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DEFAULT, pinned on a face that does not opt out.

    A missing, unproven or withdrawn door all read the same, and every
    connector added later inherits this without editing actions.py. Shopify
    is the single exception (see below) and must not be allowed to become
    the rule by accident.
    """

    class _NeedsADoor:
        """A face that declares nothing — so it inherits the closed default."""

        args_model: ClassVar[Type[BaseModel]] = AddTagArgs

        async def perform(
            self,
            merchant_id: str,
            installation: Optional[ConnectorInstallation],
            args: BaseModel,
            context: Dict[str, Any],
        ) -> Dict[str, Any]:
            raise AssertionError("must never be reached without a door")

    spec = CONNECTORS["shopify"]
    # cast: the Protocol is structural, but pyrefly will not infer a local
    # class as satisfying it through setitem's invariant value type.
    monkeypatch.setitem(
        spec.actions, "needs_door", cast(ConnectorAction, _NeedsADoor())
    )
    _door(monkeypatch, None)

    with pytest.raises(ActionError, match="no usable 'shopify' connection"):
        await perform_action(
            "m1",
            "shopify",
            "needs_door",
            {"order_id": "1", "tags": ["a"]},
            {"run_id": "r-1", "node_id": "n-1"},
        )


# --- the door is the tenancy ---------------------------------------------------


async def test_shopify_refuses_without_a_door(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    """No installation, no action — and no flag that excuses it.

    These faces once declared ``needs_installation = False``, on the argument
    that while nautilus carries every shop the merchant id IS the domain and
    the row constrains nothing. That is a convention between two services,
    not a guarantee, and the flag was a bypass on the one check the whole
    verb rests on. The package's own onboarder records the shop (with a NULL
    credential, as the migration switch), so there is a door to require.
    """
    _door(monkeypatch, None)
    seen = stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(200, json={"ok": True})
    )

    with pytest.raises(ActionError, match="no usable 'shopify' connection"):
        await perform_action(
            "acme.myshopify.com",
            "shopify",
            "add_tag",
            {"order_id": "1", "tags": ["CONFIRM"]},
            {"run_id": "r-1", "node_id": "tag-1"},
        )

    # Refused BEFORE anything left the process: a door check that still spent
    # a request would be a log line, not a guard.
    assert seen == []


async def test_the_door_names_the_shop_not_the_tenant(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    """The account id is the installation's, and only the installation's.

    A shop with its own door must be addressed by its own account id, or an
    OAuth install would be silently ignored in favour of a tenant id that is
    not a domain at all.
    """
    _door(monkeypatch, _installation())  # external_account_id = acme.myshopify.com
    seen = stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(200, json={"ok": True})
    )

    await perform_action(
        "some-other-tenant",
        "shopify",
        "add_tag",
        {"order_id": "1", "tags": ["a"]},
        {"run_id": "r-1", "node_id": "tag-1"},
    )

    assert json.loads(seen[0].content.decode())["merchant_id"] == "acme.myshopify.com"


async def test_args_that_do_not_fit_are_a_defect(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    _door(monkeypatch, _installation())
    with pytest.raises(ActionError, match=r"bad argument\(s\): order_id; tags"):
        await perform_action(
            "m1",
            "shopify",
            "add_tag",
            {},
            {"run_id": "r-1", "node_id": "n-1"},
        )


# --- the seam ------------------------------------------------------------------


async def test_a_shop_holding_its_own_credential_does_not_travel_by_relay(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    """The one fork that gets deleted when the tokens move."""
    _door(monkeypatch, _installation(credential_id="cred-1"))
    with pytest.raises(ActionError, match="direct transport is not built yet"):
        await perform_action(
            "m1",
            "shopify",
            "add_tag",
            {"order_id": "1", "tags": ["a"]},
            {"run_id": "r-1", "node_id": "n-1"},
        )


def test_the_run_ref_is_deterministic_and_splittable() -> None:
    assert _run_ref({"run_id": "r-1", "node_id": "tag-1"}) == "r-1:tag-1"


# --- the transport -------------------------------------------------------------


async def test_the_envelope_is_nautiluss_own_and_the_signature_covers_it(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    """The bytes we sign are the bytes we send, and the receiver verifies by
    re-serialising what it parsed — so key order and types are the contract."""
    _door(monkeypatch, _installation())
    seen = stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(200, json={"ok": True})
    )

    facts = await perform_action(
        "m1",
        "shopify",
        "add_tag",
        {"order_id": "5408422249", "tags": ["CONFIRM", "vip"]},
        {"run_id": "r-1", "node_id": "tag-1"},
    )

    assert facts == {"ok": True}
    request = seen[0]
    assert str(request.url) == RELAY
    body = request.content.decode()
    assert json.loads(body) == {
        "type": "order_action",
        # The SHOP DOMAIN, off the installation — never our tenant id, and
        # never something a plan could have said.
        "merchant_id": "acme.myshopify.com",
        "shopify_order_id": "5408422249",
        "add_shopify_tag": ["CONFIRM", "vip"],
        "add_shopify_note": None,
        "run_id": "r-1",
        "node_id": "tag-1",
    }
    # Compact, exactly as JSON.stringify writes it.
    assert ", " not in body and '": ' not in body
    assert request.headers["checksum"] == calculate_hmac_sha256(body, SECRET)
    assert request.headers["Idempotency-Key"] == "r-1:tag-1"


async def test_a_note_travels_as_the_same_envelope_with_both_keys(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    _door(monkeypatch, _installation())
    seen = stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(200, json={"ok": True})
    )
    await perform_action(
        "m1",
        "shopify",
        "add_note",
        {"order_id": "1", "note": "promised a refund"},
        {"run_id": "r-1", "node_id": "note-1"},
    )
    sent: Dict[str, Any] = json.loads(seen[0].content.decode())
    assert sent["add_shopify_tag"] == []
    assert sent["add_shopify_note"] == "promised a refund"


async def test_a_refusal_is_a_defect_and_a_bad_moment_is_not(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    """The whole reason the two are separate classes: the walker parks on the
    first and re-sends on the second."""
    _door(monkeypatch, _installation())
    args = {"order_id": "1", "tags": ["a"]}
    ref = {"run_id": "r-1", "node_id": "tag-1"}

    stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(ActionError, match="nautilus refused"):
        await perform_action("m1", "shopify", "add_tag", args, ref)

    stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(502, text="bad gateway")
    )
    with pytest.raises(RuntimeError) as caught:
        await perform_action("m1", "shopify", "add_tag", args, ref)
    assert not isinstance(caught.value, ActionError)


async def test_an_unconfigured_deployment_refuses_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail CLOSED and as a DEFECT: no retry configures a URL, and a run that
    silently did nothing would report success."""
    monkeypatch.setattr(via_nautilus, "NAUTILUS_WEBHOOK_URL", "")
    _door(monkeypatch, _installation())
    with pytest.raises(ActionError, match="not configured for this deployment"):
        await perform_action(
            "m1",
            "shopify",
            "add_tag",
            {"order_id": "1", "tags": ["a"]},
            {"run_id": "r-1", "node_id": "n-1"},
        )


async def test_an_unsigned_envelope_is_refused_here_not_by_nautilus(
    monkeypatch: pytest.MonkeyPatch, relay: None
) -> None:
    """The twin of the unset URL, and the same treatment for the same reason.

    Without a secret the envelope cannot be signed, and nautilus answers 401
    to an unsigned one — so posting anyway reaches the SAME parked run one
    hop later, carrying "refused (401)" where a legible sentence belongs,
    having spent a request to learn something knowable before it. Both are
    "this deployment is not configured to perform Shopify actions".
    """
    monkeypatch.setattr(via_nautilus, "ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY", "")
    _door(monkeypatch, _installation())
    seen = stub_http(
        monkeypatch, via_nautilus, lambda r: httpx.Response(200, json={"ok": True})
    )

    with pytest.raises(ActionError, match="not configured for this deployment"):
        await perform_action(
            "m1",
            "shopify",
            "add_tag",
            {"order_id": "1", "tags": ["a"]},
            {"run_id": "r-1", "node_id": "n-1"},
        )

    assert seen == []


def test_the_args_model_is_the_contract_and_not_the_wire_body() -> None:
    """A field added for a transport's convenience is a field every published
    plan would have to be republished to lose.

    order_id IS the contract, though — tagging an order means which order and
    which tags. It arrives as an ARG, resolved by the square from the plan's
    own `{placeholder}`, and not by this file reaching into a run's context
    for a key it guessed the name of."""
    assert set(AddTagArgs.model_fields) == {"order_id", "tags"}
    assert set(AddNoteArgs.model_fields) == {"order_id", "note"}
    assert set(UpdateOrderArgs.model_fields) == {"order_id", "tags", "note"}


def test_update_order_takes_a_tag_a_note_or_both() -> None:
    """The shape a merchant asks for — "tag it CONFIRMED and write why" —
    without a second square for the second half.

    Individually optional, together required: a step that would change
    nothing is an author's mistake, not a no-op to perform quietly. It would
    POST, succeed, and leave the order exactly as it was.
    """
    assert UpdateOrderArgs(order_id="1", tags=["CONFIRMED"]).tags == ["CONFIRMED"]
    assert UpdateOrderArgs(order_id="1", note="called").note == "called"
    both = UpdateOrderArgs(order_id="1", tags=["A"], note="called")
    assert both.tags == ["A"] and both.note == "called"

    with pytest.raises(ValidationError):
        UpdateOrderArgs(order_id="1")
    # whitespace is not a note
    with pytest.raises(ValidationError):
        UpdateOrderArgs(order_id="1", note="   ")


async def test_update_order_is_ONE_request_not_two() -> None:
    """The relay's envelope carries both fields together, so a tag and a note
    are one POST, one signature, one idempotency key and one retry. Two
    squares would be two of each — and a pair that can half-succeed, leaving
    an order tagged but not noted, which is a state no author asked for."""
    sent: List[Dict[str, Any]] = []

    class _Once:
        async def update_order(self, shop, order_id, tags, note, run_ref):
            sent.append(
                {
                    "shop": shop,
                    "order_id": order_id,
                    "tags": tags,
                    "note": note,
                    "run_ref": run_ref,
                }
            )
            return {"ok": True}

    action = SHOPIFY_ACTIONS["update_order"]
    with patch.object(shopify_actions, "_transport", lambda _installation: _Once()):
        facts = await action.perform(
            "shop.myshopify.com",
            _installation(),
            UpdateOrderArgs(order_id="7071", tags=["CONFIRMED"], note="called"),
            {"run_id": "r-1", "node_id": "tag-1"},
        )

    assert len(sent) == 1
    assert sent[0]["tags"] == ["CONFIRMED"] and sent[0]["note"] == "called"
    # the ACTION's own facts, not the carrier's body
    assert facts == {"ok": True, "tagged": ["CONFIRMED"], "noted": True}
