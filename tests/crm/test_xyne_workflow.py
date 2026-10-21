"""XyneSpaces flow_action through the one decode engine: the code-layer spec
attributes a form submission to the CUSTOMER who filled it.

The chain this pins: WE send the form (the ask-slots step), putting the run's
merchant and the run's customer's phone into context.flowJSON.data, so the
relay's submission carries both back. The merchant read (finding 1) is
connectivity/ingress.py's; what lives HERE is the customer decode. The identity
field `customer_mobile_number` (a phone) resolves() probes on, so the PM/Dev
who filled the form maps back to the same customer the run is about — and the
run's wait can wake (finding 3). A submission whose flowJSON.data carries no
phone has no identity and is quarantined (replayable), exactly as canon says a
customer-level letter with no handle must be.

The generic four-part square (fixtures exist, every field resolves, the engine
finds the person in every recorded letter, derivers match) is pinned for ALL
code entries by test_catalog.py; what lives here is xyne's own behaviour — the
customer-level decode, the declared variables riding out for templates, and
the one derived tool turning the date-named slot keys into a single readable
string a workflow can fill a template blank with.

Payloads are shaped exactly as filed: the relay's flow_action submission,
unmodified.
"""

import hashlib
import hmac
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.crm.connectivity.ingress as connectivity_ingress
import app.crm.outreach.entry as entry
import app.crm.outreach.nodes.action as action_node
import app.crm.outreach.walker as walker
import app.crm.record.api as record_api
import app.crm.record.consumers as record_consumers
import app.crm.record.extractors.xyne as xyne_spec
import app.crm.record.workers as workers
from app.crm.connectivity.providers.xyne import inbound
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.outreach.plans import Catalogs, validate_definition
from app.crm.outreach.schemas import EnrollmentRun, Workflow, WorkflowDefinition
from app.crm.record import catalog
from app.crm.record.db import DbTxn
from app.crm.record.extractors import EXTRACTORS, engine
from app.crm.record.ingress import INGRESS
from app.crm.record.schemas import RawEvent

FIXTURE = Path(__file__).parent / "fixtures" / "xyne" / "flow_action.json"

FLOW_SPEC = catalog.code_spec("xyne", "flow_action")


def _fixture() -> Dict[str, Any]:
    """The recorded relay submission, byte-identical to the fixture."""
    return json.loads(FIXTURE.read_text())


def _extract(payload: Dict[str, Any]):
    """The engine over xyne's flow_action spec — the worker's exact read."""
    assert FLOW_SPEC is not None
    return engine.extract(payload, FLOW_SPEC)


# --- the package's public surface is the registry, nothing else ----------------


def test_the_package_exports_the_spec_contract_and_nothing_else() -> None:
    """SOURCE · ENTRIES · DERIVERS are what record/catalog.py reads off a
    SPEC_MODULES entry — and ALL the package exports."""
    assert xyne_spec.__all__ == ["SOURCE", "ENTRIES", "DERIVERS"]


# --- the decode: customer-level, a phone handle, clean variables --------------


def test_flow_action_is_declared_about_a_customer() -> None:
    """The submission names the CUSTOMER who filled the form — the identity
    field is a phone we sent in flowJSON.data, so it resolves to the person
    the run is about (finding 3)."""
    spec = catalog.code_spec("xyne", "flow_action")
    assert spec is not None and spec.about == "customer"
    assert (
        "payload.context.flowJSON.data.customer_mobile_number" in spec.identity["phone"]
    )


def test_the_fixture_decodes_to_a_customer_and_a_phone_handle() -> None:
    """The engine over the recorded submit yields the phone handle it sent, so
    resolve() can find the same customer the run is for."""
    extracted = _extract(_fixture())
    assert extracted.about == "customer"
    assert extracted.handles.get("phone") == "+919876543210"


def test_a_submission_without_the_sent_phone_yields_no_handle() -> None:
    """A callback whose flowJSON.data carries no customer_mobile_number has no
    identity — the pass quarantines it as replayable, exactly as canon says a
    customer-level letter with no handle must be."""
    payload = _fixture()
    del payload["context"]["flowJSON"]["data"]["customer_mobile_number"]
    extracted = _extract(payload)
    assert extracted.about == "customer"
    assert extracted.handles == {}


