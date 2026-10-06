"""The dial path puts a lead back in its room with the rank it had.

Every re-queue on the dial path is ``schedule_lead(..., template_id=...)``. It carries
no rank, so on a ranked number the rank is read from the lead's row
(``meta_data.priority``). A re-queue that named no template would go to today's
schedule and lose the rank.
"""

import ast
import inspect
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
import app.database.accessor as accessor
from app.ai.voice.agents.breeze_buddy.dispatch import queue as queue_mod, worker
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import acceptor, routes, scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import Rank, Ticket
from tests.breeze_buddy.dispatch.v2.conftest import (
    OWNER,
    claim_next,
    seed_number,
    use_redis,
)

EVENT = 1_791_522_600_123
PRIORITY = {"rank": 3, "order": "newest_event", "event_ms": EVENT}
RANK3 = (3 - 100) * 10**13 + (10**13 - 1) - EVENT  # its ready score


def test_every_requeue_on_the_dial_path_names_the_template():
    for module in (worker, acceptor):
        calls = [
            node
            for node in ast.walk(ast.parse(inspect.getsource(module)))
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("schedule_lead", "_schedule_if_ours")
        ]
        assert calls, module.__name__
        for call in calls:
            # template_id=..., or the caller's **kwargs handed on
            assert any(
                kw.arg in ("template_id", None) for kw in call.keywords
            ), ast.unparse(call)


@pytest.mark.asyncio
async def test_a_lead_the_dial_path_gives_back_waits_again_at_its_rank(rr, monkeypatch):
    use_redis(monkeypatch, rr, routes, queue_mod)
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    await seed_number(rr, "N1", 1, {"T1": {}})
    await rr.hset("bb:num:N1", mapping={"ranked": "1", "live_day": "0"})
    await rr.sadd("bb:v2:active", "N1")
    now = int(time.time() * 1000)
    await scripts.enqueue("T1", "L1", now - 1, rank=Rank(3, "n", EVENT))
    claimed = await claim_next("N1")
    assert claimed and claimed[0] == "L1"  # the pile lead has the line

    # no call was placed: the line goes back first, then the lead (acceptor._requeue)
    await rr.set("bb:dispatch:enabled", "0")  # keep the re-queued lead in the room
    assert await scripts.return_line("N1", "L1", claimed[1], OWNER) is not None
    lead = NS(metaData={"priority": PRIORITY})
    monkeypatch.setattr(accessor, "get_lead_by_id", AsyncMock(return_value=lead))
    await acceptor._requeue(
        Ticket(
            number_id="N1", lead_id="L1", tk=claimed[1], template_id="T1", issued_ms=now
        )
    )

    assert await rr.zscore("bb:q:T1", "L1") == RANK3
