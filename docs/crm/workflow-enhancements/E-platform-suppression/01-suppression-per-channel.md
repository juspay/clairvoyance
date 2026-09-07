# E/01 — Suppression probes by channel (P6)

**Track E · step 1** · **Kind**: feat · **PR title**: `feat(crm): is_suppressed answers per channel — a WhatsApp STOP no longer blocks voice (enh E/01)` · **Depends on**: nothing (the dispatcher's `_gate` passes `message.channel`; rollout 19 is not required) · **Notes**: §1 platform OBSERVATION, §11 P6

## Why
`platform_identity.is_suppressed` is one derived boolean over the whole `suppressions` map; the gate asks `is_suppressed(handles)` with no channel. A WhatsApp STOP (channel `whatsapp`) blocks email and voice too. Conservative today; wrong once channels multiply, and wrong in the other direction for `'*'`-less entries if someone later "fixes" it naively.

## Design
- Contract: `is_suppressed(handles, channel: Optional[str] = None)`. `None` keeps today's semantics (any live entry). With a channel: probe `suppressions ? $channel OR suppressions ? '*'` AND liveness of THAT entry — done in SQL (`jsonb` path + the same `until` predicate the 048 trigger uses), not by reading the map into Python, so the gate stays one indexed statement. Fail closed unchanged.
- Dispatcher `_gate` passes `message.channel`. Buddy's voice pre-checks (DND/blacklist) are unchanged (ADR 0010: voice stays outside the gate).
- 048's `is_suppressed` column stays as "any channel" for the console list.

## Red tests
- Query: channel form contains both `? $n` probes and the `until` predicate; no-channel form unchanged; logic still returns True on a DB error.
