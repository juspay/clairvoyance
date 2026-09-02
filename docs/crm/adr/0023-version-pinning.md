# 0023 — Runs execute the definition version they entered under

**Status:** Proposed — needs Swaroop's sign-off before rollout phase 11 starts · **Date:** 2026-09-03

Lives here until Swaroop mirrors it into the corpus (`decisions/0023-…` beside
0022). Supersedes the "audit stamp, never an execution pin" sentence of
migration 057 and canon T19 §edits; T20's `workflow_version` becomes the
execution pin. Rollout: `docs/crm/workflow-rollout/` phases 11–14.

## Context

Canon T19 and migration 057 chose ONE live `definition` per plan: publish copies `draft` → `definition` in place, every run not yet past the change follows the new document, and `version` is "an AUDIT stamp ('she entered under v3'), never an execution pin". Safety comes from the publish validator, which BLOCKS the stranding edits: removing a node that open runs stand on, and changing the `entry` words while runs are open (`plans.validate_definition`, `occupied_nodes`, `live_entry`).

That choice is right for runs measured in minutes. A cart-recovery board lives a day at most and its squares are almost always empty; fixing a template name reaches every waiting run, which is a feature (`context/reading-notes.md` §14.5).

It fails for runs measured in weeks. A loan-onboarding board has a token on nearly every square at all times, so nearly every meaningful edit hits the stranding guard. The only exits are archive-and-eject every journey, or a second plan beside the first — which is why the funnel ships as five disposable clocks today (§13, §14.7). Every enterprise journey engine (Braze Canvas, SFMC Journey Builder; Temporal and Step Functions for code) pins in-flight runs to the version they entered under and lets new entrants take the new one (§14.4). This record reverses the 057 sentence and makes "edits reach everyone" an opt-in mode.

## Decision

1. **Every enrollment executes the definition version recorded in `crm_workflow_enrollment.workflow_version` at entry.** The column stops being an audit stamp and becomes the execution pin: the walker resolves the run's definition by `(workflow_id, workflow_version)` on every claim, never by reading `crm_workflow.definition`.
2. **Publish creates a new, immutable version row** (`crm_workflow_version`: merchant_id, workflow_id, version, definition, on_publish, published_by, published_at). A version a run references is never mutated. `crm_workflow.definition` stays the LATEST version's document, for the entry consumer and the console.
3. **A plan declares `on_publish: "pin"` (default) or `"migrate"`.**
   - `pin`: new entrants take vN+1; runs in flight finish vN. The occupied-node and entry-change refusals do not apply — a new version cannot strand anyone.
   - `migrate`: today's semantics as a mode — inside the publish atom every open run is re-pinned to vN+1, and the publish is allowed only when the existing stranding validator passes (occupied nodes kept, `entry` unchanged). Braze offers the same choice; a short board opts in to keep "fix a template name for every waiting run".
4. **The entry consumer reads twice.** Entries are evaluated against the LATEST version (new runs start on the newest document). Goals and `wait_event` listening are evaluated per OPEN RUN against that run's pinned version — a v3 run is ended by v3's goals and woken by v3's listening nodes even after v5 changed them. One consumer, two reads (§15.3); a `migrate` plan is the degenerate case where every open run is pinned to the latest.
5. **Old versions are retained while any non-exited run references them**, then swept by the walker pod's housekeeping after a retention window. The latest version is never swept.
6. **A template a retained version names may not be retired while runs reference that version.** Retire refuses with the count. The guard is registered from the composition root (the record/consumers.py inversion) so connectivity never imports outreach.

## Consequences

- **Storage:** `crm_workflow_version` (owner outreach, phase 11), backfilled from every live plan's current `definition` so existing runs resolve.
- **Walker:** reads the pinned definition by `(workflow_id, version)` with a small immutable-key cache; a missing version is an honest park, never a fallback to the live document (phase 12).
- **Entry consumer:** iterates the customer's open runs for goals and listening, the merchant's latest plans for entries; run-scoped cancel/resume builders so a v3 goal never touches a sibling v5 run (phase 13).
- **Tooling:** migrate-forward (move waiting runs from vN to vN+1 when their current node still exists and `entry` is unchanged — the stranding validator reused as a pure function), a per-version run count, the unreferenced-version sweep, the template-retirement guard (phase 14).
- **Guards demoted:** the occupied-node and entry-change refusals survive only as `migrate`-mode preconditions and as the migrate-forward check.
- **Canon text:** 057's comment cannot be edited (merged migrations are immutable) — this record supersedes it; T19 §edits ("edits reach everyone not yet past them") becomes the definition of `migrate` mode, not the law; T20's `workflow_version` column reads "execution pin".
- **Vocabulary:** one new word on the definition (`on_publish`), in code, validated at publish; a closed CHECK on the column that stores it (a status enum, law 11).

## Alternatives rejected

- **Keep blocking.** A three-week board would be unpublishable for most edits; the loan funnel stays clocks forever and every long journey pays the reporting-join tax (§14.2, §14.3).
- **Per-node versioning.** Too fine: a run would mix nodes from several versions and no document would describe what it is executing.
- **Copy the document into each run's context at entry.** Bloats every row with the whole plan, breaks "one document" for the console, and makes a version-wide fix impossible.

## Rollout

Phases 11–14 in `docs/crm/workflow-rollout/`: 11 storage + the `on_publish` word (no behaviour change), 12 the walker reads the pin (the first behaviour change), 13 the consumer's two reads, 14 migrate-forward, retention and the retirement guard. **Existing plans default to `pin`** — there are no long-running production plans yet, and a short plan that wants today's semantics declares `migrate` on its next publish. Phase 15–17 vocabulary (topic branching, multi-entry, facts on resume, the `stages` ladder) builds on the pin and folds the loan clocks into one board.
