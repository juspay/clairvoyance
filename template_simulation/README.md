# template_simulation

Places simulated phone calls against a template — the real flow engine, the
real STT/TTS pipeline, no telephony — grades each call deterministically,
and (when something fails reproducibly) proposes a template patch and proves it.

## Run it

```bash
uv run python template_simulation/run.py                 # the template in templates/
uv run python template_simulation/run.py -t <path>       # one-off override
```

Drop a template JSON into `template_simulation/templates/` and the bare command picks
it up. Everything else — concurrency, turn/time caps, iterate on/off, which
GRID model plays which role — lives in `config.py`, the one file you edit.

## Files

| File | Job |
|---|---|
| `config.py` | The settings, plus the seam that turns a setting into an LLM service, plus the shared GRID retry. |
| `suite.py` | **What gets tested**: the data model, and the cases derived from the template's own structure. Personas are the only LLM-authored part. |
| `call.py` | **How one call runs**: the bot façade, the mocked boundary handlers, the buffer-backed transport, and the turn loop around the real pipeline. |
| `grade.py` | **How a call is judged**: deterministic assertions (the gate), the LLM judge (advisory), stability across repetitions, and the plain-English "why". |
| `iterate.py` | **How a failure becomes a fix**: propose a patch, re-run the full suite, accept only if it fixed something reproducibly and broke nothing. |
| `run.py` | CLI, orchestration, report, artifacts. Run this file. |

## The flow, end to end

```
template.json
   │  derive cases (pure Python: one per function + adversarial + tool)
   │  author personas (one GRID call each, fresh every run)
   ▼
run every case once, CALL_CONCURRENCY at a time ── real 8 kHz audio call
   │                                                 real STT / LLM / TTS
   │  re-run ONLY the failures, once, to tell real from unlucky
   ▼
verdict per case: CONFIRMED_PASS · CONFIRMED_FAIL · FLAKY
   │
   ├── gate passes ─────────────────────────────► done (exit 0)
   └── something failed reproducibly
          │  ask GRID for the smallest prose patch, with the transcript as evidence
          │  re-derive any persona whose source text the patch changed
          │  re-run the FULL suite on the candidate
          ▼
       accept iff a targeted failure became a pass and nothing regressed
       → iterated-<template>.json (the original file is never written)
```

## Cost model

A real-time audio call costs real seconds and real STT/TTS credit, so the
harness spends them deliberately:

- **Cases run in parallel** (`CALL_CONCURRENCY`, default 3). Each call builds
  its own STT/TTS websocket, its own recorder and its own per-run hook
  namespace, so concurrent calls cannot bleed into each other — verified on a
  real 3-way run: every recording contained only its own conversation, and
  the only agent/customer audio overlap was the 5.6s of deliberate barge-in in
  `B-hostile_interrupt`. Set it to 1 for a jitter-free fidelity pass.
- **Only failures are re-run.** Confirming a failure is worth a second call;
  confirming a pass is not. This is what separates a real defect from an
  unlucky sample, so it is not optional.
- **Hard caps per call** (`MAX_CUSTOMER_TURNS`, `MAX_CALL_SECONDS`). One
  observed adversarial persona ran 270s / 35 turns before these existed.
- **Iterate only escalates on a real failure.** A clean run never pays for the
  patch loop.

## Design invariants

- **Grounded assertions only.** Every expectation carries a `Grounding`
  citing the template field it was read from; an ungrounded field is dropped
  at grading time, not weakened. An earlier harness keyword-matched function
  descriptions to guess what a difficult caller "should" trigger and
  manufactured failures that were then "fixed" by degrading working prompts.
- **The judge never gates**, with one exception: a catastrophic (≤1/5)
  `policy_adherence` or `no_instruction_leak` score becomes a real critical
  assertion. An agent confirming a payment it never took is a safety bug, not
  rubric taste. Everything else in the rubric is advisory.
- **Stability over single runs.** Only `CONFIRMED_FAIL` may drive a patch;
  only `CONFIRMED_PASS → CONFIRMED_FAIL` is a regression. Flaky is reported
  and never acted on — treating noise as signal made an earlier loop a random
  walk.
- **Everything is re-derived from the template every run**, never loaded
  back. The `<template>.cases.json` a run writes is its own record for a
  person to read — hand-editing it has no effect on anything.
- **A patch that edits prose re-authors the personas written from it.**
  A case means "a customer behaving the way function F's description says must
  be answered by calling F". Measuring a candidate against personas written
  from the pre-patch text makes a correct fix score identically to no fix —
  which is exactly why the loop used to reject every patch it proposed.
- **Call termination matches telephony's real mechanism.** Production ends a
  call by queuing a real `EndFrame` on the pipeline task; the mocked handler
  does the same, rather than relying only on a flag the turn loop polls.
- **The original template file is never written.** Promotion is a human action.

## What this reaches into

Read-only, all real code: `template.*` (types, context, builder, hooks,
transformation_function, utils, vad), `agent.pipeline.build_pipeline`,
`agent.flow.{setup_flow_manager, prepare_initial_node}`, `llm/stt/tts`
factories, `tts.generate_audio` and `voice.tts.elevenlabs`,
`chat.turn_core.resolve_llm_configuration`, `template.generator.prompts`
(iterate only), `schemas.breeze_buddy.core.LeadCallTracker`,
`services.live_config.store.get_config`, plus pipecat / pipecat_flows.

`run.py` sets `.env`, `SKIP_KMS_DECRYPT`, `JWT_SECRET_KEY` and `JWT_ALGORITHM`
defaults at the very top, before any `app.*` import.


## Output

`simulation_run/<template>/<timestamp>/` — `summary.txt` (the whole run, in plain
language: per-case pass/fail, judge scores, and the iteration log if one
ran — read this first), one JSON + WAV per call under `cases/` and `audio/`
(the original eval — the template exactly as given).

A patch loop adds `iterated-<template>.json` (the accepted template — never
the original file), `patches/iteration-N.json` (every proposal, accepted or
not — the most useful thing to read when a loop makes no progress), and its
own `iteration-N/cases/` + `iteration-N/audio/` per round — every round
re-uses the same case ids and repetition numbers as the eval, so each
candidate's calls get their own subfolder instead of overwriting the
original run's (or an earlier round's) recordings.
