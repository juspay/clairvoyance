# Xyne apps — connect, receive form submissions, post messages

A merchant's **Xyne app** is a connector account: `xyne:<appId>`. Once
connected, the app's form submissions arrive as `flow_action` events on the
spine, and a workflow can post to a Xyne channel with
`{connector: "xyne", action: "post_message"}`.

## 1. Connect an app

`POST /connectors/xyne/onboard` (merchant-scoped, like every connector):

```json
{
  "merchant_id": "flow-shop",
  "app_id": "app-123",
  "api_token": "<the app's Xyne API token>",
  "webhook_secret": "<the secret Xyne signs this app's webhooks with>",
  "base_url": "https://spaces.xyne.juspay.net",
  "display_label": "Hiring app"
}
```

- `base_url` is optional (defaults to `https://spaces.xyne.juspay.net`) and
  must be `https://` on a `*.xyne.juspay.net` host — the token is never sent
  anywhere else.
- Before anything is written, onboarding calls `GET {base_url}/api/apps` with
  `Authorization: Bearer <api_token>`. A 401/403 refuses the connection; an
  unreachable Xyne refuses it too (try again).
- On success: one row in `credentials` (custom, named
  `xyne:<merchant>:<appId>`) holding `base_url`, `api_token` and
  `webhook_secret`; one `crm_connector_installation` row with
  `connector_key = 'xyne'`, `external_account_id = <appId>`, status `healthy`.
  Re-onboarding the same app rotates the credential in place.

## 2. Point the app's webhook at us

In Xyne, set the app's webhook URL to:

```
https://<our host>/ingest/webhooks/xyne/<appId>
```

Each app has its own URL. The app id in the path is how the door finds THAT
app's secret before trusting the body:

| Check | Answer |
|---|---|
| app id held by no usable door (unknown, revoked, disabled) | 403 |
| credential missing / unreadable, or no `webhook_secret` | 403 |
| `x-xyne-signature` is not HMAC-SHA256 (hex, optional `sha256=`) of the raw body with that app's secret | 403 |
| signed, but not a `type: "submit"` with `actionId`, `values`, `context.messageId` | 200 + error reply, nothing filed |
| store failed | 200 + error reply (Xyne never retries; the person resubmits) |
| filed | 200 + `{"type": "close_screen", "finalMessage": "Thanks, your response is saved"}` |

The merchant is **the app's owner** — never a `merchant_id` in the body.
`context.messageId` is the event's `external_id`, so a replay is deduped.

## 3. Use it in a workflow

The catalog entry is **Form submitted** (`source: xyne`, topic
`flow_action`, about a customer). The person is resolved from
`context.flowJSON.data.customer_mobile_number`, so the form a workflow sends
must carry it. Fields include `actionId`, `values.selected_dates`,
`messageId`, `conversationId`, `userId`, `kind`, and the derived
`{selected_slots}` (`2026-10-15: 10:00, 11:00`, one line per date).

`post_message` args:

| Arg | |
|---|---|
| `channel_id` | required — the Xyne channel |
| `text` | message text; may be a `{placeholder}` |
| `flow` | optional FlowJSON form; placeholders inside it are resolved too |

An action square may also ask for `{run_id}` and `{customer_mobile_number}`
(filled from the run itself, action args only). Put `{run_id}` in the form's
`data.enrollment_id` and the wait can hear only its own run's answer:

```json
{ "type": "wait", "topics": ["flow_action"], "key": "actionId",
  "match": { "payload": "context.flowJSON.data.enrollment_id", "run": "id" } }
```

Worked examples: `docs/crm/plans/xyne-availability-resume.json` (send the
form, wait, post the picks) and `xyne-availability-start.json` (enter on a
submission).

Failures: a 4xx from Xyne or an unusable credential **parks** the run; a 5xx,
429 or timeout is retried by the walker.

## Open items

- **Confirm against live Xyne before going live:** the signature header and
  scheme, and the `postMessage` body (`channelId`, `text`, `flow`).
- **Xyne's async `MESSAGE_POSTED` reply** (a flat body with top-level
  `eventType`, `messageId`, `conversationId`) is not a submission, so it is
  answered with the error reply and not filed. Its real shape is not
  captured yet; add it once a recorded callback exists.
- **One app per merchant for sending:** `post_message` acts through the
  merchant's newest usable Xyne app. Receiving works for any number of apps.
- **One merchant per app id:** if two merchants onboard the same app id, the
  newest door owns its webhooks.
