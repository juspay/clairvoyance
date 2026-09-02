# 99 — Backlog (not scheduled; each needs its own phase file before work starts)

From `context/reading-notes.md` §16.3 and `context/nits.md`.

| Item | What | Why deferred |
|---|---|---|
| G4 | List-shaped facts (`line_items`) never reach templates — producer scalar summary vs fire-time letter read vs per-plan extractor (manas's open question on #1041) | Needs a ruling from Swaroop on the direction |
| G5 | Generic nodes: `http` action (coupons, lender APIs), `condition` on a customer attribute / context fact, `split` (percentage) | Product scope; each is a NODE_TYPES entry + validator + walker test |
| G6 | Human handoff / task node | Depends on where tasks live (assist data layer, PR #963) |
| G10 | Dry-run / simulate a plan against a sample event | Validator + a `simulate` endpoint that walks without writing |
| G11 | Send pacing per merchant/channel (W8 in `channels.py`) | Needed at promo-day scale, not for pilot |
| — | Operator endpoint to re-drive quarantined events (phase 04 follow-up) | Ops convenience |
| — | Buddy call-template deletion guard against pinned versions (phase 14 covers channel templates only) | Legacy path |
| N1 | Type-string matching in `entry.py`/`pick_next` → NodeSpec flags | Cosmetic |
| N2 | Global (merchant NULL) templates allowed on call nodes — confirm intent | Needs a ruling |
| N3 | Two definitions of "goal after entry" — phase 06 unifies on `entered_event_at`; verify | Verify after 06 |
| N5/N6/N7 | Onboarding credential-before-atom; degraded doors cannot send; template webhook consumer (#1040 lands it) | Connectivity, other owners |
| N8–N17 | See `context/nits.md` | Hygiene |
| P3 | Identity staple: loser's handles not present in the payload stay unreachable — check ADR 0021 intent | Identity module, needs a ruling |
| P4 | `crm_workflow_enrollment.customer_id` has no composite FK to `crm_customer` | Migration; check canon T20 |
| P6 | Suppression gate ignores channel (any suppression blocks all channels) | Design question once channels multiply |
| P7 | Run context exposes every scalar payload key via GET runs | Console/privacy review |
| P8 | Ingest size cap reads only Content-Length | Minor |
| — | In-call WhatsApp (`send_whatsapp` builtin) | **Dropped by decision 2026-09-02; do not re-add without a new decision** |
