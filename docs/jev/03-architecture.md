# Integration architecture

This is how Jev should be wired so that each judgment site is a small, testable,
reviewable unit, so that a model or threshold change never needs a code hunt, and so
that the CRM laws hold without exceptions.

## One client, two doors

**Buddy side.** `app/services/typesafe/` holds the only HTTP client: an `httpx.AsyncClient`
with a persistent connection pool, a per-request timeout, the retry policy (408, 429,
5xx with jittered backoff and a total budget), and a circuit breaker that opens after
N consecutive failures and reports through `track_error`. Nothing else imports the
vendor endpoint. Configuration follows the existing cascade: the key from static
config, the model alias and per-site flags from dynamic config (Redis or DevCycle), and
template-level overrides where a site is template-scoped.

**CRM side.** CI rule 4 forbids `app/crm` from importing `app.ai`. Two placements are
lawful; the corpus should pick one:

1. A CRM module `app/crm/judgment/` with its own `contracts.py`
   (`classify_reply(...)`, `score_transcript(...)`), which imports the shared client from
   `app/services/typesafe/`. Other CRM modules import only the contract.
2. The classifier as a **record consumer** living outside `app/crm` and registered from
   `record/worker_main.py`, which is already the sanctioned async per-row extension
   point and already sits behind the import boundary.

Option 1 keeps the vocabulary in code inside the CRM where the laws are enforced;
option 2 needs no new module. Either way the consumer runs **after**
`consume_attributed_event` and therefore must emit a **new letter** rather than expect
outreach to re-read the old one.

## The question registry

Each judgment site is one file, named for what it decides, with four things in it and
nothing else:

```python
# app/ai/voice/agents/breeze_buddy/judgments/observer_questions.py

VERSION = "2026-09-20.1"          # bump on any wording or threshold change
MODEL = "jev-1.13.0"              # pin the served version, not the alias

QUESTIONS = {
    "is_machine": {"type": "noul", "instructions": "...", "criteria": {...}},
    "machine_kind": {"type": "choice", "instructions": "...", "criteria": {...}},
    ...
}

THRESHOLDS = {"hangup_once": 0.95, "hangup_two_turns": 0.85, "abuse_alert": 0.90}


def decide(answers: dict, history: list[dict]) -> ObserverPlan:
    """PURE. Gathered answers in, a plan out. No I/O."""
```

This is the CRM skeleton's *gather, decide (pure, returns a plan), apply* applied to a
model call, and it gives four properties for free:

- **Reviewable diff.** A wording change, a new option, a threshold move: one file, one
  reviewer, one version bump.
- **Golden tests.** `decide()` is tested with recorded answers; the model is never in
  the unit test. The evaluation harness in `scripts/jev_eval.py` is the seed of the
  golden set that runs on every model upgrade.
- **Re-cut without re-calling.** Raw answers are stored (below). When a threshold
  changes, the historical decisions are recomputed from stored answers, which the
  vendor recommends and which the cost makes irrelevant anyway.
- **Ids stay local.** Question ids are not sent to the model; the full question must be
  in `instructions`, and every option must have a description. The evaluation showed
  that undescribed options collapsed Bengali and Gujarati script into "hindi".

## Storage

One table, whichever side owns it, with the columns the analytics and backtests need:

```
judgment_result
  id, merchant_id NOT NULL, site TEXT, question_version TEXT, model TEXT,
  source_kind TEXT, source_id TEXT,          -- lead / call_sid / chat_session / event id
  answers JSONB,                              -- raw answers, never the state
  latency_ms INT, created_at TIMESTAMPTZ
  UNIQUE (merchant_id, site, source_kind, source_id, question_version)
```

Store answers, not state: the state is the transcript, which already lives where it
lives, and copying it doubles the PII surface. If the CRM owns the table it follows the
canon (`merchant_id` first in every unique index, a `TABLE_OWNERS` entry, a migration
with the next number).

