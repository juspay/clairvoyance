"""The spine consumer registry: record owns the WHEN, worker_main owns the
WHO, and the import arrow only ever points subscriber -> record."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

import app.crm.record.consumers as record_consumers
import app.crm.record.workers as workers
from app.crm.record.consumers import consumers, register_consumer
from app.crm.record.schemas import RawEvent


async def _consumer_a(
    event: RawEvent, customer_id: Optional[str], handles: Any, variables: Any = None
) -> None:
    return None


def test_register_is_idempotent() -> None:
    # Worker imports can run twice (tests, reload); the same function must
    # never end up delivering every event twice.
    before = consumers()
    register_consumer(_consumer_a)
    register_consumer(_consumer_a)
    added = [c for c in consumers() if c is _consumer_a]
    assert len(added) == 1
    record_consumers._CONSUMERS = [
        c for c in record_consumers._CONSUMERS if c is not _consumer_a
    ]
    assert consumers() == before


def test_consumers_returns_a_copy() -> None:
    # Mutating the returned list must not edit the registry.
    snapshot = consumers()
    snapshot.append(_consumer_a)
    assert _consumer_a not in consumers()


def test_the_pass_runs_every_registered_consumer_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Registration order is execution order: entry rules first today;
    # segments and the A13 transactional-send consumer join behind them
    # with zero edits in the pass.
    ran: List[str] = []

    async def first(
        event: RawEvent, customer_id: Optional[str], handles: Any, variables: Any = None
    ) -> None:
        ran.append(f"first:{customer_id}:{(handles or {}).get('phone')}")

    async def second(
        event: RawEvent, customer_id: Optional[str], handles: Any, variables: Any = None
    ) -> None:
        ran.append(f"second:{customer_id}")

    monkeypatch.setattr(record_consumers, "_CONSUMERS", [first, second])
    event = RawEvent(
        id="e1",
        merchant_id="m1",
        source="lead-api",
        topic="t",
        schema_version="1",
        external_id="x",
        payload={},
        received_at=datetime.now(timezone.utc),
    )
    asyncio.run(
        workers._consume_attributed_event(event, "cust-1", {"phone": "+911234567890"})
    )
    assert ran == ["first:cust-1:+911234567890", "second:cust-1"]


def test_worker_main_registers_the_entry_consumer() -> None:
    # The composition root fills the slot: importing worker_main is what
    # wires outreach's entry rules onto the spine.
    import app.crm.worker_main  # noqa: F401  (registration is an import effect)
    from app.crm.outreach.contracts import consume_attributed_event

    assert consume_attributed_event in consumers()


def test_record_imports_no_subscriber() -> None:
    # The structural inversion itself, greppable: nothing under record/
    # imports outreach (or any other subscriber module). Rule 12 enforces
    # this in CI over the real tree; this pins it from the test suite too.
    import pathlib

    record_dir = pathlib.Path(workers.__file__).parent
    offenders: Dict[str, str] = {}
    for py in record_dir.rglob("*.py"):
        text = py.read_text()
        for needle in ("app.crm.outreach", "app.crm.connectivity"):
            if needle in text:
                offenders[str(py)] = needle
    assert offenders == {}


def test_worker_main_registers_the_template_retire_guard() -> None:
    # Phase 14: the same inversion for connectivity's retire guard —
    # connectivity may not import outreach (outreach already imports its
    # contracts; the reverse arrow would close a cycle), so worker_main
    # hands outreach's count into connectivity's slot.
    import app.crm.worker_main  # noqa: F401  (registration is an import effect)
    from app.crm.connectivity import retire_guard as connectivity_retire_guard
    from app.crm.outreach.contracts import template_references

    assert connectivity_retire_guard._retire_guard is template_references


def test_the_api_process_wires_the_guard_through_worker_main() -> None:
    # app/main.py imports worker_main (for start/stop of the worker role),
    # so the API pod — where the retire route lives — registers it too.
    # Read from the AST, so a commented-out or string-quoted mention can
    # never satisfy this.
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/main.py").read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "app.crm.worker_main" in imported


def test_connectivity_imports_no_outreach() -> None:
    # The structural half of the guard: the hook exists so this stays true.
    # Judged from each file's import nodes — absolute or relative — so a
    # `from ..outreach import x` is caught and a mention in a comment or
    # docstring is not.
    import ast
    import pathlib

    import app.crm.connectivity.templates as connectivity_templates

    package_dir = pathlib.Path(connectivity_templates.__file__).parent
    offenders: List[str] = []
    for py in package_dir.rglob("*.py"):
        package = "app.crm.connectivity" + (
            "." + ".".join(py.relative_to(package_dir).parent.parts)
            if py.relative_to(package_dir).parent.parts
            else ""
        )
        for node in ast.walk(ast.parse(py.read_text())):
            targets: List[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package.split(".")[
                        : len(package.split(".")) - node.level + 1
                    ]
                    targets = [".".join(base + ([node.module] if node.module else []))]
                elif node.module:
                    targets = [node.module]
            offenders.extend(
                f"{py}: {target}"
                for target in targets
                if target == "app.crm.outreach"
                or target.startswith("app.crm.outreach.")
            )
    assert offenders == []
