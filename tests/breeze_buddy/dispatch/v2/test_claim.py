"""A ticket is delivered through one list; the first owner to claim it is the only one who
can mark, give back or clear it (spec 2026-10-05 §4.3, design card rule 49)."""

import json
import time

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
from tests.breeze_buddy.dispatch.v2.conftest import seed_number

pytestmark = pytest.mark.asyncio


def NOW() -> int:
    return int(time.time() * 1000)


async def _issue(rr, lead: str = "L1") -> scripts.Ticket:
    await seed_number(rr, "N1", 2, {"T1": {}})
    assert await scripts.enqueue("T1", lead, NOW() - 1) == 1
    t = scripts.parse_ticket(await rr.lpop("bb:tickets"))
    assert t is not None
    return t


async def test_match_appends_the_ticket_to_the_one_list(rr):
    t = await _issue(rr)
    assert (t.number_id, t.lead_id, t.template_id) == ("N1", "L1", "T1")
    lease = json.loads(await rr.hget("bb:inflight:N1", "L1"))
    assert (lease["tk"], lease["issued_ms"]) == (t.tk, t.issued_ms)
    assert "owner" not in lease


async def test_the_first_owner_wins_and_a_second_claim_is_refused(rr):
    t = await _issue(rr)
    assert await scripts.claim("N1", "L1", t.tk, "a") is True
    assert await scripts.claim("N1", "L1", t.tk, "b") is False
    assert await scripts.claim("N1", "L1", t.tk, "a") is True  # our own re-run
    lease = json.loads(await rr.hget("bb:inflight:N1", "L1"))
    assert lease["owner"] == "a" and lease["claimed_ms"] >= t.issued_ms


async def test_a_stale_ticket_id_cannot_be_claimed(rr):
    t = await _issue(rr)
    assert await scripts.claim("N1", "L1", t.tk + 1, "a") is False
    assert "owner" not in json.loads(await rr.hget("bb:inflight:N1", "L1"))


async def test_only_the_owner_can_give_back_or_clear(rr):
    t = await _issue(rr)
    assert await scripts.claim("N1", "L1", t.tk, "a")
    assert await scripts.return_line("N1", "L1", t.tk, "b") == -1
    assert await scripts.clear_lease("N1", "L1", t.tk, "b") is False
    assert await rr.sismember("bb:busy:N1", "lead:L1")
    assert await scripts.return_line("N1", "L1", t.tk, "a") == 0
    assert not await rr.sismember("bb:busy:N1", "lead:L1")


async def test_an_unclaimed_ticket_cannot_be_marked_or_given_back(rr):
    t = await _issue(rr)
    assert await scripts.mark_dialling("N1", "L1", t.tk, "a") is scripts.Mark.REFUSED
    assert await scripts.return_line("N1", "L1", t.tk, "a") == -1
    assert await rr.sismember("bb:busy:N1", "lead:L1")


async def test_mark_dialling_needs_the_claiming_owner(rr):
    t = await _issue(rr)
    assert await scripts.claim("N1", "L1", t.tk, "a")
    SUPERSEDED, DIAL = scripts.Mark.SUPERSEDED, scripts.Mark.DIAL
    assert await scripts.mark_dialling("N1", "L1", t.tk, "b") is SUPERSEDED
    assert await scripts.mark_dialling("N1", "L1", t.tk, "a") is DIAL
    assert await scripts.mark_dialling("N1", "L1", t.tk, "a") is DIAL  # our re-run
    assert await scripts.mark_dialling("N1", "L1", t.tk, "b") is SUPERSEDED


async def test_the_kill_switch_refuses_the_mark(rr):
    # rule 24 at the commit point: a dispatch whose checks ran while dialling was
    # switched off never sends its request
    t = await _issue(rr)
    assert await scripts.claim("N1", "L1", t.tk, "a")
    await rr.set("bb:dispatch:enabled", "0")
    assert await scripts.mark_dialling("N1", "L1", t.tk, "a") is scripts.Mark.REFUSED
    assert "dialling_ms" not in json.loads(await rr.hget("bb:inflight:N1", "L1"))
    await rr.set("bb:dispatch:enabled", "1")
    assert await scripts.mark_dialling("N1", "L1", t.tk, "a") is scripts.Mark.DIAL


async def test_the_reaper_clears_any_lease_with_the_ticket_id(rr):
    t = await _issue(rr)
    assert await scripts.claim("N1", "L1", t.tk, "a")
    assert await scripts.clear_lease("N1", "L1", t.tk + 1) is False
    assert await scripts.clear_lease("N1", "L1", t.tk) is True
    assert await rr.sismember("bb:busy:N1", "lead:L1")  # the call holds the line


async def test_a_garbled_entry_parses_to_none():
    assert scripts.parse_ticket("N1|L1|x|T1|5") is None
    assert scripts.parse_ticket("only|four|parts|here") is None
    assert scripts.parse_ticket("") is None
    assert scripts.parse_ticket(None) is None
    assert scripts.parse_ticket("N1|L1|7|T1|5") == scripts.Ticket(
        "N1", "L1", 7, "T1", 5
    )
