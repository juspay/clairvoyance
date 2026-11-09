# Xyne apps — connect an app, receive answers, send forms

A merchant's **Xyne app** is a connector account, `xyne:<appId>`. A merchant
can connect any number of apps; each app belongs to one merchant.

## 1. In Xyne (once per app)

Done with a Xyne login, not the app token (Xyne Apps guide, "Setup"):

1. `POST /api/apps/create` (needs XYNE-APPS **Write**) → the app id, e.g. `app_hire01`.
2. `POST /api/apps/install/:appId` (needs **Admin**) → the app's API token,
   shown **once**. Keep it for step 2 below.
3. Get the app's webhook signing secret (ask the Xyne team where it is shown).

## 2. Connect it to Buddy

`POST /connectors/xyne/onboard` (merchant-scoped, like every connector):

```json
{
  "merchant_id": "pragathi",
  "api_token": "<the app's Xyne API token>",
  "webhook_secret": "<the secret Xyne signs this app's webhooks with>",
  "base_url": "https://spaces.xyne.juspay.net",
  "display_label": "Hiring bot",
  "preview_user_id": "<the Xyne user id that receives form previews>"
}
```

`preview_user_id` is optional to connect, but a form cannot be approved
without it (§5): Xyne checks a form only by being sent it, so a person —
usually whoever builds the forms — receives a preview of each one.

There is no `app_id` field: the app id is read from the token's `appId` claim.

What Buddy checks, in order:

| Check | If it fails |
|---|---|
| Caller is logged in and may act for `merchant_id` | 401 / 403 |
| `base_url` is `https://` on a `*.xyne.juspay.net` host, with no path, query, fragment or login | 422 |
| `webhook_secret` is at least 16 characters | 422 |
| `preview_user_id`, when given, is a Xyne user id | 422 |
| The token carries an `appId` | 400, Xyne never called |
| Xyne accepts the token: `GET {base_url}/api/apps/channel/list?limit=1&joined=true` with `Authorization: Bearer <token>`. 200, or 403 `missing_permission` (the token is genuine, the app only lacks `channels:read`), passes | 400 (401, another 403, or another 4xx), or 400 "try again" (Xyne unreachable, 5xx, 429, redirect) |
| No other merchant has this app connected (checked after the token is proven, and again under a lock in the transaction) | 409 |
| The connection is not disabled by an administrator | 400 |

On success:

- **`credentials`**: one row named `xyne:<merchant>:<appId>`, encrypted
  with AES-256-GCM under `CREDENTIAL_ENCRYPTION_KEY`: `api_token`,
  `webhook_secret`, `base_url` and, when given, `preview_user_id`.
- **`crm_connector_installation`**: `connector_key = 'xyne'`,
  `external_account_id = <appId>`, `credential_id` → that row, status
  `healthy`. The response is this row; it never contains a secret.
- **`crm_channel_binding`**: `channel = 'xyne'`, `address = <appId>` — the
  pipe a form leaves on. The merchant's first Xyne app is the primary one;
  sends go out through it.

Connecting the same app again replaces the token, secret, base URL and
preview user in place (leave `preview_user_id` out and it is removed).
Disconnecting sets the row to `revoked` and pauses its binding; after that
another merchant may connect the app — the first merchant's paused binding is
then retired (kept, because their sent messages point at it), and if the app
later comes back to them, their binding is revived. A binding of a merchant
still connected is never touched: the second merchant gets 409.

## 3. Before the first prod connection

Run on the replica (read-only):

```sql
SELECT is_encrypted, count(*) FROM credentials GROUP BY is_encrypted;
```

Recent `false` rows mean `CREDENTIAL_ENCRYPTION_KEY` is missing or invalid
(not 32 bytes): the vault then stores credentials as plain JSON, with only a
log line. Fix the key before connecting apps.

## 4. Receive form answers

Set the app's webhook URL in Xyne — once per app, in each workspace the app
is installed in — with a Xyne login that has XYNE-APPS **Write**
(`POST /api/apps/configureWebhook/:appId`):

```json
{ "webhookUrl": "https://<buddy host>/ingest/webhooks/xyne/<appId>" }
```

