"""Atomic Lua scripts for the v2 dialler (design card §2, rules 15–21). Single-node Redis (D14).

Mode lives only in ``bb:num:{N}.mode`` (rule 21): ``enqueue`` writes only for a
v2-accounted number (``v2_pending``, ``v2``, ``draining``) and ``match`` issues
tickets only in ``v2``.

Every wrapper returns None (False for the yes/no gates) when Redis errors: ``_run``
catches every error, on the dialler's own client, which never retries or sleeps
(``redis_client.py``). Callers treat None as "not done" and rely on the sweep /
reconcilers (D16).

Scripts go by their SHA1 (EVALSHA, design card rule 57): the body travels only when
Redis answers NOSCRIPT (first use, a restart, a failover, SCRIPT FLUSH), which means the
script did not run, so sending it then is not a second run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum, IntEnum
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from redis.exceptions import NoScriptError, ResponseError

from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    RESELLER_PAUSED_PREFIX,
    SCHEDULE_ZSET,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.redis_client import v2_redis
from app.core.config.static import BB_V2_DUE_RECHECK_S, BB_V2_MATCH_CAP
from app.core.logger import logger

IST_OFFSET_S = 19800  # India Standard Time, UTC+5:30 (calling hours are IST)

# Same rule as managers/calls.py ``hours_open`` (inclusive at both ends, wraps past
# midnight), on whole IST seconds of the day: the window closes up to 1 s later than
# Python's, which compares microseconds (21:00:00.4 is open here, closed there).
# A route without hours is open.
_HOURS_FN = """
local function hours_open(s, e, sec)
  if s == nil or e == nil then return true end
  if s <= e then return sec >= s and sec <= e end
  return sec >= s or sec <= e
end
"""

# Today's pause key for a reseller, set by hand (rule 8). ``match`` reads it directly, so
# nothing has to mirror or SCAN for it (Fable M2).
_PAUSED_PREFIX_LUA = "local PAUSED_PREFIX = '%s'\n" % RESELLER_PAUSED_PREFIX
_RECHECK_LUA = "local RECHECK_MS = %d\n" % int(BB_V2_DUE_RECHECK_S * 1000)

# match keeps its number's bb:due entry (design card rule 55): the time it can next issue.
_MATCH_FN = _HOURS_FN + _PAUSED_PREFIX_LUA + _RECHECK_LUA + """
local function now_ms_and_ist()
  local t = redis.call('TIME')
  local sec = tonumber(t[1])
  return sec * 1000 + math.floor(tonumber(t[2]) / 1000), (sec + %d) %% 86400
end

