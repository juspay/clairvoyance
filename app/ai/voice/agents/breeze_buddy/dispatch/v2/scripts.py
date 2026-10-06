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
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

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

# Ranks. On a number whose bb:num:{N}.ranked is 1, a lead that is ready now waits at a
# score below zero, (rank - 100) * 10^13 + t with rank 1..99 and t < 10^13:
#   first ready first: the ms it became ready; with live_day, (100000 - IST day) * 10^8 +
#   ms of that day (a newer day first). Newest event first: (10^13 - 1) - the event's ms.
# A lead waiting for a later time keeps its due time as its score, as on an unranked
# number; bb:qp:{T} remembers its ready score until match gives it back (promote).
# redis.call sends a number exactly; `..`, tostring and cjson keep 14 digits and a ready
# score has 15, so as text it goes through string.format('%.0f').
# A lead may carry a next-day rank (a live lead not called by closing is pile the next
# day): it is live only on the IST day of its event. bb:qn:{T} remembers its next-day
# score and bb:qnd:{T} that day; from the next day match gives it that score (roll), and
# a lead queued after its live day takes the next rank at once.
PROMOTE_CAP = 5000  # due leads given their ready score, per template per match run

_RANK_FN = (
    "local BAND, IST_OFFSET_MS, PROMOTE_CAP = 10000000000000, %d, %d\n"
    % (
        IST_OFFSET_S * 1000,
        PROMOTE_CAP,
    )
    + """
local function rank_or(v, fallback)  -- a rank 1..99, else fallback
  local r = tonumber(v)
  if not r or r < 1 then return fallback end
  return math.min(math.floor(r), 99)
end

local function pscore(rank, order, ready_ms, event_ms, live_day)
  local t = ready_ms
  if order == 'n' then
    t = (BAND - 1) - event_ms
  elseif live_day then
    local ist = ready_ms + IST_OFFSET_MS
    t = (100000 - math.floor(ist / 86400000)) * 100000000 + ist % 86400000
  end
  return (rank - 100) * BAND + math.max(0, math.min(BAND - 1, math.floor(t)))
end

local function ist_day(ms) return math.floor((ms + IST_OFFSET_MS) / 86400000) end

local function put_ranked(t, l, due_ms, now_ms, rank, order, event_ms, live_day, nrank, norder)
  if nrank then
    local ready = math.max(due_ms, now_ms)
    local day = ist_day(event_ms > 0 and event_ms or ready)
    if day < ist_day(now_ms) then
      rank, order = nrank, norder  -- its live day is over
    else
      redis.call('HSET', 'bb:qn:' .. t, l,
                 string.format('%.0f', pscore(nrank, norder, ready, event_ms, live_day)))
      redis.call('ZADD', 'bb:qnd:' .. t, day, l)
    end
  end
  if due_ms <= now_ms then
    redis.call('ZADD', 'bb:q:' .. t, pscore(rank, order, now_ms, event_ms, live_day), l)
    redis.call('HDEL', 'bb:qp:' .. t, l)
  else
    redis.call('ZADD', 'bb:q:' .. t, due_ms, l)
    redis.call('HSET', 'bb:qp:' .. t, l,
               string.format('%.0f', pscore(rank, order, due_ms, event_ms, live_day)))
  end
  return order
end

-- The room's leads whose time has come get their ready score: the remembered one, else
-- the number's default rank at their due time. True = the cap was hit, more remain.
local function promote(t, room, now_ms, default_rank, live_day)
  local due = redis.call('ZRANGEBYSCORE', room, 0, now_ms, 'WITHSCORES', 'LIMIT', 0, PROMOTE_CAP)
  for i = 1, #due, 2 do
    local kept = redis.call('HGET', 'bb:qp:' .. t, due[i])
    local ps = kept and tonumber(kept) or pscore(default_rank, 'f', tonumber(due[i + 1]), 0, live_day)
    redis.call('ZADD', room, ps, due[i])
    redis.call('HDEL', 'bb:qp:' .. t, due[i])
  end
  return #due / 2 >= PROMOTE_CAP
end

-- The room's leads whose live day is over take their next-day score; a lead that left
-- the room is only forgotten. True = the cap was hit, more remain.
local function roll(t, room, now_ms)
  local old = redis.call('ZRANGEBYSCORE', 'bb:qnd:' .. t, '-inf', ist_day(now_ms) - 1,
                         'LIMIT', 0, PROMOTE_CAP)
  for _, l in ipairs(old) do
    local ns = redis.call('HGET', 'bb:qn:' .. t, l)
    local cur = tonumber(redis.call('ZSCORE', room, l))
    if ns and cur and cur < 0 then
      redis.call('ZADD', room, tonumber(ns), l)
    elseif ns and cur then
      redis.call('HSET', 'bb:qp:' .. t, l, ns)  -- still waiting for its time
    end
    redis.call('ZREM', 'bb:qnd:' .. t, l)
    redis.call('HDEL', 'bb:qn:' .. t, l)
  end
  return #old >= PROMOTE_CAP
end
"""
)

