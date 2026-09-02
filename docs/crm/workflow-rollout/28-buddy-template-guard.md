# Phase 28 — Buddy call-template retirement guard against pinned versions

**Kind**: fix (buddy) · **PR title**: `fix(buddy): refuse deleting a call template a live workflow version still names` · **Depends on**: 14 · **Notes**: phase 14 "Out of scope", `context/reading-notes.md` §14.7 · **Wave 7**

## Design
- Buddy's template delete path (`app/api/routers/breeze_buddy/templates…` — find the DELETE handler; `app/database/accessor/breeze_buddy/template.py` is in the legacy allowlist) gains a check through the hook pattern buddy already uses for CRM (`lead_call_tracker.register_created_hook` precedent): `register_template_retire_guard(fn)` in the accessor; `app/main.py` registers outreach's `runs_referencing_call_template(merchant_id, template_id) -> int` (a sibling of phase 14's channel-template guard: `definition->'nodes'` with `type='call' AND template_id=$x` across non-exited runs' pinned versions).
- Count > 0 → 409 with the count and the workflow ids; no guard registered → refuse (fail closed, same as phase 14).
- Boundary: `app/api` imports only `app.crm.outreach.contracts` (rule 4) and the data layer imports nothing from crm (hook), so the registration lives in `main.py`.

## Red tests
- Guard registered + count → 409; count 0 → delete proceeds; unregistered → refused with an error log.
