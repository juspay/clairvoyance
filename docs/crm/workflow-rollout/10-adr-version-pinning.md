# Phase 10 — ADR: version pinning for in-flight runs

**Kind**: docs · **PR title**: `docs(crm): ADR — runs execute the definition version they entered under` · **Depends on**: nothing; **blocks 11–17** · **Notes**: §14.4, §14.5, §14.7, §15.3

## Why
Canon T19 / migration 057 says: one live `definition`; publish replaces it in place; every run not yet past the change follows the new document; `version` is "an AUDIT stamp, never an execution pin"; safety = the publish validator blocks stranding edits. That fits runs of minutes. For runs of weeks (loan onboarding) there is always someone on nearly every node, so most meaningful edits are refused and the only exits are archive-and-eject or a second plan. Every enterprise journey engine (Braze Canvas, SFMC Journey Builder, Temporal for code) pins in-flight runs to the version they entered under and lets new entrants take the new one. This ADR reverses the 057 sentence and makes "edits reach everyone" an opt-in mode.

## Deliverable
`docs/crm/adr/0022-version-pinning.md` (the corpus numbers ADRs 0001–0021; 0022 is the next; Swaroop mirrors it into the corpus site). Sections:
1. **Context** — the T19 choice, why it was right for 30-minute runs, the loan-funnel failure (cite §14.5).
2. **Decision** — (a) every enrollment executes the definition version recorded in `workflow_version` at entry; (b) publish creates a new immutable version row, never mutates a version a run references; (c) a plan declares `on_publish: "pin"` (default) or `"migrate"`; `migrate` re-pins every open run to the new version inside the publish atom and is allowed only when the existing stranding validator passes (occupied nodes kept, entry unchanged) — i.e. today's semantics become a mode; (d) the entry consumer evaluates ENTRIES against the latest version and GOALS/LISTENING against each open run's pinned version; (e) old versions are retained while any non-exited run references them, then swept; (f) a template (call or channel) named by a retained version may not be retired while runs reference it — refuse with the count.
3. **Consequences** — storage (`crm_workflow_version`), walker read path + cache, entry consumer redesign (iterate open runs), drain/migrate-forward tooling, the occupied-node/entry guards demoted to `migrate`-mode preconditions, canon text changes: 057's comment (cannot edit a merged migration — the ADR supersedes it), T19 §edits, T20 col `workflow_version` becomes "execution pin".
4. **Alternatives rejected** — keep blocking (loan board impossible); per-node versioning (too fine); copy-on-publish into each run's context (bloats every row, breaks "one document").
5. **Rollout** — phases 11–14 in this folder; no behaviour change until 12 lands; existing plans get `on_publish: migrate` semantics? **Decision: existing plans default to `pin`** — there are no long-running production plans yet; state it.

## Acceptance
- ADR merged; `docs/crm/building-modules.md` law list gains one line under law 10/11 area: "Runs execute their pinned version (ADR 0022); `migrate` is the opt-in". `docs/crm/migrations.md` ownership table gains the planned `crm_workflow_version` (owner outreach) marked "phase 11".
- No code.

## Decisions already made
- Default is `pin`. Migrate is opt-in and validator-gated.
- Version rows are immutable; a "fix a typo for everyone" on a pinned plan is a new version + migrate-forward (phase 14), never an in-place edit.
