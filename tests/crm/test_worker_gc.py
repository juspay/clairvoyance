"""A CRM worker role freezes its startup objects before its loops start
(app/crm/worker_main.py, start_worker_role).

What is proven here:
- a worker role collects once, then freezes, BEFORE its loop runs — so the
  loop's own collections never walk the startup heap again;
- the api role and an unregistered role freeze nothing (the api returns
  early; an unknown role is refused before anything happens).
"""

import asyncio
from typing import Any, List

import pytest

import app.crm.worker_main as worker_main


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    seen: List[str] = []
    monkeypatch.setattr(worker_main.gc, "collect", lambda: seen.append("collect"))
    monkeypatch.setattr(worker_main.gc, "freeze", lambda: seen.append("freeze"))

    async def role(stop_event: asyncio.Event) -> None:
        seen.append("loop")
        await stop_event.wait()

    monkeypatch.setattr(worker_main, "ROLES", {"walker": role})
    monkeypatch.setattr(worker_main, "_task", None)
    monkeypatch.setattr(worker_main, "_stop_event", None)
    return seen


async def test_a_worker_role_freezes_its_startup_objects_before_its_loop(
    calls: List[str],
) -> None:
    await worker_main.start_worker_role("walker")
    await asyncio.sleep(0)  # let the role's task take its first step
    await worker_main.stop_worker_role()
    assert calls == ["collect", "freeze", "loop"]


@pytest.mark.parametrize("role", ["api", "no-such-role"])
async def test_the_api_and_an_unknown_role_freeze_nothing(
    calls: List[str], role: str
) -> None:
    raised: Any = None
    try:
        await worker_main.start_worker_role(role)
    except ValueError as e:
        raised = e
    assert calls == []
    assert (raised is not None) == (role != "api")
