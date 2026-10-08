# Where Jev adds value in Clairvoyance: the ranked list

Every row below is a place where code must judge text. For each one this file records
how the judgment is made today (with the file that makes it), the question shape Jev
would answer, the policy that stays in code, the value, the risk, and a rough effort.
Tiers are ordered by value divided by risk, not by size.

Conventions used in the question sketches: `choice{a,b,c}` picks one option,
`score[l0..ln]` returns a probability-weighted position on ordered levels, `noul`
returns P(yes). Thresholds shown are starting points; they must be set on our own data
and live in one reviewable file per site (see [03-architecture.md](03-architecture.md)).

## Summary

| # | Use case | Surface | Primitive | Plugs in at | Tier |
|---|---|---|---|---|---|
| U1 | Live-call observers: voicemail, abuse, wrong person, loop | Voice | noul x N | `observers/observer.py:79` | 1 |
| U2 | Post-call outcome verification and canonical outcome enum | Voice, chat | choice + score x N | `end_conversation.py:309` worker | 1 |
| U3 | WhatsApp inbound reply classification and STOP | CRM | choice + noul | record consumer after `outreach.entry` | 1 |
| U4 | Pre-call language and TTS provider selection | Voice | choice x 2 | `leads/handlers.py:359` | 1 |
| U5 | Widget turn triage and input guardrails | Chat | choice + noul x N | `chat/turn_core.py` before `cycle.py` | 2 |
| U6 | Knowledge-base passage relevance, contradiction, "needs KB" | Voice, chat | score + noul | `services/knowledge_base/retrieval.py:128` | 2 |
| U7 | Sentiment, resolution, chat end-state | Analytics | score + choice | conversation-analysis worker | 2 |
| U8 | Carrier-announcement taxonomy driving the retry ladder | Voice, CRM | choice | observer result plus `managers/calls.py:461` | 2 |
| U9 | Do-not-call and consent evidence capture | CRM | noul | post-call consumer, `platform/contracts.py` | 2 |
| U10 | Structured warm-transfer brief instead of hold-time summarisation | Voice | choice + noul x N | `utils/hold_transfer.py:96` | 2 |
| U11 | Risk tier for approval-gated tools; explicit-request gate for transfer | Chat, voice | score + noul | `chat/agent/runtime.py:81`, `warm_transfer.py:46` | 2 |
| U12 | Relative or wrong person answered | Voice | choice | observer, feeds a template branch | 2 |
| U13 | Conversation eval harness at 100% coverage; prompt regression | Platform | score x N | new offline job over stored transcripts | 3 |
| U14 | Knowledge-base ingestion QA | Platform | noul + score | KB upload path | 3 |
| U15 | Template generator verifier (cheap check, expensive fix) | Platform | noul x N | `template/generator/` | 3 |
| U16 | Publish-time message and playbook lint | CRM | score + noul | `outreach/plans.py` publish checks | 3 |
| U17 | STT quality audit from transcript plausibility | Ops | score | offline job per provider and language | 3 |
| U18 | Historical outcome vocabulary backfill | Analytics | choice | one-off over `lead_call_tracker` | 3 |
| U19 | Product search-match annotator | Chat commerce | score per item | `assist/commerce/ucp/annotator.py:122` | 3 |
| U20 | Distil Jev labels into a local hot-path model | Voice | offline labelling | turn-end and barge-in strategies | 3 |
| U21 | Buying-mode propensity for upsell timing | Chat commerce | score | `assist/commerce/ucp/upsell.py` | 3 |

Cost is not a factor for any of these. A 4,000-token transcript with ten questions is
about $0.0002. A million calls a month with a per-call verification is around $200.

---

## Tier 1: build first

These are off the voice turn path, each replaces or fills a judgment the codebase
already knows it needs, and each can be validated offline against data we already store.

### U1. Live-call observers