Every callback of that app arrives there, signed with the app's signing
secret (`X-Xyne-Signature`, plain-hex HMAC-SHA256 of the body). Buddy finds
the app's connection from `<appId>` in the URL — the merchant is the app's
owner, never anything in the body — and checks the signature with that
app's secret.

| Callback | Buddy answers | Stored |
|---|---|---|
| `X-Xyne-Event: flow_action`, `type: "submit"` | 200 `{"type":"close_screen","finalMessage":"Response received"}` | one `xyne` / `flow_action` event; one person's same answer twice is stored once |
| A field edit (`type: "inputChange"`) or any other app event | 200 `{"type":"ack"}` | nothing |
| Wrong signature, unknown app, disconnected app | 403 | nothing |
| Body over 1 MB / not JSON | 413 / 400 | nothing |
| A read or the store failed | 503 `{"type":"error","message":"Couldn't save your response, please submit again","retryable":true}` — Xyne shows it to the person | nothing |

What a workflow can use from a stored answer (catalog entry `xyne` /
`flow_action`, "Form submitted"):

| Field | Is | Use |
|---|---|---|
| `context.userId` | who answered — identity `xyne_user_id` | finds the customer; Xyne's server sets it, the person cannot |
| `reply` | `form_submitted` | the arrow out of a listening square |
| `button` | the `actionId` of the button pressed | branch on which button. The person's browser sends it and Xyne does not check it against the form, so it is the person's claim, like their answers |
| `flow_response` | every answer as `name: value` lines | one blank in a call or message (≤ 1000 characters; longer is dropped and a step that needs it parks) |
| `replied_to` | `context.messageId` | which of our sends it answers — finds the run |

