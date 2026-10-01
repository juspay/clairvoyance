"""
Redis key names for the event-driven dispatch module.

All keys are namespaced with ``bb:`` so they don't collide with other
Redis usage in the app.
"""

# Time-sorted set of leads waiting for their dispatch moment.
# score = next_attempt_at unix-ms, member = lead_id
SCHEDULE_ZSET = "bb:schedule:leads"

# FIFO lists of leads ready to dispatch right now, one per merchant tier.
# ``READY_LIST`` is the normal tier and the only list that existed before
# tiers; a lead with no tier hint lands there.
READY_LIST = "bb:ready:leads"
READY_LIST_HIGH = "bb:ready:leads:high"
READY_LIST_MEDIUM = "bb:ready:leads:medium"
READY_LISTS = (READY_LIST_HIGH, READY_LIST_MEDIUM, READY_LIST)

# Per-lead tier hint read by the promoter: value ``high`` or ``medium``.
# Written by ``schedule_lead`` when the merchant is in a tier list; absent
# means normal. TTL'd, so cancelled or finished leads need no cleanup.
LEAD_TIER_PREFIX = "bb:lead:tier:"  # + lead_id

# Per-worker reliability list. Workers RPUSH their in-flight lead_id here
# so the reaper can recover work from a dead worker.
PROCESSING_LIST_PREFIX = "bb:processing:leads:"  # + worker_uuid

# Per-worker liveness key. Refreshed every BB_WORKER_HEARTBEAT_REFRESH_S.
# TTL BB_WORKER_HEARTBEAT_TTL_S; absence => worker is presumed dead.
WORKER_HEARTBEAT_PREFIX = "bb:worker:heartbeat:"  # + worker_uuid

# Per-telephony-number capacity semaphore. LIST of opaque tokens; LLEN ==
# remaining free channels.
CHANNEL_PREFIX = "bb:channel:"  # + telephony_number_id

# Leader-election lock for the promoter. SET NX EX, value = pod instance id.
PROMOTER_LEADER = "bb:promoter:leader"

# Operational flags (set/unset by ops via runbook).
# Note: the global dispatcher kill switch lives in dynamic config
# (``BB_DISPATCH_ENABLED`` in app/core/config/dynamic.py), not as a raw Redis
# key — flip via DevCycle UI for instant rollback.
PROMOTER_PAUSED = "bb:promoter:paused"  # presence => promoter skips ticks
RESELLER_PAUSED_PREFIX = "bb:reseller:paused:"  # + reseller_id

# Alert throttling — TTL'd keys that suppress duplicate Slack pages while
# the underlying condition persists. Each alert kind has its own key.
ALERT_THROTTLE_PREFIX = "bb:alert:fired:"  # + alert_name

# Per-number, per-minute HyperLogLog of distinct leads waiting for a channel
# (``capacity_defer_seconds``). The number id is a ``{}`` hash tag so the
# two minute keys share a slot for the multi-key PFCOUNT on Redis Cluster.
CAPACITY_WAIT_PREFIX = "bb:capwait:"  # + {telephony_number_id}:minute_bucket


def processing_list_for(worker_uuid: str) -> str:
    return f"{PROCESSING_LIST_PREFIX}{worker_uuid}"


def worker_heartbeat_key(worker_uuid: str) -> str:
    return f"{WORKER_HEARTBEAT_PREFIX}{worker_uuid}"


def channel_key(telephony_number_id: str) -> str:
    return f"{CHANNEL_PREFIX}{telephony_number_id}"


def reseller_paused_key(reseller_id: str) -> str:
    return f"{RESELLER_PAUSED_PREFIX}{reseller_id}"


def alert_throttle_key(alert_name: str) -> str:
    return f"{ALERT_THROTTLE_PREFIX}{alert_name}"


def lead_tier_key(lead_id: str) -> str:
    return f"{LEAD_TIER_PREFIX}{lead_id}"


def capacity_wait_key(telephony_number_id: str, minute_bucket: int) -> str:
    return f"{CAPACITY_WAIT_PREFIX}{{{telephony_number_id}}}:{minute_bucket}"