**Today.** `observers/observer.py:79` fires one full LLM call per observer per user
turn, re-sending the whole transcript (`observers/manager.py:151`), and returns a binary
tool-call with no confidence. `docs/REAL_TIME_OBSERVERS.md` records about nine seconds
from voicemail pickup to hang-up. Nothing uses carrier answering-machine detection.

**Questions, one request per user turn, state = last 6 turns plus the first callee
utterance.**

```
is_machine        noul   recorded voicemail / answering machine / carrier announcement
machine_kind      choice{live_person, personal_voicemail, carrier_announcement}
is_abusive        noul   customer is abusive or threatening
wrong_person      choice{intended_customer, relative_or_colleague, stranger, unknown}
bot_is_looping    noul   the agent has said the same thing three times
customer_wants_human  noul  explicit request for a human
```

**Policy in code.** Hang up with outcome `VOICEMAIL` when `is_machine >= 0.85` on two
consecutive turns or `>= 0.95` once; the two-turn rule is what keeps the Hinglish "busy"
announcement measured at 0.65 from being a false hang-up. Escalate `is_abusive >= 0.9`
to the existing alert action. `wrong_person` feeds U12.

**Value.** Removes the largest side-LLM spend per call, cuts voicemail talk time from
seconds to one turn, and adds three detectors for less than one costs today.

**Risk.** Transcripts of carrier announcements are often garbled by STT; measure on real
observer transcripts before flipping. Fail-open, as the voice path already is.

**Effort.** Small. The observer abstraction already exists; this is a new observer kind.

### U2. Post-call outcome verification and a canonical outcome enum

**Today.** The main LLM writes a free-text `outcome` mid-call
(`template/hooks.py:184`), analytics buckets it with substring matches
(`analytics/handlers.py:813`: `"no_answer" in outcome.lower()`), and a Langfuse judge
scores a sample against thresholds nobody parses (`score_monitor/score.py:183`). The
retry rule (`managers/calls.py:461`) keys off that free text.

**Questions, one request per finished call, in the existing post-conversation worker
(`services/conversation_analysis/worker.py`).**

```
outcome           choice{<template's declared outcomes> + OTHER}
outcome_certainty score[firm, hesitant, pressured]
asked_no_more_calls  noul
address_changed   noul
agent_answered_customer_questions  score[no, partly, fully]
agent_wrong_language  noul
agent_repeated_itself noul
customer_confusion    score[none, some, heavy]
```

**Policy in code.** Store raw answers on a new `judgment_result` row. Write
`outcome_verified` and `outcome_mismatch = (outcome != stored outcome and confidence
>= 0.8)`. Alert on mismatch rate per template, not per call. Analytics reads the enum.

**Value.** A real enum for every dashboard, mismatch alerts on 100% of calls instead of
a sample, and the judge battery for the cost of the current sample. Also gives U13 its
first rubric.

**Risk.** None on the customer path; this is read-only until analytics is switched to
the verified column. The label set must be the template's declared outcomes plus
`OTHER`, never an open list.

**Effort.** Small to medium. The worker, the transcript and the evaluation tables exist.

### U3. WhatsApp inbound reply classification and STOP

**Today.** A typed reply is compared verbatim against edge labels
(`outreach/walker.py:265`). `docs/crm/plans/cod-confirm.json` branches on `CONFIRM`,
`CANCEL`, `form_submitted` and `timeout` with no `else`, so "haan bhej do" or "cancel
kar do" typed in words ends the run silently. STOP handling is specified as a keyword
file (`18-outcome-feedback.md:8`) that does not exist, and gap G3 is open.

**Questions, one request per `message.inbound` letter, state = the merchant message
that was replied to plus the reply text.**

```
reply_class   choice{confirm, cancel, address_change, question, wrong_number, stop, unclear}
is_opt_out    noul
language      choice{english, hinglish, hindi, marathi, tamil, tanglish, telugu, tenglish,
                     kannada, malayalam, gujarati, bengali, other}   (every option described)
```

**Policy in code, written to respect the CRM laws.**