def test_the_action_and_envelope_variables_ride_out_for_templates() -> None:
    """A workflow can read the action name, the context envelope, and the two
    fields WE sent (finding 1, 3, 11)."""
    extracted = _extract(_fixture())
    assert extracted.variables["actionId"] == "submit-interview-availability"
    assert extracted.variables["type"] == "submit"
    assert extracted.variables["kind"] == "interview_availability"
    assert extracted.variables["merchant_id"] == "flow-shop"
    assert extracted.variables["customer_mobile_number"] == "919876543210"
    assert extracted.variables["messageId"] == "e9be4e96-9e96-4c9e-b259-ee75ea8bb499"
    assert (
        extracted.variables["conversationId"] == "ead0b5ff-32f9-460c-92d6-f52847257465"
    )
    assert extracted.variables["userId"] == "cmo014zxi0dz1rkq60eouxllf"


def test_the_selected_slots_derive_is_person_readable() -> None:
    """The date-named slot keys become ONE string a call node can read aloud —
    one line per date, in the recipient's own spelling, nothing reworded."""
    extracted = _extract(_fixture())
    assert extracted.variables["selected_slots"] == (
        "2026-10-15: 10:00\n2026-10-16: 09:00, 10:00\n2026-10-17: 14:00"
    )


def test_a_date_nobody_picked_is_left_out() -> None:
    """A slot key the recipient left empty contributes no line."""
    payload = _fixture()
    payload["values"]["slots_2026-10-17"] = []
    extracted = _extract(payload)
    assert extracted.variables["selected_slots"] == (
        "2026-10-15: 10:00\n2026-10-16: 09:00, 10:00"
    )


def test_an_empty_submission_reads_no_selected_slots() -> None:
    """An empty submission is a completed form with nothing picked — the
    derived slot name is simply absent (a None derived never rides out; the
    run's context drops it before any template sees it)."""
    payload = {
        "actionId": "submit-interview-availability",
        "type": "submit",
        "values": {},
    }
    extracted = _extract(payload)
    assert "selected_slots" not in extracted.variables


def test_a_submission_with_no_values_object_reads_nothing() -> None:
    """A malformed envelope never raises: the declared context fields ride
    out, the declared list is said as None (a letter that made none), and the
    derived slot name is absent."""
    extracted = _extract(
        {"actionId": "submit-interview-availability", "type": "submit"}
    )
    assert extracted.variables["actionId"] == "submit-interview-availability"
    assert extracted.variables["type"] == "submit"
    assert extracted.variables["selected_dates"] is None
    assert "selected_slots" not in extracted.variables


def test_xyne_is_a_spec_source_not_an_imperative_extractor() -> None:
    """A source is in ONE of the two — the spec or the flat extractor, never
    both (test_catalog.py fails CI on the overlap)."""
    assert "xyne" not in EXTRACTORS


# Real router -> queued event -> real worker/consumer/enrol -> real walker.
# Only identity, storage, transactions, and outbound network calls are doubled.
_SECRET = "local-xyne-webhook-test"
_MERCHANT = "flow-shop"
_CUSTOMER = UUID("00000000-0000-0000-0000-000000000201")
_EXAMPLES = Path(__file__).parents[2] / "docs" / "crm" / "plans"
_enrol_module = importlib.import_module("app.crm.outreach.enrol")


def _example(mode: str) -> Dict[str, Any]:
    return json.loads((_EXAMPLES / f"xyne-availability-{mode}.json").read_text())