# match keeps its number's bb:due entry (design card rule 55): the time it can next issue.
_MATCH_FN = _HOURS_FN + _PAUSED_PREFIX_LUA + _RECHECK_LUA + _RANK_FN + """
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
  -- ranked number: read here, after the full check, so a full number pays nothing
  -- backfill: leads queued before N was ranked are getting their rows' ranks
  -- (reconcile.backfill_ranks); until then nobody is promoted to the default rank
  local rk = redis.call('HMGET', num, 'ranked', 'live_day', 'default_rank', 'backfill')
  local more_to_promote = false
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
      if rk[1] == '1' and rk[4] ~= '1'
         and promote(tid, room, now_ms, rank_or(rk[3], 1), rk[2] ~= '0') then
        more_to_promote = true
      end
      if rk[1] == '1' and roll(tid, room, now_ms) then more_to_promote = true end
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
      local lease, list = {t = best.t, issued_ms = now_ms, tk = tk}, 'bb:tickets'
      local entry = n .. '|' .. best_l .. '|' .. tk .. '|' .. best.t .. '|' .. now_ms
      -- A call with no lead row yet (bb:qi; N's intents flag only stops new ones, so
      -- it is not read here): its line waits in bb:grants until the grant worker has
      -- made the row (PUBLISH); g = waiting for the row, r = its run, ps = the score
      -- it had, should it have to wait again.
      local run = redis.call('HGET', 'bb:qi:' .. best.t, best_l)
      if run then
        lease.g, lease.r, lease.ps = 1, run, string.format('%%.0f', best.s)
        list, entry = 'bb:grants', entry .. '|' .. run
      end
      redis.call('HSET', inflight, best_l, cjson.encode(lease))
      redis.call('RPUSH', list, entry)
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
  if more_to_promote then next_ms = now_ms end  -- the next tick promotes the rest
  if next_ms == nil then
    redis.call('ZREM', 'bb:due', n)  -- full (a freed line runs match) or nothing waits
  else
    redis.call('ZADD', 'bb:due', math.max(next_ms, now_ms), n)
  end
  return issued
end
""" % IST_OFFSET_S


class Rank(NamedTuple):
    """A lead's place on a ranked number: ``rank`` 1..99, lowest first (0 = the lead has
    none: the number's default rank); ``order`` "f" first ready first, or "n" newest
    event first by ``event_ms``. ``next_rank`` (0 = none) and ``next_order``: the place
    it takes once the IST day of its event is over. A number that is not ranked ignores
    it."""

    rank: int
    order: str
    event_ms: int
    next_rank: int = 0
    next_order: str = "n"


def rank_from_priority(priority: Any) -> Rank:
    """The rank kept on a lead row (``meta_data.priority`` = {rank, order, event_ms and,
    optionally, next_rank, next_order}); ``Rank(0, "f", 0)`` when the row carries none.
    """
    try:
        rank = int(priority["rank"])
        if rank < 1:
            raise ValueError(rank)
        order = "n" if priority.get("order") in ("n", "newest_event") else "f"
        next_order = "f" if priority.get("next_order") in ("f", "first_ready") else "n"
        return Rank(
            rank,
            order,
            int(priority.get("event_ms") or 0),
            int(priority.get("next_rank") or 0),
            next_order,
        )
    except (KeyError, TypeError, ValueError):
        return Rank(0, "f", 0)


