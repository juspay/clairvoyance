# Track B — Human handoff

**What this track delivers**: a human can take over a run and hand it back: the `crm_handoff` table with its routes and spine letter, then the `handoff` node.

**Owner**: one agent, start to finish. Steps are strictly ordered; each is one PR with one commit, titled exactly as its file's *PR title* (it ends in `(enh B/NN)`). Open the next step's PR only after the previous one is MERGED into `origin/release`.

## Steps
1. 01 handoff table and API
2. 02 handoff node

## Files this track owns
new `app/crm/outreach/handoffs.py`, a migration, routes in `outreach/api.py`, one registry entry in `nodes.py`, the arrival-action hook in `walker.py`.

## Shared files with other tracks
Track A owns `nodes.py`'s `branches`/`listens` flags; B/02 adds `listens` if A/01 has not merged yet and says so. Rebase, not dependency.

## Before the first step
Read, in order: `CLAUDE.md`, `docs/crm/building-modules.md`, `docs/crm/migrations.md`, `docs/crm/workflow-enhancements/README.md` (the rules and the prompt), this file, every step file in this folder, and the sections of `docs/crm/workflow-rollout/context/reading-notes.md` the step files cite. Then read the current code of the files this track owns against those notes and report drift in one paragraph before writing code.

## Done when
Every step's PR is merged and `pytest tests/crm` is green on `release` afterwards. The PR list is the ledger (`is:pr "(enh B/"`); step files are never edited to say "done".
