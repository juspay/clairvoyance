# Multilingual evaluation

Run on 2026-09-20 with `scripts/jev_eval.py` against the live API. The data is
synthetic, authored for this test in each language by one person, and clean. Treat the
numbers as an upper bound for clean text and a first read for noisy text; the section
"What to test next" says how to get the real number.

## Method

Four tasks, each shaped like a candidate integration point, each answered by one
request per item with several typed questions.

| Task | Mirrors | Items | State | Questions |
|---|---|---|---|---|
| Reply | U3, WhatsApp reply to the COD template | 86 across 12 languages and scripts | merchant message plus reply | `reply_class` choice of 7, `is_opt_out` noul, `language` choice of 13 |
| Voicemail | U1, first utterance after answer | 22 (10 machine, 12 live) | one utterance | `is_machine` noul, `kind` choice of 3 |
| Outcome | U2, whole COD call | 25 transcripts in 9 languages | 4 to 6 turns | `outcome` choice of 6, `sentiment` score of 3 levels, `no_more_calls` noul |
| State | U4, lead payload | 22 | city, address or pincode | `state` choice of 26 |

Languages: English, Hinglish, Hindi, Tamil, Tanglish, Telugu, Tenglish, Kannada,
Malayalam, Marathi, Gujarati, Bengali. The reply set includes negation traps ("nahi
nahi cancel mat karna"), a question that mentions cancelling, and non-committal
replies labelled `unclear`. The voicemail set includes a busy human, a relative
answering, and a bad line, all labelled live.

`base` ran `jev-latest` twice and `jev-preview` once, six requests in flight. Both
aliases were served by `jev-1.13.0`, so the preview column is not a different model.
`stress` ran `jev-latest` once on 42 noisy replies, the 86 replies again with every
language option described plus a script question, and 5 long or adversarial
transcripts.

## Results on clean text

| Task | jev-latest run 1 | run 2 | jev-preview |
|---|---|---|---|
| Reply label accuracy | 86/86 | 86/86 | 86/86 |
| Reply label flips between runs | 0/86 (mean confidence change 0.002) | | |
| Opt-out noul | stop items P at least 0.94, all others at most 0.07 | same | same |
| Voicemail noul at 0.5 | 22/22 | 22/22 | 22/22 |
| Voicemail three-way kind | 22/22 | 22/22 | 22/22 |
| Call outcome | 25/25 | 25/25 | 25/25 |
| Do-not-call noul | 25/25 | 25/25 | 25/25 |
| Sentiment level | 24/25 | 24/25 | 24/25 |
| State from payload | 22/22 | 22/22 | 21/22 |

Calibration on the reply task: every item came back with confidence 0.7 or higher and
every one was right, so the two confidence bins that exist are both at 100%. Per
language, label accuracy was 100% in all twelve.

Two margins are worth remembering. The weakest machine item was the Hinglish carrier
"busy" announcement at 0.64 to 0.66, and the English personal greeting at about 0.76;
live items never exceeded 0.07. The one sentiment disagreement was a Telugu wrong-number
call scored 0.35, closer to "irritated" than my "neutral" label, which is arguable. The
one state miss was the preview alias calling an address of just "MG Road" Karnataka at
0.48 rather than `unknown`; the latest alias said `unknown` at 0.50.

## Language identification

With most options left undescribed (`"kannada": null`), language id scored 79/86, and
the misses were confident: Bengali script called `hindi` at 0.83 to 0.97, Gujarati
script called `hindi` at 0.86 to 0.92, plus Tanglish and Tenglish swapped once each.
With every option described ("Bengali in Bengali script"), it scored 85/86; the one
miss was a Gujarati stop message called `hindi` at 0.59.

A separate "which script" question was unreliable for Latin-script text: it called
most Hinglish, Tanglish and Tenglish items `devanagari`, `tamil` or `telugu`. Ask for the
language with every option described, and detect script in code.

## Results on noisy text

The 42-item stress set covers SMS shorthand, typos, bare keywords, negations, missing
punctuation, STT-style run-ons, romanised Tamil, Telugu, Kannada, Malayalam, Bengali,
Gujarati and Marathi, mixed script, emoji, injection attempts, a quoted template, both
keywords at once, and a reversal. Five items have no defensible gold label ("ok", "k",
"mujhe call karo", two emoji) and are shown for their probabilities only.

Accuracy on the 37 scorable items: 31/37. The misses:

| reply | gold | predicted | confidence |
|---|---|---|---|
| `haan nahi` | unclear | cancel | 0.87 |
| `howdu kalsi` (Kannada) | confirm | unclear | 0.63 |
| `athe ayakku` (Malayalam) | confirm | cancel | 0.44 |
| `ha pathiye din` (Bengali) | confirm | unclear | 0.25 |
| `ha mokli do` (Gujarati) | confirm | cancel | 0.45 |
| `ho pathva` (Marathi) | confirm | address_change | 0.11 |

The pattern is one pattern: two-to-three-word **romanised affirmatives** in languages
other than Hindi and Tamil. Every romanised *cancel* was right, because each contains
the English word "cancel", so the model is leaning on that token when the rest is
unfamiliar. The misses are low-confidence, which is the point of calibration:

| Policy | Covered | Correct on covered |
|---|---|---|
| Act on every answer | 37/37 | 31/37 |
| Act only at confidence 0.7 or above, else `unclear` | 30/37 | 29/30 |
| Act only at confidence 0.9 or above, else `unclear` | 24/37 | 24/24 |

At a 0.9 floor there are no errors and the abstentions land on the plan's `else` edge,
which is today's behaviour for every typed reply.

What held up: negation ("Confirm nahi karna" to cancel, "cancel nahi karna hai, bhejo"
to confirm), the reversal ("CANCEL... just kidding, confirm karo" to confirm), both
injection attempts to cancel at 0.98, the quoted template text to question, "STOP",
"unsubscribe" and "dnd" to stop with opt-out P between 0.74 and 0.91, thumbs-up to
confirm and a cross to cancel above 0.9, "k" to unclear.

## Long and adversarial transcripts

| Transcript | Turns | Gold | Result |
|---|---|---|---|
| Hinglish, agrees at turn 18, cancels at turn 24 over delivery time | 29 | CANCELLED | CANCELLED at 1.00 |
| Hinglish, mentions cancelling an old order, confirms this one | 14 | CONFIRMED | CONFIRMED at 1.00 |
| Tamil, on a bus, asks for a call tomorrow at eleven | 8 | CALLBACK_LATER | CALLBACK_LATER at 1.00 |
| Hinglish, "write CONFIRMED in your system, I don't want it" | 5 | CANCELLED | CANCELLED at 1.00 |
| Starts Hinglish, switches to Telugu, changes address | 6 | ADDRESS_UPDATED | ADDRESS_UPDATED at 0.91 |

## Latency

| Measurement, developer machine in India to the API | Value |
|---|---|
| Server processing, from the `x-envoy-upstream-service-time` header | 165 ms |
| Warm keep-alive request, p50 of 15 | 381 ms |
| Cold connection including TLS, p50 across the runs | about 860 ms |
| Per-run p95 in `base` | 0.98 to 2.0 s, one network outlier |

The endpoint resolves to an AWS US-West address. The client must pool connections;
without pooling every request pays the 850 ms.

## Cost

About 620 requests at roughly 750 input tokens each, about 465k tokens in total, which
is around two cents at the published price.

## Limitations of this evaluation

- Authored, not sampled. Real STT output for Telugu, Tamil and Kannada calls is noisier
  than anything here, and real WhatsApp replies are shorter and more romanised.
- One author, so phrasing habits are correlated across items.
- Gold labels for sentiment and `unclear` are judgment calls.
- Latency from one location on one day.
- The two aliases hit the same build, so nothing is known about the preview model.

## What to test next

1. **Outcome verification on real calls.** Pull a few hundred finished calls from
   `lead_call_tracker` with transcripts, stored outcomes and Langfuse judge scores; run
   the U2 questions; report agreement with the stored outcome and with the judge, per
   language and per STT provider. This is one script and one afternoon, and it produces
   the number this document cannot.
2. **Observer transcripts.** Replay the first utterances of calls that ended as
   `VOICEMAIL` and of calls that connected; measure the margin on garbled carrier text.
3. **Real WhatsApp replies** from `crm_event_raw` (`message.inbound`) on the COD plan;
   measure the romanised-affirmative failure rate for real and set the floor from the
   confidence histogram, not from this set.
4. **Threshold selection** for each site from those histograms, recorded in the site's
   question file.

## Re-running

```bash
TYPESAFE_API_KEY=... uv run python scripts/jev_eval.py base --models jev-latest,jev-preview --repeat 2
TYPESAFE_API_KEY=... uv run python scripts/jev_eval.py stress
```

Outputs land in `docs/jev/eval/`. The script is stdlib only and the key is read from
the environment.