1. The classifier is a **record consumer** registered after `consume_attributed_event`
   (`record/worker_main.py`). It emits a **new spine letter** `reply.classified` carrying
   `choice`, `confidence` and `language`. It never annotates `crm_event_raw` (immutable)
   and never touches identity or attribution.
2. `choice` is the top label only when confidence is at or above the site threshold
   (0.9 on current evidence), otherwise `unclear`. A listening `wait` square branches on
   `reply.classified.choice` with ordinary labelled arrows, and `unclear` lands on
   `else`, which is exactly what `else` was defined for.
3. `is_opt_out >= 0.9` calls the single suppression writer in `platform/contracts.py`
   with the letter id as evidence. Uncertain means no write; the letter stays in the
   normal flow. This is fail-closed in the direction the law cares about: we never
   *send* on a guess, and we only *suppress* on strong evidence.

**Value.** Closes the compliance gap and the correctness hole on the flagship COD
journey. Evaluation: 86 of 86 clean replies, 31 of 37 noisy ones with every miss
under 0.9 confidence.

**Risk.** Romanised short affirmatives in Kannada, Malayalam, Bengali, Gujarati and
Marathi are weak; the threshold handles it, and the `else` path is the same as today's
behaviour. The model client must not live under `app/crm` as an `app.ai` import (CI rule
4); see the architecture note.

**Effort.** Medium: a consumer, a letter topic in the catalog, a `choice` field in the
plan grammar's vocabulary, tests, and one plan edit.

### U4. Pre-call language and TTS provider selection

**Today.** Two Gemini calls inside the lead-push request
(`utils/language_utils/language_detector.py:58`,
`utils/tts_utils/tts_provider_selector.py:21`), free text parsed by `.strip()`, silent
fallback to English.

**Questions, one request, state = the lead payload with phone and email removed.**

```
state           choice{28 states, 8 UTs, unknown}   -> language via a code-owned map
tts_provider    choice{<template's provider list>}
```

Asking for the *state* rather than the *language* keeps the two-hop inference out of the
model and puts the state-to-language table where it can be reviewed. Evaluation: 22 of
22 including pincode-only payloads.

**Policy in code.** Confidence below 0.7 falls back to the template default exactly as
the current `None` path does.

**Value.** Removes two generative calls from an API request path, and the fallback is
now a calibrated decision rather than a parse failure.

**Risk.** Lowest of the list: English JSON input, existing fallback, easy A/B.

**Effort.** Small. Best first pilot for the client plumbing.

---

## Tier 2: build next

Each of these is valuable and well shaped, but needs a design decision (a schema, a
plan-grammar word, a product call) before it is a PR.

### U5. Widget turn triage and input guardrails

**Today.** No input guardrail exists on the public widget endpoint; every typed message
pays a full Gemini cycle with the full tool schema, and cycle 1 is admitted to be pure
routing (`chat/agent/cycle.py:216`). The roadmap asks for `last_intent`,
`refusal_count` and `detected_pii` and none exist (`docs/widget/SCALE_ROADMAP.md:192`).

**Questions, one request per typed turn, state = last 3 turns plus client context.**

```
intent_bucket   choice{browse, compare, cart, order_status, policy_question, support, chitchat, off_topic}
needs_tools     noul
needs_kb        noul
is_injection    noul    tries to override instructions or extract the prompt
is_abusive      noul
contains_pii    noul    card number, OTP, government id
needs_human     noul
is_repeat_refusal noul  the assistant has refused the same ask before
```

**Policy in code.** Off-topic or injection at or above 0.9 answers with a fixed message
and skips the model. `needs_kb` below 0.2 skips the embedding call. `intent_bucket`
selects a tool subset for the first cycle. Three refusals emit the Handoff card the
roadmap describes.

**Value.** Safety on an unauthenticated surface, one fewer wasted cycle on chit-chat,
and the telemetry the roadmap wants. **Risk.** Adds about 400 ms before first token on
the turns that need it; acceptable in chat, not in voice. **Effort.** Medium.