def _rank_argv(rank: Optional[Rank]) -> List[Any]:
    """rank, order, event_ms, next_rank ('' = none), next_order."""
    if rank is None:
        return [""] * 5
    return [*rank[:3], rank.next_rank or "", rank.next_order]


def _enqueue_rank_argv(rank: Optional[Rank], run_id: str = "") -> List[Any]:
    """ENQUEUE's ARGV 6-11: the rank, the run id (ARGV 9), the next-day rank."""
    r = _rank_argv(rank)
    return [*r[:3], run_id, *r[3:]]


class Enqueue(IntEnum):
    """``enqueue``'s refusals; a reply >= 0 is the tickets issued."""

    ROUTE_MISSING = -1  # no bb:route:{T}: resolve it and retry (rule 18)
    HOLDS_LINE = -2  # the lead holds a line on N: its holder re-queues it (rule 17)
    NOT_V2 = -3  # N is not v2-accounted, nothing written: today's schedule
    NEED_RANK = -4  # N is ranked and no rank was given, nothing written: read it, retry


# ARGV: template_id, lead_id, due_ms, cap, only_if_absent ('1'|'0'),
#       rank ('' not given | '0' none | '1'..'99'), order ('f'|'n'), event_ms (ranked numbers),
#       run_id ('' = the lead has a row; else the member is the id its lead WILL have and
#       bb:qi:{T} remembers the run: only on a number whose bb:num:{N}.intents is 1),
#       next_rank ('' none), next_order
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
local nf = redis.call('HMGET', 'bb:num:' .. n, 'mode', 'ranked', 'live_day', 'intents')
local mode = nf[1]
if mode ~= 'v2_pending' and mode ~= 'v2' and mode ~= 'draining' then return -3 end  -- NOT_V2
local run = ARGV[9] or ''
-- NOT_V2: N takes no rowless calls, or is switching (its rooms may go to today's
-- schedule, which needs a lead row)
if run ~= '' and (nf[4] ~= '1' or mode ~= 'v2') then return -3 end
-- Enqueue.HOLDS_LINE
if redis.call('SISMEMBER', 'bb:busy:' .. n, 'lead:' .. l) == 1 then return -2 end
if redis.call('HEXISTS', 'bb:inflight:' .. n, l) == 1 then return -2 end
if ARGV[5] == '1' and redis.call('ZSCORE', 'bb:q:' .. t, l) then return 0 end
if nf[2] == '1' and ARGV[6] ~= '0' then
  local rank = rank_or(ARGV[6], nil)
  if not rank then return -4 end               -- Enqueue.NEED_RANK
  put_ranked(t, l, tonumber(ARGV[3]), (now_ms_and_ist()), rank, ARGV[7],
             tonumber(ARGV[8]) or 0, nf[3] ~= '0', rank_or(ARGV[10], nil), ARGV[11])
else
  -- unranked number, or a lead with no rank ('0'): promote gives it the default rank
  redis.call('ZADD', 'bb:q:' .. t, ARGV[3], l)
end
if run ~= '' then redis.call('HSET', 'bb:qi:' .. t, l, run) end
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
if not o or o.g then return 0 end  -- g: no lead row yet, so no ticket was written
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


# ARGV: number_id, lead_id, ticket, template_id ('' = don't re-queue), due_ms, cap, allow_dialling ('1'|'0'),
#       rank, order, event_ms, next_rank, next_order (as ENQUEUE's; without a rank the
#       lead is re-queued at its due time)
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
  -- list the template on the number it is routed to now, due when the lead is; it may
  -- have moved off n (Fable I2)
  local rn = redis.call('HGET', 'bb:route:' .. t, 'number')
  local rank = rn and rank_or(ARGV[8], nil)
  local nf = rank and redis.call('HMGET', 'bb:num:' .. rn, 'ranked', 'live_day')
  if nf and nf[1] == '1' then
    put_ranked(t, l, tonumber(ARGV[5]), redis_now_ms(), rank, ARGV[9],
               tonumber(ARGV[10]) or 0, nf[2] ~= '0', rank_or(ARGV[11], nil), ARGV[12])
  else
    redis.call('ZADD', 'bb:q:' .. t, ARGV[5], l)
  end
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
if not o or o.owner or o.dialling_ms or o.g then return 0 end
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

