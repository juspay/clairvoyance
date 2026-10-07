"""
Event-driven dispatch module for Breeze Buddy.

Replaces the cron-based `process_backlog_leads` with a Redis-backed
ZSET-as-schedule + leader-elected promoter + worker-pool design.

See docs/BACKLOG_DISPATCHER_REDESIGN.md for the architecture.
"""

# Imported for its side effect: registers the created-lead schedule hook.
from app.ai.voice.agents.breeze_buddy.dispatch import created_hook  # noqa: F401