## Shadow mode, then flip

Every site ships in shadow first: the judgment runs, the answer and the *current*
decision are both logged, and nothing changes for the customer. A per-site, per-merchant
dynamic flag flips a site from shadow to active. Agreement rate between the model and
the current mechanism is the first dashboard; the second is the confidence histogram,
which tells you where to put the threshold before any customer sees the change.

## Latency and rate budgets

Measured on 2026-09-19 from a developer machine in India to the API, which resolves to
an AWS US-West address:

| Measurement | Value |
|---|---|
| Server processing, from `x-envoy-upstream-service-time` | about 165 ms |
| Warm keep-alive request, p50 | about 380 ms |
| Cold connection including TLS | about 850 ms |

Consequences:

- **Never on the voice turn path.** No turn-end, no barge-in, no per-token decision.
  Those go through distillation (U20) or stay as they are.
- **Async sites** (observers, post-call, CRM consumer): no budget concern; use the
  connection pool and a 5 s timeout with park-and-retry semantics on the CRM side.
- **Request-path sites** (pre-call selection in lead push): 1.5 s timeout, fall back to
  the template default on timeout, exactly as the current Gemini path falls back.
- **Chat pre-turn triage**: accept about 400 ms before first token, or run it
  concurrently with the first model cycle and use it only to cancel or redirect.
- **Rate limits** at the time of the snapshot were 1,200 requests per minute and 250k
  tokens per second. Per-turn observers across many concurrent calls will exceed
  1,200 per minute at scale; batch all questions for a turn into one request, and
  confirm limits with the vendor before U1 leaves shadow.

## Failure policy per site

| Site | Policy | Reason |
|---|---|---|
| Voice observers, pre-call selection, chat triage | **Fail open**: on error or timeout behave as today | The voice path's existing law |
| Post-call verification, analytics, eval | Skip and retry later; never block the call record | Read-only |
| CRM reply classification | Emit `unclear` on error; the run continues exactly as today | `else` is today's behaviour |
| Suppression and consent writes | **Fail closed**: write only at or above the threshold; on error write nothing | Permission-adjacent |
| Ship-confidence | Band `unknown` on error, which ships as today | Advisory |

## Data handling

Transcripts and replies carry names, addresses and sometimes card or OTP fragments.
Before any state leaves the process: remove phone numbers and emails (the judgment
never needs them), truncate to the turns the question needs, and never send the lead
payload wholesale when a projection will do. Add the vendor to the processor list for
the same review Azure OpenAI, Gemini and Langfuse went through. Store answers, not
state.

## Jaggedness guardrails, applied

From the vendor's own limits list for the current model:

- **Literal reading.** Write criteria as concrete situations, not adjectives; include
  the negative case explicitly ("asking to call back later is NOT a do-not-call").
- **No arithmetic, counting or dates.** Digit counts, amounts, windows and ages are
  computed in code and, if needed, passed in as already-computed facts.
- **Indirection.** Ask for the state, map to the language in code. Ask for the final
  position, not "did they change their mind and what to".
- **Context rot.** Send the turns the question needs, not the whole call, unless the
  question is about the whole call.
- **Adversarial text.** It handled injection well in the evaluation; keep code-side
  invariants anyway (a `stop` never becomes a `confirm` by any path).
- **No cross-question invariants.** Pick one primitive per decision; do not ask a noul
  and a choice for the same fact and expect them to agree.
- **Add `other` or `unclear`** to every choice, and route it to today's behaviour.

## Model pinning and upgrades

Pin `jev-1.13.0`, not `jev-latest`, in `MODEL`. On a new version: run
`scripts/jev_eval.py` and the per-site golden sets against both, diff the confidence
histograms on a week of stored states, then bump `MODEL` and `VERSION` together in one
PR per site. Both `jev-latest` and `jev-preview` resolved to the same build on
2026-09-19, so the alias is not a safe proxy for "unchanged".