class _Memory:
    def __init__(self):
        self.events = []
        self.flows = []
        self.runs = []
        self.seen = set()
        self.stamped = []
        self.quarantined = []
        self.resumed = []
        self.outbound = []
        self.lookups = []
        self.available = True
        self.storage_error = False

    def flow(self, definition):
        now = datetime.now(timezone.utc)
        flow = Workflow(
            id=uuid4(),
            merchant_id=_MERCHANT,
            name="Xyne test",
            status="live",
            version=1,
            created_by=None,
            created_at=now,
            updated_at=now,
            definition=definition,
            draft=None,
        )
        self.flows.append(flow)
        return flow

    def run(self, flow, node, context=None):
        now = datetime.now(timezone.utc)
        run = EnrollmentRun(
            id=uuid4(),
            merchant_id=_MERCHANT,
            workflow_id=flow.id,
            workflow_version=flow.version,
            customer_id=_CUSTOMER,
            status="waiting",
            current_node=node,
            wake_at=now,
            entered_at=now,
            exited_at=None,
            exit_reason=None,
            context={"phone": "+919876543210", **(context or {})},
            enrollment_key=str(_CUSTOMER),
            attempts=1,
            last_error=None,
        )
        self.runs.append(run)
        return run

    async def get_installation_for_connector(
        self, merchant_id, connector_key, external_account_id=None
    ):
        self.lookups.append((merchant_id, connector_key, external_account_id))
        if not self.available or (merchant_id, connector_key, external_account_id) != (
            _MERCHANT,
            "merchant_http",
            "xyne",
        ):
            return None
        return ConnectorInstallation(
            id="local-xyne-account",
            merchant_id=merchant_id,
            connector_key=connector_key,
            external_account_id="xyne",
            status="healthy",
        )

    async def ingest_event(self, **event):
        if self.storage_error:
            raise RuntimeError("local test storage unavailable")
        key = (event["merchant_id"], event["source"], event["external_id"])
        if key in self.seen:
            return None
        self.seen.add(key)
        raw = RawEvent(id=str(uuid4()), received_at=datetime.now(timezone.utc), **event)
        self.events.append(raw)
        return raw.id

    async def stamp_event(self, txn, event_id, customer_id):
        self.stamped.append((event_id, customer_id))

    async def quarantine_event(self, txn, event_id, reason):
        self.quarantined.append((event_id, reason))

    async def live_workflows(self, merchant_id):
        return [flow for flow in self.flows if flow.merchant_id == merchant_id]

    async def workflow_status(self, merchant_id, workflow_id):
        return "live"

    async def open_runs_for_customer(self, merchant_id, customer_id):
        return [
            run
            for run in self.runs
            if run.status != "exited"
            and run.merchant_id == merchant_id
            and str(run.customer_id) == customer_id
        ]

    async def definition_for(self, run):
        flow = next(flow for flow in self.flows if flow.id == run.workflow_id)
        return WorkflowDefinition.model_validate(flow.definition)

    async def source_event_used(
        self, txn, merchant_id, workflow_id, customer_id, event_id
    ):
        return any(
            str(run.workflow_id) == workflow_id
            and run.context.get("source_event_id") == event_id
            for run in self.runs
        )

    async def admission_facts(self, *args, **kwargs):
        return {"runs": 0, "latest_entered_at": None}

    async def lock_templates_shared(self, *args):
        pass

    async def insert_enrollment(
        self,
        txn,
        merchant_id,
        workflow_id,
        version,
        customer_id,
        node,
        wake_at,
        context,
        enrollment_key,
    ):
        flow = next(flow for flow in self.flows if str(flow.id) == workflow_id)
        run = self.run(flow, node, context)
        run.wake_at = wake_at
        run.enrollment_key = enrollment_key
        return run

    async def resume_run_by_id(self, merchant_id, run_id, node_id, patch, facts=None):
        run = next(run for run in self.runs if str(run.id) == run_id)
        if run.merchant_id != merchant_id or run.current_node != node_id:
            return False
        self.resumed.append(run_id)
        run.context.update(patch)
        run.context.setdefault("facts", {})[node_id] = facts or {}
        run.wake_at = datetime.now(timezone.utc)
        return True

    async def exit_run(
        self, run_id, reason, lease, current_node=None, context=None, **kwargs
    ):
        run = next(run for run in self.runs if str(run.id) == run_id)
        run.status = "exited"
        run.exit_reason = reason
        if context is not None:
            run.context = context
        return True

    async def perform_action(
        self, merchant, connector, action, args, context, account=None
    ):
        self.outbound.append((args, context, account))
        return {"ok": True, "status": 200, "facts": {}}


