# Track E — Platform suppression

**What this track delivers**: suppression answers per channel, plus the platform module's two hygiene nits.

**Owner**: one agent, start to finish. Steps are strictly ordered; each is one PR with one commit, titled exactly as its file's *PR title* (it ends in `(enh E/NN)`). Open the next step's PR only after the previous one is MERGED into `origin/release`.

## Steps
1. 01 suppression per channel
2. 02 platform nits

## Files this track owns
`app/crm/platform/*`, the dispatcher's `_gate` call site (one argument).

## Shared files with other tracks
Track D also edits `dispatch.py` (a different function). Rebase only.

## Before the first step
Read, in order: `CLAUDE.md`, `docs/crm/building-modules.md`, `docs/crm/migrations.md`, `docs/crm/workflow-enhancements/README.md` (the rules and the prompt), this file, every step file in this folder, and the sections of `docs/crm/workflow-rollout/context/reading-notes.md` the step files cite. Then read the current code of the files this track owns against those notes and report drift in one paragraph before writing code.

## Done when
Every step's PR is merged and `pytest tests/crm` is green on `release` afterwards. The PR list is the ledger (`is:pr "(enh E/"`); step files are never edited to say "done".