`context.flowJSON` (the form as the person's browser sent it back) is stored
with the event and never read: the person can edit it.

For the answer to find a person, the letter that starts their run must carry
`"xyne_user_id": "<their Xyne user id>"` at the top level, next to their
phone. Example plan: `docs/crm/plans/xyne-interview-availability.json`.

Known limits:

- **Which run an answer wakes.** An answer names the message the form was
  posted in (`replied_to`). When Buddy sent the form (§5), the send recorded
  that message, and the answer wakes only the run that sent it. A form
  posted outside Buddy names a message Buddy never sent, so its answer wakes
  EVERY run of that person that is listening for `flow_action`: a person
  with two such runs open has both woken by one answer.
- **Xyne's top-level fields become run facts.** As for every source, the
  answer's top-level scalars (`actionId`, `type`) are kept as facts of the
  square that heard it, and the answer becomes the run's latest letter: a
  later step reading `type` reads Xyne's `"submit"`, not the starting
  letter's `type`. Name the merchant's own facts so they do not collide.

## 5. Send a form from a workflow

Xyne keeps no templates — every message carries its whole form — so a form
is registered with **Buddy**, through the same template endpoints WhatsApp
templates use, and checked by **Xyne** by being sent to the preview user.

**1. Create the form** — `POST /connectors/templates`:

```json
{
  "merchant_id": "pragathi",
  "channel": "xyne",
  "provider_account_ref": "app_hire01",
  "name": "interview_slots",
  "language": "en",
  "components": [
    { "id": "h", "type": "heading", "props": { "content": "Hi {candidate_name}, pick your slots" } },
    { "id": "slots", "type": "multiselect",
      "props": { "name": "slots", "label": "Free slots", "options": "$slots" } },
    { "id": "go", "type": "button",
      "props": { "label": "Submit", "action": { "type": "submit", "actionId": "submit" } } }
  ]
}
```

`components` is Xyne's own components array (FlowJSON 2.0), as Xyne
documents it. Two kinds of blank:

| Written as | Filled by | From |
|---|---|---|
| `{name}` in any text | Buddy, before posting (Xyne shows text as written) | a text fact the send step maps |
| `"$name"` as a dropdown/select/multiselect's `options` | Xyne, from the message's `data.name` | a `list` fact the send step maps — the list itself, each item `{"label", "value"}` |

**2. Submit it** — `POST /connectors/templates/{id}/submit` with any
`category` (Xyne has none; it is kept as given). Buddy checks every component
has an `id` and a `type` and the form is under 32 KB, then posts a preview —
blanks as written, lists empty — to the app's `preview_user_id`:

| Xyne answers | The template |
|---|---|
| 201 | `approved` at once; the preview is in the preview user's DM |
| 4xx | stays a draft; the submit answers 400 with Xyne's own reasons (`components.1.props.label: Required`) |
| no answer, 5xx, 429 | stays a draft; 400 "try again" |
| no `preview_user_id` on the connection | stays a draft; 400 naming it — connect the app again with one |

Editing an approved form (`PATCH /connectors/templates/{id}`) is checked the
same way and takes effect on the next send; forms already sent keep the copy
Xyne stored with each message. Retiring it tells Xyne nothing — Xyne never
held it.

**3. Use it in a workflow** — a send square on channel `xyne`, the plan's
`purpose_key` set:

```json
{ "id": "ask-availability", "type": "send", "channel": "xyne",
  "template": "interview_slots",
  "variables": { "candidate_name": "candidate_name", "slots": "slots" } }
```

Publish refuses the square unless `interview_slots` is approved on the
merchant's primary Xyne app.

**Who it goes to:** the customer's live Xyne user id — the
`crm_customer_handle` row that the starting letter's `xyne_user_id` put there
(Xyne 2/5) — read when the square sends. Never the phone, never a copy on
the run. A customer with no Xyne user id parks the run, by name.

**The lists:** a run keeps only short text facts, so a `list` field reaches
the run as one line of text. On a Xyne send, a mapped fact that is a
declared `list` variable of the starting letter's topic is read **whole**
from that letter instead (at most 100 items and 16 KB; more parks the run
rather than sending a cut list). A declared list the letter does not carry
parks the run too, by name. Every `"$name"` picker must get a non-empty list
of `{"label", "value"}` — Xyne accepts anything there and would show an
empty picker, so Buddy blocks the send (`template_variables_invalid`)
instead. Pragathi registers `interview.requested` with `slots` as
`type: "list"`, `variable: true`.

**What happens to the message** (the manifest row, `crm_message`):

| Outcome | Status / reason |
|---|---|
| Xyne 201 | `accepted`; `provider_message_id` = Xyne's `messageId` — what the answer's `replied_to` names |
| The person said STOP on this Xyne id | `blocked` / `suppressed` (the gate, before any call) |
| No usable token or base URL; no approved form; not a Xyne user id; a `{blank}` with no value; a `"$name"` picker without a `{label, value}` list | `blocked`, no call made |
| 400 | `failed` / `xyne_form_refused` |
| 401 | `failed` / `xyne_token_refused` — connect the app again |
| 403 `missing_permission` | `failed` / `xyne_missing_permission` — the app lacks `chat:write` |
| other 403 | `failed` / `xyne_forbidden` |
| 404 | `failed` / `xyne_recipient_unknown` — not a user of the app's workspace |
| 429, 5xx, a redirect, no answer | retried |

**STOP:** a STOP on a Xyne user id is recorded like any other
(`record_suppression("xyne_user_id", <id>, …)`). Before every Xyne send the
gate checks the Xyne id AND the person's own phone and email — a STOP given
by phone reaches their Xyne forms too. The gate now refuses an address it
cannot check rather than letting it through.

Known limits of sending:

- **One app sends.** A merchant with two Xyne apps sends through the
  primary one (the first connected), and its forms must be registered on
  that app.
- **A retry can post twice.** Xyne's postMessage takes no idempotency key;
  if Xyne took the form but its answer was lost, the retry posts it again
  and only the second copy's message is recorded. An answer to the FIRST
  copy names a message Buddy never recorded, so — like a form posted
  outside Buddy — it wakes every run of that person listening for
  `flow_action`. Each answer is still stored once.
- **A submit that dies after Xyne accepted the preview** (the database
  fails at that instant) leaves the form `submitting`: Xyne sends no events
  that could repair it, unlike Meta. Ops sets the row back to `draft` and
  the merchant submits again.
- **Lists come from the starting letter only.** A list on a later letter,
  or from an action step's answer, is not read whole yet — so a repeat
  letter that refreshes a run still waiting on its first square does not
  refresh its list.
- **A merged-away customer parks.** A run whose customer was merged into
  another reads the merged-away record, which holds no live Xyne id.