@pytest.fixture
def runtime(monkeypatch):
    memory = _Memory()
    monkeypatch.setattr(inbound, "XYNE_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setitem(INGRESS, "xyne", connectivity_ingress.XYNE_INGRESS)
    monkeypatch.setattr(connectivity_ingress, "installation_accessor", memory)
    monkeypatch.setattr(record_api, "ingest_event", memory.ingest_event)
    monkeypatch.setattr(workers, "accessor", memory)
    monkeypatch.setattr(
        record_consumers, "_CONSUMERS", [entry.consume_attributed_event]
    )
    monkeypatch.setattr(entry, "enrollment_accessor", memory)
    monkeypatch.setattr(entry, "workflow_accessor", memory)
    monkeypatch.setattr(entry, "definition_for", memory.definition_for)
    monkeypatch.setattr(_enrol_module, "enrollment_accessor", memory)
    monkeypatch.setattr(_enrol_module, "version_accessor", memory)
    monkeypatch.setattr(walker, "enrollment_accessor", memory)
    monkeypatch.setattr(walker, "workflow_accessor", memory)
    monkeypatch.setattr(walker, "definition_for", memory.definition_for)
    monkeypatch.setattr(action_node, "perform_action", memory.perform_action)

    async def resolve(merchant, handles, **kwargs):
        assert merchant == _MERCHANT
        assert handles["phone"] == "+919876543210"
        return str(_CUSTOMER)

    async def atomically(operation, *args):
        return await operation(object(), *args)

    async def context_cap():
        return 1000

    async def no_goal(*args):
        return False

    monkeypatch.setattr(workers, "crm_resolve", resolve)
    monkeypatch.setattr(_enrol_module, "atomically", atomically)
    monkeypatch.setattr(entry, "CRM_CONTEXT_VALUE_MAX_CHARS", context_cap)
    monkeypatch.setattr(walker, "customer_has_event", no_goal)
    app = FastAPI()
    app.include_router(record_api.webhook_router, prefix="/crm/ingest/webhooks")
    with TestClient(app) as client:
        yield client, memory


def _submit(client, payload):
    raw = json.dumps(payload).encode()
    signature = hmac.new(_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/crm/ingest/webhooks/xyne",
        content=raw,
        headers={"x-xyne-signature": signature, "content-type": "application/json"},
    )


@pytest.mark.asyncio
async def test_signed_submission_starts_a_run_and_executes_its_next_action(runtime):
    client, memory = runtime
    memory.flow(_example("start"))
    payload = _fixture()
    del payload["context"]["flowJSON"]["data"]["enrollment_id"]
    response = _submit(client, payload)
    assert response.status_code == 200
    assert response.json()["type"] == "close_screen"
    assert memory.events[0].payload == payload
    await workers._process_one(cast(DbTxn, object()), memory.events[0])
    assert len(memory.runs) == 1
    run = memory.runs[0]
    assert run.customer_id == _CUSTOMER
    assert run.context["selected_slots"].startswith("2026-10-15: 10:00")
    await walker.walk_run(run)
    assert run.status == "exited" and run.exit_reason == "completed"
    assert len(memory.outbound) == 1
    assert memory.outbound[0][0]["body"]["text"] == run.context["selected_slots"]


@pytest.mark.asyncio
async def test_submission_resumes_only_its_originating_run_then_executes(runtime):
    client, memory = runtime
    flow = memory.flow(_example("resume"))
    intended = memory.run(flow, "await-availability")
    sibling = memory.run(flow, "await-availability")
    memory.flow(_example("start"))
    payload = _fixture()
    payload["context"]["flowJSON"]["data"]["enrollment_id"] = str(intended.id)
    assert _submit(client, payload).json()["type"] == "close_screen"
    await workers._process_one(cast(DbTxn, object()), memory.events[0])
    assert memory.resumed == [str(intended.id)]
    assert len(memory.runs) == 2  # the reply must not also start a new workflow
    assert "reply_await-availability" not in sibling.context
    await walker.walk_run(intended)
    assert intended.exit_reason == "completed"
    assert sibling.status == "waiting"
    assert len(memory.outbound) == 1
    assert "2026-10-15: 10:00" in memory.outbound[0][0]["body"]["text"]


@pytest.mark.asyncio
async def test_outgoing_form_contains_trusted_merchant_phone_and_run_metadata(runtime):
    _, memory = runtime
    flow = memory.flow(_example("resume"))
    run = memory.run(flow, "ask-availability", {"run_id": "spoofed-run"})
    definition = WorkflowDefinition.model_validate(flow.definition)
    await action_node.execute(run, definition.nodes[0], definition)
    data = memory.outbound[0][0]["body"]["flow"]["data"]
    assert data["merchant_id"] == run.merchant_id
    assert data["customer_mobile_number"] == "+919876543210"
    assert data["enrollment_id"] == str(run.id)
    assert memory.outbound[0][2] == "xyne"


@pytest.mark.asyncio
async def test_duplicate_callback_is_acknowledged_but_enqueues_only_once(runtime):
    client, memory = runtime
    memory.flow(_example("start"))
    payload = _fixture()
    del payload["context"]["flowJSON"]["data"]["enrollment_id"]
    for _ in range(2):
        assert _submit(client, payload).json()["type"] == "close_screen"
    assert len(memory.events) == 1
    await workers._process_one(cast(DbTxn, object()), memory.events[0])
    await workers._process_one(cast(DbTxn, object()), memory.events[0])
    assert len(memory.runs) == 1


@pytest.mark.asyncio
async def test_missing_customer_phone_quarantines_without_starting_a_run(runtime):
    client, memory = runtime
    memory.flow(_example("start"))
    payload = _fixture()
    data = payload["context"]["flowJSON"]["data"]
    del data["customer_mobile_number"]
    del data["enrollment_id"]
    assert _submit(client, payload).json()["type"] == "close_screen"
    await workers._process_one(cast(DbTxn, object()), memory.events[0])
    assert memory.quarantined == [(memory.events[0].id, "no_handle")]
    assert memory.runs == [] and memory.stamped == []


@pytest.mark.parametrize("bad_signature", [None, "0" * 64])
def test_invalid_callback_signature_never_reaches_storage(runtime, bad_signature):
    client, memory = runtime
    headers = {} if bad_signature is None else {"x-xyne-signature": bad_signature}
    response = client.post(
        "/crm/ingest/webhooks/xyne", json=_fixture(), headers=headers
    )
    assert response.status_code == 403
    assert memory.events == [] and memory.lookups == []


@pytest.mark.parametrize("failure", ["unknown_merchant", "no_account", "storage"])
def test_unstored_submissions_return_a_resubmission_error(runtime, failure):
    client, memory = runtime
    payload = _fixture()
    if failure == "unknown_merchant":
        payload["context"]["flowJSON"]["data"]["merchant_id"] = "another-merchant"
    elif failure == "no_account":
        memory.available = False
    else:
        memory.storage_error = True
    response = _submit(client, payload)
    assert response.status_code == 200
    assert response.json()["type"] == "error"
    assert memory.events == []


@pytest.mark.parametrize("mode", ["start", "resume"])
def test_workflow_examples_pass_catalog_validation(mode):
    fields: Catalogs = {
        item.topic: {field.path: field for field in item.fields}
        for item in catalog.code_entries()
        if item.source in {"xyne", "shopify"}
    }
    assert validate_definition(_example(mode), catalogs=fields) == []


def test_signature_covers_the_exact_body_bytes(runtime):
    client, memory = runtime
    raw = json.dumps(_fixture()).encode()
    signature = hmac.new(_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/crm/ingest/webhooks/xyne",
        content=raw + b"\n",
        headers={"x-xyne-signature": signature},
    )
    assert response.status_code == 403
    assert memory.events == [] and memory.lookups == []


def test_attribution_lookup_failure_returns_resubmission_error(runtime, monkeypatch):
    client, memory = runtime

    async def unavailable(*args, **kwargs):
        raise RuntimeError("local test database unavailable")

    monkeypatch.setattr(memory, "get_installation_for_connector", unavailable)
    assert _submit(client, _fixture()).json()["type"] == "error"
    assert memory.events == []


@pytest.mark.asyncio
async def test_an_unrelated_form_action_does_not_start_a_workflow(runtime):
    client, memory = runtime
    memory.flow(_example("start"))
    payload = _fixture()
    del payload["context"]["flowJSON"]["data"]["enrollment_id"]
    payload["actionId"] = "submit-another-form"
    assert _submit(client, payload).json()["type"] == "close_screen"
    await workers._process_one(cast(DbTxn, object()), memory.events[0])
    assert memory.runs == [] and memory.outbound == []


def test_missing_merchant_is_not_acknowledged_as_a_saved_submission(runtime):
    client, memory = runtime
    payload = _fixture()
    del payload["context"]["flowJSON"]["data"]["merchant_id"]
    assert _submit(client, payload).json()["type"] == "error"
    assert memory.events == [] and memory.lookups == []
