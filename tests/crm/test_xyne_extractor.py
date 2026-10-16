"""XyneSpaces flow_action through the one decode engine: the code-layer spec
attributes a form submission to the merchant, never to a person.

The chain this pins: the ingress door files source="xyne" letters under the
flow/installation account (connector 'xyne'), and the event worker reads each
by its catalog spec. The relay's submission carries no phone, no email and no
account id — only the platform's `context.userId` for who filled it, and there
is no binding table from that userId to a merchant customer. So the entry is
declared about="merchant" (canon T13 col 14): processed with customer NULL,
never quarantined, and every consumer still hears the letter.

The generic four-part square (fixtures exist, every field resolves, the engine
finds the person in every recorded letter, derivers match) is pinned for ALL
code entries by test_catalog.py; what lives here is xyne's own behaviour — the
merchant-level decode, the declared variables riding out for templates, and
the one derived tool turning the date-named slot keys into a single readable
string a workflow can fill a template blank with.

Payloads are shaped exactly as filed: the relay's flow_action submission,
unmodified.
"""

import json
from pathlib import Path
from typing import Any, Dict

import app.crm.record.extractors.xyne as xyne_spec
from app.crm.record import catalog
from app.crm.record.extractors import EXTRACTORS, engine

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


# --- the decode: merchant-level, no person, clean variables --------------------


def test_flow_action_is_declared_about_the_merchant() -> None:
    """The submission names no person by design — no phone, no email, no way
    to a customer. Declared merchant-level so the pass stamps a NULL customer
    and never quarantines a letter that is not about a person."""
    spec = catalog.code_spec("xyne", "flow_action")
    assert spec is not None and spec.about == "merchant"


def test_the_fixture_decodes_merchant_level_with_no_handles() -> None:
    """The engine over the recorded submit yields no handle and a merchant
    about — the same refusal test_catalog pins for every merchant entry."""
    extracted = _extract(_fixture())
    assert extracted.about == "merchant"
    assert extracted.handles == {}


def test_the_action_and_envelope_variables_ride_out_for_templates() -> None:
    """A workflow can read the action name and the context envelope."""
    extracted = _extract(_fixture())
    assert extracted.variables["actionId"] == "submit-interview-availability"
    assert extracted.variables["type"] == "submit"
    assert extracted.variables["kind"] == "interview_availability"
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
