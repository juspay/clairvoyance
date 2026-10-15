"""A CRM worker role freezes its startup objects before its loops start
(app/crm/worker_main.py, start_worker_role).

What is proven here, with the real garbage collector (only worker_main's own
name for it is wrapped, to see the order; the global module is untouched):
- a worker role collects once, then freezes, BEFORE its loop runs — the
  objects alive at start really are frozen afterwards, and the pod logs how
  many;
- the api role, an unregistered role and a pool too small to work freeze
  nothing (each returns or is refused before the freeze).

Every test unfreezes on the way out, so the rest of the suite runs on a
normal heap.
"""

import asyncio
import gc
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Tuple

import pytest

import app.crm.worker_main as worker_main


class _Log:
    """The two logger calls start_worker_role makes: bind, then info."""

    def __init__(self) -> None:
        self.lines: List[Tuple[Dict[str, Any], str]] = []
        self._bound: Dict[str, Any] = {}

    def bind(self, **fields: Any) -> "_Log":
        self._bound = fields
        return self

    def info(self, message: str) -> None:
        self.lines.append((self._bound, message))


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[str]]:
    seen: List[str] = []

    def collect() -> int:
        seen.append("collect")
        return gc.collect()

    def freeze() -> None:
        seen.append("freeze")
        gc.freeze()

    monkeypatch.setattr(
        worker_main,
        "gc",
        SimpleNamespace(
            collect=collect, freeze=freeze, get_freeze_count=gc.get_freeze_count
        ),
    )

    async def role(stop_event: asyncio.Event) -> None:
        seen.append("loop")
        await stop_event.wait()

    monkeypatch.setattr(worker_main, "ROLES", {"walker": role})
    monkeypatch.setattr(worker_main, "_task", None)
    monkeypatch.setattr(worker_main, "_stop_event", None)
    gc.unfreeze()
    yield seen
    gc.unfreeze()


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _Log:
    fake = _Log()
    monkeypatch.setattr(worker_main, "logger", fake)
    return fake


async def test_a_worker_role_freezes_its_startup_objects_before_its_loop(
    calls: List[str], log: _Log
) -> None:
    await worker_main.start_worker_role("walker")
    await asyncio.sleep(0)  # let the role's task take its first step
    frozen = gc.get_freeze_count()
    await worker_main.stop_worker_role()
    assert calls == ["collect", "freeze", "loop"]
    assert frozen > 0
    # The logged count is taken at the freeze; frozen objects can still be
    # freed by their reference count afterwards, so it is not compared exact.
    [(fields, message)] = log.lines
    assert fields["worker"] == "walker" and fields["frozen"] > 0
    assert message == "walker: startup objects frozen"


@pytest.mark.parametrize("role", ["api", "no-such-role"])
async def test_the_api_and_an_unknown_role_freeze_nothing(
    calls: List[str], log: _Log, role: str
) -> None:
    raised: Any = None
    try:
        await worker_main.start_worker_role(role)
    except ValueError as e:
        raised = e
    assert calls == []
    assert gc.get_freeze_count() == 0
    assert log.lines == []
    assert (raised is not None) == (role != "api")


async def test_a_pool_too_small_to_work_is_refused_before_the_freeze(
    calls: List[str], log: _Log, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_main, "POSTGRES_POOL_SIZE", 1)
    monkeypatch.setattr(worker_main, "POSTGRES_MAX_OVERFLOW", 0)
    with pytest.raises(RuntimeError, match="pool ceiling of at least 2"):
        await worker_main.start_worker_role("walker")
    assert calls == []
    assert gc.get_freeze_count() == 0
    assert log.lines == []
