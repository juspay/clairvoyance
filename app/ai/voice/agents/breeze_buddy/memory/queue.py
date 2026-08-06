"""Crash-safe Redis queue for memory extraction jobs.

All keys share one Redis Cluster hash tag. Lua scripts atomically move job IDs
between scheduled and processing ZSETs while payloads remain in a hash until
acknowledged.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Protocol

from pydantic import ValidationError

from app.schemas.breeze_buddy.memory import MemoryExtractionJob
from app.services.redis.client import get_redis_service

_TAG = "{memory-extraction}"
PAYLOAD_HASH = f"memory:{_TAG}:payloads"
SCHEDULE_ZSET = f"memory:{_TAG}:scheduled"
PROCESSING_ZSET = f"memory:{_TAG}:processing"
LEASE_HASH = f"memory:{_TAG}:leases"
COMPLETED_ZSET = f"memory:{_TAG}:completed"
POISON_PREFIX = f"memory:{_TAG}:poison:"

COMPLETED_TTL_SECONDS = 7 * 24 * 60 * 60
POISON_TTL_SECONDS = 7 * 24 * 60 * 60

_ENQUEUE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', ARGV[4])
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1
  or redis.call('ZSCORE', KEYS[2], ARGV[1])
  or redis.call('ZSCORE', KEYS[3], ARGV[1])
  or redis.call('ZSCORE', KEYS[4], ARGV[1]) then
  return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
return 1
"""

_CLAIM_LUA = """
local stale = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', ARGV[1],
                         'LIMIT', 0, ARGV[3])
for _, id in ipairs(stale) do
  redis.call('ZREM', KEYS[3], id)
  redis.call('HDEL', KEYS[4], id)
  if redis.call('HEXISTS', KEYS[1], id) == 1 then
    redis.call('ZADD', KEYS[2], ARGV[1], id)
  end
end
local due = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1],
                       'LIMIT', 0, ARGV[2])
local result = {}
for _, id in ipairs(due) do
  if redis.call('ZREM', KEYS[2], id) == 1 then
    local payload = redis.call('HGET', KEYS[1], id)
    if payload then
      redis.call('ZADD', KEYS[3], ARGV[4], id)
      redis.call('HSET', KEYS[4], id, ARGV[5])
      table.insert(result, id)
      table.insert(result, payload)
    end
  end
end
return result
"""

_ACK_LUA = """
if redis.call('HGET', KEYS[4], ARGV[1]) ~= ARGV[2] then
  return 0
end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
redis.call('ZADD', KEYS[3], ARGV[3], ARGV[1])
return 1
"""

_RETRY_LUA = """
if redis.call('HGET', KEYS[4], ARGV[1]) ~= ARGV[2] then
  return 0
end
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
return 1
"""

_POISON_LUA = """
if redis.call('HGET', KEYS[5], ARGV[1]) ~= ARGV[2] then
  return 0
end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[5], ARGV[1])
redis.call('SET', KEYS[4], ARGV[3], 'EX', ARGV[4])
return 1
"""


class MemoryQueueUnavailable(RuntimeError):
    pass


class RedisScriptRunner(Protocol):
    async def run_script(
        self, script: str, keys: List[str], args: List[Any]
    ) -> Any: ...


@dataclass(frozen=True)
class ClaimedMemoryJob:
    job_id: str
    claim_token: str
    raw_payload: str
    job: Optional[MemoryExtractionJob]
    validation_error: Optional[str] = None


def job_id_for(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


class MemoryQueue:
    def __init__(self, redis: RedisScriptRunner) -> None:
        self._redis = redis

    @classmethod
    async def create(cls) -> "MemoryQueue":
        return cls(await get_redis_service())

    async def enqueue(
        self, job: MemoryExtractionJob, *, due_at_ms: Optional[int] = None
    ) -> bool:
        job_id = job_id_for(job.idempotency_key)
        now_ms = _now_ms()
        result = await self._redis.run_script(
            _ENQUEUE_LUA,
            keys=[PAYLOAD_HASH, SCHEDULE_ZSET, PROCESSING_ZSET, COMPLETED_ZSET],
            args=[
                job_id,
                job.model_dump_json(),
                due_at_ms if due_at_ms is not None else now_ms,
                now_ms,
            ],
        )
        if result is None:
            raise MemoryQueueUnavailable("memory enqueue script failed")
        return bool(int(result))

    async def claim(
        self, limit: int, visibility_timeout_seconds: int
    ) -> list[ClaimedMemoryJob]:
        now_ms = _now_ms()
        claim_token = uuid.uuid4().hex
        result = await self._redis.run_script(
            _CLAIM_LUA,
            keys=[PAYLOAD_HASH, SCHEDULE_ZSET, PROCESSING_ZSET, LEASE_HASH],
            args=[
                now_ms,
                max(1, limit),
                max(1, limit),
                now_ms + max(1, visibility_timeout_seconds) * 1000,
                claim_token,
            ],
        )
        if result is None:
            raise MemoryQueueUnavailable("memory claim script failed")
        values = list(result)
        claims: list[ClaimedMemoryJob] = []
        for index in range(0, len(values), 2):
            job_id = _decode(values[index])
            raw = _decode(values[index + 1])
            try:
                job = MemoryExtractionJob.model_validate_json(raw)
                claims.append(ClaimedMemoryJob(job_id, claim_token, raw, job))
            except (ValidationError, ValueError) as error:
                claims.append(
                    ClaimedMemoryJob(
                        job_id,
                        claim_token,
                        raw,
                        None,
                        _safe_error(error),
                    )
                )
        return claims

    async def ack(self, job_id: str, claim_token: str) -> bool:
        result = await self._redis.run_script(
            _ACK_LUA,
            keys=[PAYLOAD_HASH, PROCESSING_ZSET, COMPLETED_ZSET, LEASE_HASH],
            args=[
                job_id,
                claim_token,
                _now_ms() + COMPLETED_TTL_SECONDS * 1000,
            ],
        )
        if result is None:
            raise MemoryQueueUnavailable("memory ack script failed")
        return bool(int(result))

    async def retry(
        self,
        job_id: str,
        claim_token: str,
        job: MemoryExtractionJob,
        due_at_ms: int,
    ) -> bool:
        result = await self._redis.run_script(
            _RETRY_LUA,
            keys=[PAYLOAD_HASH, SCHEDULE_ZSET, PROCESSING_ZSET, LEASE_HASH],
            args=[job_id, claim_token, job.model_dump_json(), due_at_ms],
        )
        if result is None:
            raise MemoryQueueUnavailable("memory retry script failed")
        return bool(int(result))

    async def poison(
        self,
        job_id: str,
        *,
        claim_token: str,
        attempt: int,
        error: str,
    ) -> bool:
        metadata = json.dumps(
            {
                "job_id": job_id,
                "attempt": attempt,
                "error": error[:500],
                "failed_at": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
        )
        result = await self._redis.run_script(
            _POISON_LUA,
            keys=[
                PAYLOAD_HASH,
                SCHEDULE_ZSET,
                PROCESSING_ZSET,
                f"{POISON_PREFIX}{job_id}",
                LEASE_HASH,
            ],
            args=[job_id, claim_token, metadata, POISON_TTL_SECONDS],
        )
        if result is None:
            raise MemoryQueueUnavailable("memory poison script failed")
        return bool(int(result))


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _safe_error(error: Exception) -> str:
    return f"{type(error).__name__}: invalid memory job payload"
