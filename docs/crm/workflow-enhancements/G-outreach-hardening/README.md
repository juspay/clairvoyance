# Track G — Outreach hardening

**What this track delivers**: tenancy FK on enrollments, redacted run reads, the buddy call-template guard, ops nits and two rulings.

**Owner**: one agent, start to finish. Steps are strictly ordered; each is one PR with one commit, titled exactly as its file's *PR title* (it ends in `(enh G/NN)`). Open the next step's PR only after the previous one is MERGED into `origin/release`.

## Steps
1. 01 enrollment customer FK (migration)
2. 02 run context redaction
3. 03 buddy template guard
4. 04 ops nits + rulings (N2 needs Swaroop)

## Files this track owns
`app/crm/outreach/{api,redaction}.py`, one migration, buddy's template delete path + `app/main.py` registration, `worker_main.py`.

## Shared files with other tracks
Track A also edits `outreach/api.py` (simulate route) — rebase only.

## Before the first step
Read, in order: `CLAUDE.md`, `docs/crm/building-modules.md`, `docs/crm/migrations.md`, `docs/crm/workflow-enhancements/README.md` (the rules and the prompt), this file, every step file in this folder, and the sections of `docs/crm/workflow-rollout/context/reading-notes.md` the step files cite. Then read the current code of the files this track owns against those notes and report drift in one paragraph before writing code.

## Done when
Every step's PR is merged and `pytest tests/crm` is green on `release` afterwards. The PR list is the ledger (`is:pr "(enh G/"`); step files are never edited to say "done".