# The four scripts of a call with no lead row yet (a lease marked g, its entry in bb:grants).

# ARGV: number_id, lead_id, ticket -> 1 = the lead row exists now and its ticket is on
# bb:tickets; 0 = the lease is gone or is no longer waiting (nothing written). The issue
# time is set to now, because bb:tickets is in issue order (REPUSH_TICKET, the monitor).
PUBLISH_LUA = _LEASE_FN + """
local n, l = ARGV[1], ARGV[2]
local o = lease_if(n, l, ARGV[3])
if not o or not o.g then return 0 end
o.g, o.r, o.ps, o.repushed_ms = nil, nil, nil, nil
o.issued_ms = redis_now_ms()
redis.call('HSET', 'bb:inflight:' .. n, l, cjson.encode(o))
redis.call('HDEL', 'bb:qi:' .. o.t, l)
redis.call('RPUSH', 'bb:tickets', n .. '|' .. l .. '|' .. ARGV[3] .. '|' .. o.t .. '|' .. o.issued_ms)
return 1
"""

# ARGV: number_id, lead_id, ticket, older_than_ms, max_ms, cap -> for a lease still
# waiting for its row: 2 = older than max_ms: the line is freed and the member waits
# again at the score it had (unless it was withdrawn); 1 = its entry is on bb:grants
# again (not sent for older_than_ms); 0 = nothing (too young, or not such a lease).
REGRANT_LUA = _MATCH_FN + _LEASE_FN + """
local n, l = ARGV[1], ARGV[2]
local o = lease_if(n, l, ARGV[3])
if not o or not o.g then return 0 end
local now = redis_now_ms()
if now - o.issued_ms >= tonumber(ARGV[5]) then
  redis.call('HDEL', 'bb:inflight:' .. n, l)
  redis.call('SREM', 'bb:busy:' .. n, 'lead:' .. l)
  if redis.call('HEXISTS', 'bb:qi:' .. o.t, l) == 1 then
    redis.call('ZADD', 'bb:q:' .. o.t, tonumber(o.ps), l)
  end
  v2_match(n, tonumber(ARGV[6]))
  return 2
end
if now - (o.repushed_ms or o.issued_ms) < tonumber(ARGV[4]) then return 0 end
o.repushed_ms = now
redis.call('HSET', 'bb:inflight:' .. n, l, cjson.encode(o))
redis.call('LPUSH', 'bb:grants', n .. '|' .. l .. '|' .. ARGV[3] .. '|' .. o.t .. '|' .. o.issued_ms .. '|' .. o.r)
return 1
"""

# ARGV: template_id, lead_id -> 1 = it was waiting in the room. A lease it already holds
# is the grant worker's to give back (the CRM refuses the call).
WITHDRAW_LUA = """
redis.call('HDEL', 'bb:qp:' .. ARGV[1], ARGV[2])
redis.call('HDEL', 'bb:qi:' .. ARGV[1], ARGV[2])
return redis.call('ZREM', 'bb:q:' .. ARGV[1], ARGV[2])
"""