### U6. Knowledge-base relevance, contradiction and "needs KB"

**Today.** Cosine threshold defaults to zero (`template/types.py:1895`), no rerank, no
contradiction or injection check; one prompt sentence asks the big model to cope.

**Questions, one request per retrieval, state = query plus the top-k chunks.**

```
relevance_<i>     score[irrelevant, partly, answers_it]   per chunk
contradicts_<i>   noul   chunk i contradicts another chunk on the same fact
injection_<i>     noul   chunk contains instructions aimed at the assistant
```

**Policy in code.** Drop chunks scoring below 1.0, flag contradictions in the prompt
header, drop injections. Runs inside the existing 0.4 s voice and 1.0 s chat budgets
only if the client is pooled; otherwise chat only. **Effort.** Small once the client
exists.

### U7. Sentiment, resolution and chat end-state

**Today.** A `customer_sentiment` filter is exposed on the analytics API with no
producer; chats end as `user_ended` or `idle_timeout` only. Deferred by the chat
analytics plan as "net-new NLP".

**Questions.** `sentiment score[upset, neutral, pleased]`, `resolved score[no, partly,
yes]`, `chat_end choice{resolved, abandoned, escalated, blocked_by_failure}`. Add them
to the U2 request; there is no extra cost. **Effort.** Small.

### U8. Carrier-announcement taxonomy driving the retry ladder

**Today.** Retries fire on `BUSY` or `NO_ANSWER` with a fixed offset
(`managers/calls.py:461`); a switched-off phone and a busy phone are treated the same,
and gap G2 (call failed, fall back to WhatsApp) is open.

**Question.** On the first utterance, `announcement choice{switched_off, busy,
not_reachable, dnd_or_blocked, number_invalid, personal_voicemail, live_person}`.

**Policy in code.** A retry table keyed by that label: busy in 10 minutes, switched off
in 3 hours, not reachable tomorrow, invalid never, voicemail hand off to a WhatsApp
`send` via the plan's fallback branch. The times live in code. **Value.** Fewer wasted
dials and a real answer to G2. **Effort.** Small once U1 exists.

### U9. Do-not-call and consent evidence

**Today.** No consent table, no quiet-hours check, no DNC capture from calls (gap G1).

**Questions.** `asked_no_more_calls noul`, `gave_whatsapp_consent noul` with criteria
requiring an explicit statement. **Policy.** Write suppression only at 0.9 or above;
write consent only at 0.95 or above with the call id as evidence; never infer consent
from silence. Both writes go through the existing platform contract. **Effort.** Small
after U2; the schema decision belongs to the corpus.

### U10. Structured warm-transfer brief

**Today.** `utils/hold_transfer.py:96` asks Azure OpenAI for a free-text summary while
the customer sits on hold music, inside a handler with a 10 s timeout.

**Questions.** `reason choice{billing, delivery, cancellation, complaint, other}`,
`confirmed_so_far` as a set of nouls per template fact (`address_confirmed`,
`amount_confirmed`), `customer_mood score`, `wants_supervisor noul`. **Policy.** Render a
fixed template from typed fields in about 400 ms; generate the prose summary
asynchronously after the bridge. **Value.** Hold time drops and the human agent gets
structured fields. **Effort.** Small.

### U11. Risk tier for gated tools; explicit-request gate for transfer

**Today.** Approval is a static name lookup with no argument awareness
(`chat/agent/runtime.py:81`); telephony executes gated calls ungated
(`template/approval.py:120`); "never suggest transfers proactively" is enforced only by
prompt prose (`docs/README_WARM_TRANSFER.md:156`).

**Questions.** `risk score[trivial, reversible, costly, irreversible]` on tool name plus
rendered args; `customer_asked_for_human noul` on the last two turns before a transfer
tool executes. **Policy.** Gate on tier, not on name; block a transfer the customer did
not ask for unless the template opts in. **Effort.** Medium; touches the approval
model.