local function v2_match(n, cap)
  if redis.call('GET', 'bb:dispatch:enabled') == '0' then return 0 end
  local num = 'bb:num:' .. n
  local mode = redis.call('HGET', num, 'mode')
  if mode ~= 'v2' then                                             -- only v2 issues (rule 21)
    if not mode or mode == 'legacy' then
      -- today's dialler owns a legacy number: nothing to match, so the sweep stops
      -- looking at it (Fable M1)
      redis.call('ZREM', 'bb:due', n)
    else
      -- v2_pending / draining: nothing to issue until the switch flips the mode (the
      -- flip runs match). An entry already due is looked at again in RECHECK_MS instead
      -- of every tick, so switching numbers never crowd the tick's range.
      redis.call('ZADD', 'bb:due', 'XX', 'GT', now_ms_and_ist() + RECHECK_MS, n)
    end
    return 0
  end
  local busy, inflight = 'bb:busy:' .. n, 'bb:inflight:' .. n
  local free = tonumber(redis.call('HGET', num, 'max') or '0') - redis.call('SCARD', busy)
  -- Full: nothing to issue, so skip the pass over N's templates (on a number shared by
  -- hundreds of templates that pass is what a sweep would repeat), and leave bb:due:
  -- every script that frees a line runs match, which lists N again.
  if free <= 0 then
    redis.call('ZREM', 'bb:due', n)
    return 0
  end
  local numtpl = 'bb:numtpl:' .. n
  local now_ms, ist = now_ms_and_ist()
  -- One pass over N's templates; route fields can't change while the script runs.
  -- A template no longer routed to N leaves bb:numtpl:{N} (Fable I2); if it has waiting
  -- leads, the number it is routed to now is due at once.
  -- A room match can't take from now is looked at again: a paused reseller's in
  -- RECHECK_MS (the pause is today's key, removed by hand: no write tells v2), a
  -- closed window's when it opens. A disabled route needs no timer: the route write that
  -- enables it lists N as due (routes.py).
  local open, closed = {}, {}
  for _, tid in ipairs(redis.call('SMEMBERS', numtpl)) do
    local room = 'bb:q:' .. tid
    local r = redis.call('HMGET', 'bb:route:' .. tid, 'number', 'enabled', 'reseller', 'start', 'end', 'tier')
    if r[1] ~= n then
      redis.call('SREM', numtpl, tid)
      if r[1] and r[1] ~= '' and redis.call('ZCARD', room) > 0 then
        redis.call('ZADD', 'bb:due', 'LT', now_ms, r[1])
      end
    elseif r[2] == '1' then
      local recheck_ms = nil
      if r[3] and redis.call('EXISTS', PAUSED_PREFIX .. r[3]) == 1 then
        recheck_ms = RECHECK_MS
      elseif not hours_open(tonumber(r[4]), tonumber(r[5]), ist) then
        recheck_ms = ((tonumber(r[4]) - ist) %% 86400) * 1000  -- closed: both ends set
      end
      if recheck_ms then
        closed[#closed + 1] = {room = room, recheck_ms = recheck_ms}
      else
        local boost = 0
        if r[6] == 'high' then boost = 600000 elseif r[6] == 'medium' then boost = 300000 end
        open[#open + 1] = {room = room, t = tid, boost = boost, l = false, rank = 0}
      end
    end
  end
  -- Each open room's due head is read once per run, and only the room a lead was just
  -- taken from is read again: a script runs alone, so nothing else changes the rooms
  -- meanwhile (review #1287 finding 6: re-reading every room for every ticket made a
  -- burst on a number shared by 300 templates 300 x 100 reads). Same order, same ties.
  local function head(o)
    local first = redis.call('ZRANGE', o.room, 0, 0, 'WITHSCORES')
    o.s = first[1] and tonumber(first[2]) or nil  -- the head's due time, due or not
    if o.s and o.s <= now_ms then
      o.l, o.rank = first[1], o.s - o.boost
    else
      o.l = false
    end
  end
  local issued, known = 0, false  -- known: every room's head was read in this run
  -- A lead that already holds a line is only removed, which issues nothing: bound the
  -- steps so a room full of such copies never makes one long script. A room left with
  -- copies keeps a due head, so bb:due below lists N for the next tick.
  local steps, max_steps = 0, cap + #open
  while free > 0 and issued < cap and steps < max_steps do
    steps = steps + 1
    local best = nil
    for _, o in ipairs(open) do
      if not known then head(o) end  -- the first ticket: one pass, read and compare
      if o.l and (best == nil or o.rank < best.rank) then best = o end
    end
    known = true
    if best == nil then break end
    local best_l = best.l
    redis.call('ZREM', best.room, best_l)
    best.l = false  -- re-read below, only if this run issues another ticket
    -- A lead that already holds a line on N (e.g. queued under two templates) only loses
    -- this copy: a second ticket would overwrite its lease and dial it twice.
    if redis.call('SISMEMBER', busy, 'lead:' .. best_l) == 0
       and redis.call('HEXISTS', inflight, best_l) == 0 then
      redis.call('SADD', busy, 'lead:' .. best_l)
      local tk = redis.call('HINCRBY', num, 'seq', 1)                   -- ticket id (rule 15)
      redis.call('HSET', inflight, best_l, cjson.encode({t = best.t, issued_ms = now_ms, tk = tk}))
      redis.call('RPUSH', 'bb:tickets', n .. '|' .. best_l .. '|' .. tk .. '|' .. best.t .. '|' .. now_ms)
      free = free - 1
      issued = issued + 1
    end
    if free > 0 and issued < cap then head(best) end
  end
  -- bb:due: when match can next issue on N. With a line still free every open room's
  -- head is current (read in this run: o.s); a closed room costs one ZCARD.
  local next_ms = nil
  if issued >= cap then
    next_ms = now_ms  -- more to issue: the next tick goes on
  elseif free > 0 then
    for _, o in ipairs(open) do
      if o.s and (next_ms == nil or o.s < next_ms) then next_ms = o.s end
    end
    for _, c in ipairs(closed) do
      local at = now_ms + c.recheck_ms
      if (next_ms == nil or at < next_ms) and redis.call('ZCARD', c.room) > 0 then
        next_ms = at
      end
    end
  end
  if next_ms == nil then
    redis.call('ZREM', 'bb:due', n)  -- full (a freed line runs match) or nothing waits
  else
    redis.call('ZADD', 'bb:due', math.max(next_ms, now_ms), n)
  end
  return issued
end
""" % IST_OFFSET_S


class Enqueue(IntEnum):
    """``enqueue``'s refusals; a reply >= 0 is the tickets issued."""

    ROUTE_MISSING = -1  # no bb:route:{T}: resolve it and retry (rule 18)
    HOLDS_LINE = -2  # the lead holds a line on N: its holder re-queues it (rule 17)
    NOT_V2 = -3  # N is not v2-accounted, nothing written: today's schedule


# ARGV: template_id, lead_id, due_ms, cap, only_if_absent ('1'|'0')
# -> tickets issued, or an ``Enqueue`` refusal.
# only_if_absent (the backlog reconciler): a lead already in its room is left as it is,
# score untouched and no match run, so re-reading a big pile writes nothing and a stale
# page can't move a deferred lead's due time earlier.
ENQUEUE_LUA = _MATCH_FN + """
local t, l = ARGV[1], ARGV[2]
-- PoC bug (b051299f): after a Redis flush a process still had the route cached and kept
-- queuing into rooms whose route no longer existed. Refuse; the caller re-resolves.
local n = redis.call('HGET', 'bb:route:' .. t, 'number')
if not n then return -1 end                  -- Enqueue.ROUTE_MISSING
if n == '' then return -3 end                -- Enqueue.NOT_V2: no number resolved
local mode = redis.call('HGET', 'bb:num:' .. n, 'mode')
if mode ~= 'v2_pending' and mode ~= 'v2' and mode ~= 'draining' then return -3 end  -- NOT_V2
-- Enqueue.HOLDS_LINE
if redis.call('SISMEMBER', 'bb:busy:' .. n, 'lead:' .. l) == 1 then return -2 end
if redis.call('HEXISTS', 'bb:inflight:' .. n, l) == 1 then return -2 end
if ARGV[5] == '1' and redis.call('ZSCORE', 'bb:q:' .. t, l) then return 0 end
redis.call('ZADD', 'bb:q:' .. t, ARGV[3], l)
redis.call('SADD', 'bb:numtpl:' .. n, t)     -- a non-empty room is always listed on its number
-- due when the lead is (an earlier entry stays): match rewrites it on a v2 number, and a
-- v2_pending or draining number keeps it for when its mode is v2
redis.call('ZADD', 'bb:due', 'LT', ARGV[3], n)
return v2_match(n, tonumber(ARGV[4]))
"""

# ARGV: number_id, cap
MATCH_LUA = _MATCH_FN + "return v2_match(ARGV[1], tonumber(ARGV[2]))"

# Returns the lease of lead l on number n only if it still carries ticket id tk (rule 15).
_LEASE_FN = """
local function lease_if(n, l, tk)
  local v = redis.call('HGET', 'bb:inflight:' .. n, l)
  if not v then return nil end
  local o = cjson.decode(v)
  if tostring(o.tk) ~= tk then return nil end
  return o
end

local function redis_now_ms()
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end
"""

# ARGV: number_id, lead_id, ticket, owner -> 1 = the owner holds the ticket, 0 = not ours.
# The first owner to claim a live ticket (rule 15) holds it until the line is given back or
# the lease cleared; claiming again with the same owner answers 1 (the retry after a lost
# reply). A ticket delivered twice (a reaper re-push racing the first delivery) lets
# exactly one coroutine through (design card rule 49). O(1).
CLAIM_LUA = _LEASE_FN + """
local o = lease_if(ARGV[1], ARGV[2], ARGV[3])
if not o then return 0 end
if o.owner then
  if o.owner == ARGV[4] then return 1 end
  return 0
end
o.owner = ARGV[4]
o.claimed_ms = redis_now_ms()
redis.call('HSET', 'bb:inflight:' .. ARGV[1], ARGV[2], cjson.encode(o))
return 1
"""


class GiveBack(IntEnum):
    """``return_line``'s refusals; a reply >= 0 is the tickets issued."""

    NOT_OURS = -1  # the lease is not this ticket's or this owner's: nothing changed
    # dialling, and the provider didn't say "not placed": a call may exist, so the line
    # stays held (rule 26)
    DIALLING = -3


# ARGV: number_id, lead_id, ticket, owner, not_placed ('1'|'0'), cap
# -> tickets issued, or a ``GiveBack`` refusal.
RETURN_LINE_LUA = _MATCH_FN + _LEASE_FN + """
local o = lease_if(ARGV[1], ARGV[2], ARGV[3])
if not o or o.owner ~= ARGV[4] then return -1 end   -- GiveBack.NOT_OURS
if o.dialling_ms and ARGV[5] ~= '1' then return -3 end  -- GiveBack.DIALLING
redis.call('SREM', 'bb:busy:' .. ARGV[1], 'lead:' .. ARGV[2])
redis.call('HDEL', 'bb:inflight:' .. ARGV[1], ARGV[2])
return v2_match(ARGV[1], tonumber(ARGV[6]))
"""

# ARGV: number_id, lead_id, ticket, owner ('' = the lease reaper, which owns every old lease)
CLEAR_LEASE_LUA = _LEASE_FN + """
local o = lease_if(ARGV[1], ARGV[2], ARGV[3])
if not o or (ARGV[4] ~= '' and o.owner ~= ARGV[4]) then return 0 end
return redis.call('HDEL', 'bb:inflight:' .. ARGV[1], ARGV[2])
"""

# ARGV: number_id, holder, cap
RELEASE_LUA = _MATCH_FN + """
local removed = redis.call('SREM', 'bb:busy:' .. ARGV[1], ARGV[2])
return {removed, v2_match(ARGV[1], tonumber(ARGV[3]))}
"""

# ARGV: number_id, lead_id, ticket, owner -> 1 = Mark.DIAL: go ahead and dial (a lease
# this owner already marked answers 1: the retry after a lost reply, rule 36);
# 2 = Mark.SUPERSEDED: the lead's lease is a newer ticket's (the reaper freed ours and
# re-issued it) or another owner's (the same ticket id after a Redis flush reset seq);
# 0 = Mark.REFUSED: no lease left, the owner never claimed it, or the kill switch is on
# (checked at the commit point, rule 24). O(1).
MARK_DIALLING_LUA = _LEASE_FN + """
if redis.call('GET', 'bb:dispatch:enabled') == '0' then return 0 end
local v = redis.call('HGET', 'bb:inflight:' .. ARGV[1], ARGV[2])
if not v then return 0 end
local o = cjson.decode(v)
if tostring(o.tk) ~= ARGV[3] or (o.owner and o.owner ~= ARGV[4]) then return 2 end
if not o.owner then return 0 end
if o.dialling_ms then return 1 end
o.dialling_ms = redis_now_ms()
redis.call('HSET', 'bb:inflight:' .. ARGV[1], ARGV[2], cjson.encode(o))
return 1
"""

# ARGV: number_id, call_id
ADMIT_INBOUND_LUA = """
local busy = 'bb:busy:' .. ARGV[1]
local holder = 'call:' .. ARGV[2]
if redis.call('SISMEMBER', busy, holder) == 1 then return 1 end
local maxl = tonumber(redis.call('HGET', 'bb:num:' .. ARGV[1], 'max') or '0')
if redis.call('SCARD', busy) >= maxl then return 0 end
redis.call('SADD', busy, holder)
return 1
"""


class Reap(IntEnum):
    """``reap_lease``'s refusal; a reply >= 0 is the tickets issued."""

    LEASE_CHANGED = -1  # gone, re-issued, or dialling and not allowed: nothing changed


# ARGV: number_id, lead_id, ticket, template_id ('' = don't re-queue), due_ms, cap, allow_dialling ('1'|'0')
# -> tickets issued, or a ``Reap`` refusal.
# Re-checks the lease INSIDE the script (race C): the reaper's earlier read may be stale.
REAP_LEASE_LUA = _MATCH_FN + _LEASE_FN + """
local n, l = ARGV[1], ARGV[2]
local o = lease_if(n, l, ARGV[3])
if not o then return -1 end                             -- Reap.LEASE_CHANGED
if o.dialling_ms and ARGV[7] ~= '1' then return -1 end  -- Reap.LEASE_CHANGED
redis.call('HDEL', 'bb:inflight:' .. n, l)
redis.call('SREM', 'bb:busy:' .. n, 'lead:' .. l)
if ARGV[4] ~= '' then
  local t = ARGV[4]
  redis.call('ZADD', 'bb:q:' .. t, ARGV[5], l)
  -- list the template on the number it is routed to now, due when the lead is; it may
  -- have moved off n (Fable I2)
  local rn = redis.call('HGET', 'bb:route:' .. t, 'number')
  if rn and rn ~= '' then
    redis.call('SADD', 'bb:numtpl:' .. rn, t)
    redis.call('ZADD', 'bb:due', 'LT', ARGV[5], rn)
  end
end
return v2_match(n, tonumber(ARGV[6]))
"""

# ARGV: number_id, lead_id, ticket, older_than_ms, head_ms, head_number, head_ticket
# (the last three '' = bb:tickets was empty) -> 1 re-pushed, 0 not (claimed, gone,
# delivered less than older_than_ms ago, still waiting in bb:tickets, or the kill switch is
# off). A ticket popped by a pod that died before claiming it (or whose pop reply was lost)
# is delivered again, with its line still held, at the head: it is the oldest. The head_*
# describe the entry at the head of bb:tickets when the reaper's run began: the list is in
# issue order (match appends; this script and a stopping acceptor put the oldest back at
# the head), so a ticket issued before it has left the list, and one still in it (every
# pod at its in-flight guard) is never delivered twice (rule 52). One match run issues
# all its tickets with one issued_ms; among those, the number's ticket ids give the order,
# and a tie with another number's head counts as still waiting (the next run decides). claim lets one copy through; the lease remembers the
# re-push, so it goes out at most once per window. O(1).
REPUSH_TICKET_LUA = _LEASE_FN + """
if redis.call('GET', 'bb:dispatch:enabled') == '0' then return 0 end
local o = lease_if(ARGV[1], ARGV[2], ARGV[3])
if not o or o.owner or o.dialling_ms then return 0 end
local now = redis_now_ms()
if now - tonumber(o.repushed_ms or o.issued_ms) < tonumber(ARGV[4]) then return 0 end
if ARGV[5] ~= '' then
  local issued, head_ms = tonumber(o.issued_ms), tonumber(ARGV[5])
  if issued > head_ms then return 0 end
  if issued == head_ms and not (ARGV[6] == ARGV[1] and tonumber(ARGV[3]) < tonumber(ARGV[7])) then
    return 0
  end
end
o.repushed_ms = now
redis.call('HSET', 'bb:inflight:' .. ARGV[1], ARGV[2], cjson.encode(o))
redis.call('LPUSH', 'bb:tickets', ARGV[1] .. '|' .. ARGV[2] .. '|' .. ARGV[3] .. '|' .. o.t .. '|' .. o.issued_ms)
return 1
"""

# ARGV: number_id, lead_id, cap  — ledger check (PoC fix 4e5862a7): "no lease" and the removal
# are one atomic step, so a ticket issued between the check's read and its removal is safe.
RELEASE_STALE_LUA = _MATCH_FN + """
if redis.call('HEXISTS', 'bb:inflight:' .. ARGV[1], ARGV[2]) == 1 then return 0 end
local removed = redis.call('SREM', 'bb:busy:' .. ARGV[1], 'lead:' .. ARGV[2])
v2_match(ARGV[1], tonumber(ARGV[3]))
return removed
"""

# Hands a room back to today's dialler: every lead goes to today's schedule with its due
# time. ARGV: template_id, chunk -> leads moved by this call (< chunk: the room is now
# empty). Each chunk reads and removes in one step, so a lead enqueued meanwhile is never
# lost between the read and the delete; one enqueued between chunks is moved by a later one.
# A chunk per call, so a big pile never blocks Redis in one script (review #1287 finding 6:
# 100k leads took 198 ms); the caller loops. An emptied sorted set is removed by Redis.
MOVE_ROOM_LUA = """
local room = 'bb:q:' .. ARGV[1]
local n = tonumber(ARGV[2])
local items = redis.call('ZRANGE', room, 0, n - 1, 'WITHSCORES')
for i = 1, #items, 2 do redis.call('ZADD', '%s', items[i + 1], items[i]) end
if #items > 0 then redis.call('ZREMRANGEBYRANK', room, 0, #items / 2 - 1) end
return #items / 2
""" % SCHEDULE_ZSET
MOVE_ROOM_CHUNK = 1000

# ARGV: number_id, expected mode, expected mode_since_ms, ops (JSON [[cmd, key, args...], ...])
# -> 1 applied; 0 the number's mode moved on since the caller read it (nothing written).
# A switch step writes only if bb:num:{N} still has the mode and mode_since_ms its decision
# was made on (absent = legacy / 0): a step of a deposed sweep leader, or one overtaken by
# a newer step, changes nothing (review #1287 finding 3). Every write of the step is in
# ``ops`` and happens in this one script, all or none.
SWITCH_CAS_LUA = """
local cur = redis.call('HMGET', 'bb:num:' .. ARGV[1], 'mode', 'mode_since_ms')
local mode, since = cur[1], cur[2]
if not mode or mode == '' then mode = 'legacy' end
if not since or since == '' then since = '0' end
if mode ~= ARGV[2] or since ~= ARGV[3] then return 0 end
local ops = cjson.decode(ARGV[4])
local allowed = {HSET = true, HDEL = true, SADD = true, SREM = true, ZREM = true, UNLINK = true}
for _, op in ipairs(ops) do
  if not allowed[op[1]] then return redis.error_reply('switch_cas: op not allowed') end
end
for _, op in ipairs(ops) do redis.call(unpack(op)) end
return 1
"""

_HOURS_TEST_LUA = _HOURS_FN + """
if hours_open(tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])) then return 1 end
return 0
"""


_T = TypeVar("_T")


@lru_cache(maxsize=None)
def _sha(script: str) -> str:
    return hashlib.sha1(script.encode()).hexdigest()


async def _run(script: str, args: list, parse: Callable[[Any], _T]) -> Optional[_T]:
    """Run ``script`` once and parse its reply; None on a Redis error or a malformed
    reply (``_run_each``)."""
    return (await _run_each(script, [args], parse))[0]


async def _run_each(
    script: str, argvs: Sequence[list], parse: Callable[[Any], _T]
) -> List[Optional[_T]]:
    """Run ``script`` once per argv in one round trip and parse each reply: None for a
    script that failed or a malformed reply, the others still ran. Logs the error, never
    the script text."""
    if not argvs:
        return []
    rows = [[str(a) for a in argv] for argv in argvs]
    try:
        replies = await _eval_each(await v2_redis(), script, rows)
    except Exception as e:  # noqa: BLE001 — R-ERR: never raise into the dispatch path
        logger.error(f"v2 script failed: {type(e).__name__}: {e}")
        return [None] * len(rows)
    parsed: List[Optional[_T]] = []
    for reply in replies:
        try:
            if isinstance(reply, Exception):
                raise reply
            parsed.append(None if reply is None else parse(reply))
        except Exception as e:  # noqa: BLE001 — one script's failure is its own
            logger.error(f"v2 script failed: {type(e).__name__}: {e}")
            parsed.append(None)
    return parsed


async def _eval_each(client: Any, script: str, argvs: List[List[str]]) -> List[Any]:
    """``script`` once per argv, sent by its SHA1; those Redis answered NOSCRIPT (they did
    not run) are sent once more with the body, which also leaves it in Redis's cache.
    Each reply is the script's result or its error reply; a lost connection raises."""
    replies = await _send(client, "EVALSHA", _sha(script), argvs)
    unknown = [i for i, r in enumerate(replies) if isinstance(r, NoScriptError)]
    if unknown:
        again = await _send(client, "EVAL", script, [argvs[i] for i in unknown])
        for i, reply in zip(unknown, again):
            replies[i] = reply
    return replies


async def _send(
    client: Any, command: str, body: str, argvs: List[List[str]]
) -> List[Any]:
    """One round trip of ``command`` (EVAL or EVALSHA) per argv: a plain command for one,
    else a pipeline. Not a transaction: each script runs on its own, so other clients'
    commands run between them."""
    if len(argvs) == 1:
        try:
            return [await client.execute_command(command, body, 0, *argvs[0])]
        except ResponseError as e:  # as a pipeline answers it
            return [e]
    async with client.pipeline(transaction=False) as pipe:
        for argv in argvs:
            pipe.execute_command(command, body, 0, *argv)
        return list(await pipe.execute(raise_on_error=False))


async def enqueue(
    template_id: str, lead_id: str, due_ms: int, only_if_absent: bool = False
) -> Optional[int]:
    """Tickets issued (>= 0), or an ``Enqueue`` refusal."""
    return await _run(
        ENQUEUE_LUA,
        [
            template_id,
            lead_id,
            due_ms,
            BB_V2_MATCH_CAP,
            "1" if only_if_absent else "0",
        ],
        int,
    )


async def enqueue_many(
    leads: Sequence[Tuple[str, str, int]], only_if_absent: bool = False
) -> List[Optional[int]]:
    """``enqueue`` for each (template_id, lead_id, due_ms) in one round trip, each its own
    script; a lead whose script failed answers None, the others still ran."""
    flag = "1" if only_if_absent else "0"
    argvs = [
        [t, lead_id, due_ms, BB_V2_MATCH_CAP, flag] for t, lead_id, due_ms in leads
    ]
    return await _run_each(ENQUEUE_LUA, argvs, int)


async def match(number_id: str) -> Optional[int]:
    return await _run(MATCH_LUA, [number_id, BB_V2_MATCH_CAP], int)


async def match_all(number_id: str) -> Optional[int]:
    """``match`` until a run issues less than the cap (spec 2026-10-05 §4.8): the cap
    bounds one script's run time, not a number, and each run is its own script, so other
    commands run in between. It ends because every ticket takes a free line. Total
    issued; None if the first run failed (a later failure stops the loop: the sweep
    goes on from there)."""
    total: Optional[int] = None
    while True:
        issued = await match(number_id)
        if issued is None:
            return total
        total = (total or 0) + issued
        if issued < BB_V2_MATCH_CAP:
            return total


@dataclass(frozen=True)
class Ticket:
    """One ``bb:tickets`` entry: a line on ``number_id`` reserved for ``lead_id``."""

    number_id: str
    lead_id: str
    tk: int
    template_id: str
    issued_ms: int


def parse_ticket(raw: Optional[str]) -> Optional[Ticket]:
    """A ``bb:tickets`` entry (``N|L|tk|T|issued_ms``), or None when it is not one."""
    parts = (raw or "").split("|")
    if len(parts) != 5:
        return None
    try:
        return Ticket(parts[0], parts[1], int(parts[2]), parts[3], int(parts[4]))
    except ValueError:
        return None


async def claim(number_id: str, lead_id: str, ticket: int, owner: str) -> bool:
    """True = ``owner`` holds the ticket. A lost reply is retried once with the same
    owner (the script answers its own owner's re-run with 1); a second failure counts as
    not ours, and if the script did run, the reaper's claimed tier frees the line."""
    args = [number_id, lead_id, ticket, owner]
    reply = await _run(CLAIM_LUA, args, int)
    if reply is None:
        reply = await _run(CLAIM_LUA, args, int)
    return bool(reply)


async def match_many(number_ids: List[str]) -> Dict[str, Optional[int]]:
    """``match`` on every number in one round trip, each its own short script. A number
    whose script failed answers None; the others still ran."""
    argvs = [[number_id, BB_V2_MATCH_CAP] for number_id in number_ids]
    return dict(zip(number_ids, await _run_each(MATCH_LUA, argvs, int)))


async def return_line(
    number_id: str, lead_id: str, ticket: int, owner: str, not_placed: bool = False
) -> Optional[int]:
    """Tickets issued (>= 0), or a ``GiveBack`` refusal."""
    return await _run(
        RETURN_LINE_LUA,
        [
            number_id,
            lead_id,
            ticket,
            owner,
            "1" if not_placed else "0",
            BB_V2_MATCH_CAP,
        ],
        int,
    )


async def release(number_id: str, holder: str) -> Optional[list[int]]:
    """[removed 0|1, tickets issued]."""
    return await _run(
        RELEASE_LUA,
        [number_id, holder, BB_V2_MATCH_CAP],
        lambda r: [int(r[0]), int(r[1])],
    )


class Mark(Enum):
    """``mark_dialling``'s answer: what the dispatch may do with its lead."""

    DIAL = "dial"  # marked: the request may go
    # A newer ticket (or another owner) holds the lead's lease: its lock and schedule
    # belong to that holder now.
    SUPERSEDED = "superseded"
    # Not marked (no lease left, the kill switch, Redis failed): give the line back,
    # unlock, re-queue.
    REFUSED = "refused"


_MARKS: Dict[Optional[int], Mark] = {1: Mark.DIAL, 2: Mark.SUPERSEDED}


async def mark_dialling(number_id: str, lead_id: str, ticket: int, owner: str) -> Mark:
    """The commit point before the provider request. A lost reply is retried once with
    the same owner (the script answers its own owner's re-mark with 1); a second failure
    is REFUSED: without a recorded dialling time we don't dial."""
    args = [number_id, lead_id, ticket, owner]
    reply = await _run(MARK_DIALLING_LUA, args, int)
    if reply is None:
        reply = await _run(MARK_DIALLING_LUA, args, int)
    return _MARKS.get(reply, Mark.REFUSED)


async def clear_lease(
    number_id: str, lead_id: str, ticket: int, owner: str = ""
) -> bool:
    """The lease goes, ``lead:<id>`` stays busy (the call holds the line). ``owner`` ''
    is the lease reaper, which may clear any lease that still carries ``ticket``."""
    return bool(await _run(CLEAR_LEASE_LUA, [number_id, lead_id, ticket, owner], int))


async def admit_inbound(number_id: str, call_id: str) -> Optional[bool]:
    return await _run(ADMIT_INBOUND_LUA, [number_id, call_id], lambda r: bool(int(r)))


async def reap_lease(
    number_id: str,
    lead_id: str,
    ticket: int,
    template_id: str,
    due_ms: int,
    allow_dialling: bool = False,
) -> Optional[int]:
    """Tickets issued; -1 the lease changed (nothing done). ``template_id=""`` = don't re-queue."""
    return await _run(
        REAP_LEASE_LUA,
        [
            number_id,
            lead_id,
            ticket,
            template_id,
            due_ms,
            BB_V2_MATCH_CAP,
            "1" if allow_dialling else "0",
        ],
        int,
    )


async def repush_ticket(
    number_id: str,
    lead_id: str,
    ticket: int,
    older_than_ms: int,
    head: Optional[Ticket],
) -> Optional[int]:
    """1 = the unclaimed ticket is on ``bb:tickets`` again, 0 = not (see the script).
    ``head``: the entry at the head of the list when the run began, None when the list
    was empty."""
    if head is None:
        where: List[Any] = ["", "", ""]
    else:
        where = [head.issued_ms, head.number_id, head.tk]
    return await _run(
        REPUSH_TICKET_LUA, [number_id, lead_id, ticket, older_than_ms, *where], int
    )


async def release_stale(number_id: str, lead_id: str) -> Optional[int]:
    """1 = ``lead:<id>`` removed (it had no lease), 0 = kept."""
    return await _run(RELEASE_STALE_LUA, [number_id, lead_id, BB_V2_MATCH_CAP], int)


async def move_room_to_schedule(template_id: str) -> Optional[int]:
    """Leads moved from the template's room to today's schedule, ``MOVE_ROOM_CHUNK`` per
    script call until the room is empty (it is then gone). None: a call failed; the
    leads moved before it stay moved, the rest stay in the room for the next try."""
    moved = 0
    while True:
        n = await _run(MOVE_ROOM_LUA, [template_id, MOVE_ROOM_CHUNK], int)
        if n is None:
            return None
        moved += n
        if n < MOVE_ROOM_CHUNK:
            return moved


async def switch_cas(
    number_id: str, mode: str, mode_since_ms: int, ops: List[List[str]]
) -> Optional[bool]:
    """Run a switch step's writes only if the number is still in ``mode`` since
    ``mode_since_ms``. True applied, False stale (nothing written), None a Redis error.
    """
    return await _run(
        SWITCH_CAS_LUA,
        [number_id, mode, int(mode_since_ms), json.dumps(ops)],
        lambda r: bool(int(r)),
    )


async def hours_open_for_test(start: int, end: int, sec: int) -> bool:
    r = await _run(_HOURS_TEST_LUA, [start, end, sec], int)
    if r is None:
        raise RuntimeError(
            "hours_open Lua failed"
        )  # a parity check must not pass silently
    return bool(r)