# ARGV: template_id, lead_id, rank, order, event_ms, next_rank, next_order -> 1 = a member
# still in its room on a ranked number has its new rank (ready: scored now; waiting:
# remembered), else 0. A ready member that stays in its rank, first ready first, keeps
# its place: ranking it again is not a new arrival.
RERANK_LUA = _MATCH_FN + """
local t, l = ARGV[1], ARGV[2]
local s, rank = tonumber(redis.call('ZSCORE', 'bb:q:' .. t, l)), rank_or(ARGV[3], nil)
local n = s and rank and redis.call('HGET', 'bb:route:' .. t, 'number')
local nf = n and redis.call('HMGET', 'bb:num:' .. n, 'ranked', 'live_day')
if not nf or nf[1] ~= '1' then return 0 end
local now = now_ms_and_ist()
local order = put_ranked(t, l, math.max(s, 0), now, rank, ARGV[4], tonumber(ARGV[5]) or 0,
                         nf[2] ~= '0', rank_or(ARGV[6], nil), ARGV[7])
local ns = tonumber(redis.call('ZSCORE', 'bb:q:' .. t, l))
if s < 0 and order ~= 'n' and math.floor(ns / BAND) == math.floor(s / BAND) then
  redis.call('ZADD', 'bb:q:' .. t, s, l)
end
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
# A ranked number's ready score (below zero) is handed back as "now": today's promoter
# reads scores from 0 to now. The remembered scores go with the room's last chunk.
MOVE_ROOM_LUA = """
local room = 'bb:q:' .. ARGV[1]
local n = tonumber(ARGV[2])
local items = redis.call('ZRANGE', room, 0, n - 1, 'WITHSCORES')
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
for i = 1, #items, 2 do
  redis.call('ZADD', '%s', tonumber(items[i + 1]) < 0 and now or items[i + 1], items[i])
end
if #items > 0 then redis.call('ZREMRANGEBYRANK', room, 0, #items / 2 - 1) end
if #items / 2 < n then
  redis.call('UNLINK', 'bb:qp:' .. ARGV[1], 'bb:qn:' .. ARGV[1], 'bb:qnd:' .. ARGV[1])
end
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
    template_id: str,
    lead_id: str,
    due_ms: int,
    only_if_absent: bool = False,
    rank: Optional[Rank] = None,
    run_id: str = "",
) -> Optional[int]:
    """Tickets issued (>= 0), or an ``Enqueue`` refusal. ``run_id``: the lead has no row
    yet; ``lead_id`` is the id it will have (an intents number only)."""
    return await _run(
        ENQUEUE_LUA,
        [
            template_id,
            lead_id,
            due_ms,
            BB_V2_MATCH_CAP,
            "1" if only_if_absent else "0",
            *_enqueue_rank_argv(rank, run_id),
        ],
        int,
    )


async def enqueue_many(
    leads: Sequence[Tuple[Any, ...]], only_if_absent: bool = False
) -> List[Optional[int]]:
    """``enqueue`` for each (template_id, lead_id, due_ms[, rank]) in one round trip, each
    its own script; a lead whose script failed answers None, the others still ran."""
    flag = "1" if only_if_absent else "0"
    argvs = [
        [
            *lead[:3],
            BB_V2_MATCH_CAP,
            flag,
            *_enqueue_rank_argv(lead[3] if lead[3:] else None),
        ]
        for lead in leads
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
    rank: Optional[Rank] = None,
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
            *_rank_argv(rank),
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


def parse_grant(raw: Optional[str]) -> Optional[Tuple[Ticket, str]]:
    """A ``bb:grants`` entry (a ticket's five fields, then the run id), or None."""
    head, _, run_id = (raw or "").rpartition("|")
    ticket = parse_ticket(head)
    return (ticket, run_id) if ticket and run_id else None


async def publish(number_id: str, lead_id: str, ticket: int) -> Optional[int]:
    """1 = the ticket is on ``bb:tickets``; 0 = the lease is gone or not waiting."""
    return await _run(PUBLISH_LUA, [number_id, lead_id, ticket], int)


async def regrant(
    number_id: str, lead_id: str, ticket: int, older_than_ms: int, max_ms: int
) -> Optional[int]:
    """2 = line freed, member waiting again; 1 = sent to ``bb:grants`` again; 0 = nothing."""
    args = [number_id, lead_id, ticket, older_than_ms, max_ms, BB_V2_MATCH_CAP]
    return await _run(REGRANT_LUA, args, int)


async def withdraw(template_id: str, lead_id: str) -> Optional[int]:
    """Take a waiting member out of its room (and forget its rank and its run)."""
    return await _run(WITHDRAW_LUA, [template_id, lead_id], int)


async def rerank(template_id: str, lead_id: str, rank: Rank) -> Optional[int]:
    """1 = a member still waiting on a ranked number has its new rank, else 0."""
    return await _run(RERANK_LUA, [template_id, lead_id, *rank], int)


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
