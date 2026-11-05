"""The two-level entry read: route on (id, version), open a document only
when a door matches.

Each test here is a failure that review found in the design before it was
written. They are kept as tests rather than comments because every one of
them fails OPEN — no exception, no alert, just enrolments that stop or a
call that goes out with its words missing.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from app.crm.outreach import definitions, enrol as enrol_mod
from app.crm.outreach.schemas import WorkflowDefinition

_DOCUMENT: Dict[str, Any] = {
    "entry": {"topic": "checkout.initiated"},
    "nodes": [{"id": "wait-30m", "type": "wait", "minutes": 30}],
    "edges": [],
    "goal": {"topics": ["order.placed"]},
}


@pytest.fixture(autouse=True)
def _clean_caches() -> Any:
    """Both maps, both directions — a test that passes because a sibling
    warmed a key is worse than one that fails."""
    definitions.reset_caches()
    yield
    definitions.reset_caches()


# --- the refusal that would have stopped every enrolment, silently ---


def test_enrol_admits_a_plan_whose_summary_carries_no_document() -> None:
    """Routing hands enrol a SUMMARY — id, version, status, no documents.

    enrol used to guard on ``not workflow.definition``. Against that shape
    the guard is always true, so every enrolment is refused while the log
    line says ``skip_reason="not_live"`` about a plan whose status field,
    in the same line, reads "live". Nothing raises and nothing alerts: the
    first report is a merchant asking why their campaign stopped.
    """
    summary = SimpleNamespace(id=uuid4(), version=3, status="live")
    assert not hasattr(summary, "definition")

    skipped: List[Dict[str, Any]] = []

    class _Logger:
        def bind(self, **fields: Any) -> "_Logger":
            skipped.append(fields)
            return self

        def info(self, *_: Any, **__: Any) -> None:
            pass

        def warning(self, *_: Any, **__: Any) -> None:
            pass

        def error(self, *_: Any, **__: Any) -> None:
            pass

    async def _passthrough(fn: Any, *args: Any) -> Any:
        raise _Reached()

    class _Reached(Exception):
        """Proof the guard let us past it and into the atom."""

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(enrol_mod, "logger", _Logger())
        mp.setattr(enrol_mod, "atomically", _passthrough)
        with pytest.raises(_Reached):
            asyncio.run(
                enrol_mod.enrol(
                    merchant_id="m1",
                    workflow=summary,  # type: ignore[arg-type]
                    definition=WorkflowDefinition.model_validate(_DOCUMENT),
                    customer_id="c-1",
                    context={},
                )
            )

    assert not any(f.get("skip_reason") == "not_live" for f in skipped)


def test_enrol_still_refuses_a_plan_that_is_not_live() -> None:
    """The half of the guard that carries meaning stays: status is the
    only thing that decides, now that the document arrives separately."""
    summary = SimpleNamespace(id=uuid4(), version=3, status="paused")
    seen: List[Dict[str, Any]] = []

    class _Logger:
        def bind(self, **fields: Any) -> "_Logger":
            seen.append(fields)
            return self

        def info(self, *_: Any, **__: Any) -> None:
            pass

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(enrol_mod, "logger", _Logger())
        run = asyncio.run(
            enrol_mod.enrol(
                merchant_id="m1",
                workflow=summary,  # type: ignore[arg-type]
                definition=WorkflowDefinition.model_validate(_DOCUMENT),
                customer_id="c-1",
                context={},
            )
        )

    assert run is None
    assert any(f.get("skip_reason") == "not_live" for f in seen)


# --- the two maps must never become one ---


def test_the_live_document_never_lands_in_the_pinned_cache() -> None:
    """The live read is playbook-stripped; the pinned read is whole. Both
    are keyed (workflow, version), so one shared map would let whichever
    reader missed first decide what every later reader gets — and a walker
    node that found ``definition.playbook is None`` fails OPEN: blocks_for
    returns ({}, {}) and the call is placed with its words missing."""
    wf = str(uuid4())

    async def _live(merchant_id: str, workflow_id: str, version: int) -> Dict[str, Any]:
        return _DOCUMENT

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(definitions.workflow_accessor, "live_definition", _live)
        got = asyncio.run(definitions.live_definition("m1", wf, 3))

    assert got is not None
    assert (wf, 3) in definitions._live_definitions
    assert definitions._definitions == {}, (
        "a playbook-stripped document reached the pinned cache — the walker "
        "would serve it to blocks_for and place a call with no words"
    )


# --- the miss path ---


def test_concurrent_callers_for_one_plan_make_one_read() -> None:
    """The map is written only after an await, so without single-flight
    the N rows of one batch that want the same plan each miss and each
    read — the same shape the llm_call cache was fixed for."""
    reads: List[int] = []

    async def _live(merchant_id: str, workflow_id: str, version: int) -> Dict[str, Any]:
        reads.append(1)
        await asyncio.sleep(0)  # let the other callers reach the gate
        return _DOCUMENT

    wf = str(uuid4())

    async def _race() -> List[Optional[WorkflowDefinition]]:
        return list(
            await asyncio.gather(
                *(definitions.live_definition("m1", wf, 2) for _ in range(8))
            )
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(definitions.workflow_accessor, "live_definition", _live)
        results = asyncio.run(_race())

    assert len(reads) == 1, f"expected one read for eight callers, made {len(reads)}"
    assert all(r is not None for r in results)


def test_a_version_that_moved_is_not_cached_and_answers_none() -> None:
    """A publish between routing and the document read means the version
    we were routed to is no longer live. None is the honest answer — the
    caller skips this plan for this one letter and the next letter routes
    to the new version, which is a different key."""

    async def _gone(merchant_id: str, workflow_id: str, version: int) -> None:
        return None

    wf = str(uuid4())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(definitions.workflow_accessor, "live_definition", _gone)
        got = asyncio.run(definitions.live_definition("m1", wf, 9))

    assert got is None
    assert (wf, 9) not in definitions._live_definitions
    assert definitions._live_pending == {}


def test_the_live_cache_is_bounded() -> None:
    """Version churn mints a new key per publish, and this process has an
    eviction history — the map must evict, not grow."""

    async def _live(merchant_id: str, workflow_id: str, version: int) -> Dict[str, Any]:
        return _DOCUMENT

    async def _fill() -> None:
        for v in range(definitions._LIVE_CACHE_SIZE + 25):
            await definitions.live_definition("m1", "wf-1", v)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(definitions.workflow_accessor, "live_definition", _live)
        asyncio.run(_fill())

    assert len(definitions._live_definitions) == definitions._LIVE_CACHE_SIZE
