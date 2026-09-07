# Track F — Identity

**What this track delivers**: the staple carries a merged-away customer's free handles (needs a ruling), and the identity nits.

**Owner**: one agent, start to finish. Steps are strictly ordered; each is one PR with one commit, titled exactly as its file's *PR title* (it ends in `(enh F/NN)`). Open the next step's PR only after the previous one is MERGED into `origin/release`.

## Steps
1. 01 staple handles (needs Swaroop's ruling vs ADR 0021 — ask first; do 02 while waiting)
2. 02 identity nits

## Files this track owns
`app/crm/identity/{resolve,facts}.py`, one migration (trigram index).

## Shared files with other tracks
None.

## Before the first step
Read, in order: `CLAUDE.md`, `docs/crm/building-modules.md`, `docs/crm/migrations.md`, `docs/crm/workflow-enhancements/README.md` (the rules and the prompt), this file, every step file in this folder, and the sections of `docs/crm/workflow-rollout/context/reading-notes.md` the step files cite. Then read the current code of the files this track owns against those notes and report drift in one paragraph before writing code.

## Done when
Every step's PR is merged and `pytest tests/crm` is green on `release` afterwards. The PR list is the ledger (`is:pr "(enh F/"`); step files are never edited to say "done".