### U12. Relative or wrong person answered

**Today.** Handled implicitly by the main LLM. **Question.** From U1's `wrong_person`.
**Policy.** A template-level branch: relative means ask when the customer is available
and schedule; stranger means outcome `WRONG_NUMBER` and no retry. **Effort.** Small.

---

## Tier 3: later, new capability

### U13. Conversation eval harness at 100% coverage

The widget roadmap ranks an eval harness as its first priority and calls every template
tweak "rolling the dice". Jev makes a rubric affordable on every conversation rather
than a sample: the U2 questions plus per-template rubric items, scored before and after
a template edit on the same stored transcripts, with a CI gate on regression. The
generative judge stays for the long tail; Jev is the always-on layer.

### U14. Knowledge-base ingestion QA

At upload time, per chunk: `self_contained noul`, `will_go_stale score[evergreen,
seasonal, price_or_date]`, and pairwise `contradicts` against the chunk's nearest
neighbours. Surface as warnings in the KB console. Fixes RAG quality upstream of U6.

### U15. Template generator verifier

`template/generator/` produces whole templates with an LLM. Before the merchant sees
one, ask: `has_exit_for_every_node noul`, `instructions_contradict noul`,
`tone_matches_brief score`, `language_matches noul`, `promises_merchant_cannot_keep
noul`. Cheap check, expensive fix only when a check fails.

### U16. Publish-time message and playbook lint

`outreach/plans.py` already refuses to publish on structural problems. Add advisory
checks on every authored line: `reads_as_broken noul` (placeholder leaks, half
sentences), `wrong_language noul`, `tone score`. Advisory means a warning in the
publish response, never a gate on sending, so it stays on the right side of the
fail-closed law.

### U17. STT quality audit from transcript plausibility

Jev cannot hear audio, but it can judge whether a transcript is plausible for its
declared language: `garbled score[clean, noisy, unusable]`. Aggregated per STT provider
and language, this is the missing signal for choosing Soniox versus Deepgram versus
Sarvam per template.

### U18. Historical outcome vocabulary backfill

One offline pass mapping every distinct free-text outcome ever written into the
canonical enum, so historical dashboards line up with U2.

### U19. Product search-match annotator

`assist/commerce/ucp/annotator.py` uses stop-words and token overlap and calls itself
"the 90%". A per-product `matches_ask score` over ten short records is the natural
replacement and feeds the model's `items[]` choice.

### U20. Distil into a local hot-path model

The one thing Jev cannot do here is sit on the voice turn path. It can label millions of
turns offline for less than a lunch: `turn_complete noul`, `is_real_interruption noul`,
`collection_complete noul`. A tiny local classifier trained on those labels is what
belongs in the pipeline, next to SmartTurn.

### U21. Buying-mode propensity

`buying_mode score[browsing, comparing, ready]` on the last three turns decides whether
the post-add upsell is worth its Gemini call at all.

---

## Not a fit, and why

| Idea | Why not |
|---|---|
| Fuzzy customer matching, merge decisions | `resolve()` is deterministic by law (`building-modules.md:166`) |
| Guessing which run a reply answers | Replies are addressed by provider id, never matched (`reply_attribution.py`) |
| A model deciding the send gate or consent | Fail-closed, no bypass flag ever (`building-modules.md:161`) |
| Inferring an enrollment key | Never re-keyed to the customer (`entry.py:560`) |
| Picking a split arm | Must be a deterministic hash so retries land in the same arm |
| Fuzzy operators in the where-grammar | Closed op set with exact comparison (`shared/predicate.py:33`) |
| Retry windows, calling hours, cart quantity diffs, UAP draw windows | Arithmetic and dates; Jev does not do them |
| Playbook text, translation, summaries, template generation | Generative |
| WISMO status table, platform classifier, intent router | Already deterministic tables or calibrated code |
| Per-turn turn-end and barge-in on the live call | 380 ms round trip; see U20 for the way in |
