# Broadcasts

A broadcast is ONE deliberate action: admit a segment into a workflow, now
or on a schedule (canon T17, canon/06-outreach.md). It is the launch button
and nothing else — it never sends (the workflow does) and never owns the
workflow (ten broadcasts may launch the same one). Code:
`app/crm/outreach/broadcasts.py`, `app/crm/outreach/campaigns.py`.

| Phase | Adds |
|---|---|
| 1 | `crm_campaign` (the badge, T22) and `crm_broadcast` (T17); create a campaign; create a DRAFT broadcast, read one, list them |
| 2 | the launch: schedule / cancel, and the freeze — the segment read once, late, into recipient rows (T18) |
| 3 | admitting the frozen recipients into the workflow — its guards as skips with reasons, the rest enrolled with `source_broadcast_id` |
| 4 | the funnel, and closing a broadcast |

## A campaign (the badge)

`POST /campaigns` `{merchant_id, key, name, starts_at?, ends_at?}`

- `key` is the season's stamp (`diwali-2026`): lowercase letters and digits
  joined by single hyphens, at most 64 characters, lowercased on the way in.
  Unique per merchant (409 when taken); it can never be changed.
- `name` is presentation; `starts_at` / `ends_at` are for the report's
  header — a campaign never blocks a send.

## A broadcast (a draft, in phase 1)

`POST /broadcasts` `{merchant_id, name, segment_id, workflow_id, campaign_key?}`

- The segment, the workflow and the campaign must be this merchant's; 422
  names every one that is not, at once. An archived workflow is refused —
  it admits nobody. A draft or paused workflow is allowed: the launch is
  where it must be live.
- Every broadcast is born `draft`, with no clocks set beyond `created_at`.
- `GET /broadcasts?merchant_id=&campaign_key=&limit=&offset=` — newest
  first, with the total; `GET /broadcasts/{id}?merchant_id=` — one (404 for
  an unknown or another merchant's id).

## Example

[`final-nudge.json`](final-nudge.json): the Diwali season's badge, and a
draft broadcast that will admit "Offered, not granted" into a final-nudge
call workflow. Both bodies are validated in CI against the request models.
