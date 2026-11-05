# Buddy Copilot Scope Foundation

## Purpose

Buddy Copilot brings the existing Buddy Assist chat experience into the
dashboard as a read-only copilot for merchant analytics and conversation
inspection. The important backend boundary is that the chat runtime identity is
not the same thing as the merchant data scope.

This PR establishes that boundary in Clairvoyance. It defines the server-owned
scope object that later session creation and tool execution work can attach to a
normal Buddy Assist chat session as `metadata.copilot`.

## Architecture In Brief

Buddy Copilot is split across the dashboard, the normal Assist runtime, and
scoped backend tools.

```text
Loom dashboard
  -> selected merchant and optional selected agent
  -> Clairvoyance scope resolver
  -> normal Buddy Assist chat session
  -> metadata.copilot
  -> guarded read-only Copilot tools
  -> typed results/events for Loom rendering
```

Loom owns the visible user context: the dashboard login, selected merchant, and
optional selected agent. Clairvoyance treats that context as a request, not as
authority.

The Buddy Assist template owns runtime behavior: prompt, rules, refusal policy,
and chat-agent behavior. That template's merchant id is runtime identity only.
It must not become analytics or conversation data scope.

Future Copilot tools will load the resolved scope from `metadata.copilot` and
use only `scope.data.data_merchant_id` and optional
`scope.data.data_template_id` for analytics and conversation reads.

## What This PR Adds

### Scope Request

The dashboard can request a Copilot data scope with:

```json
{
  "data_merchant_id": "merchant-1",
  "data_template_id": "11111111-1111-4111-8111-111111111111",
  "timezone": "Asia/Kolkata",
  "date_range": {
    "date_from": "2026-07-01",
    "date_to": "2026-07-30"
  }
}
```

`data_merchant_id` is the merchant whose analytics or conversations should be
queried. `data_template_id` is optional; when it is missing, the scope means all
agents under the selected data merchant. When present, `data_template_id` must
be a UUID-shaped template id before the resolver attempts ownership lookup.

### Resolved Scope

The resolver returns an immutable `CopilotScope` with:

- `actor`: authenticated user snapshot for downstream audit and policy.
- `data`: selected merchant and optional selected agent/template.
- `date_window`: normalized date range and timezone.
- `capabilities`: read-only Phase 1 capability names.

The resolved scope can be injected into the normal chat session as:

```json
{
  "copilot": {
    "actor": {},
    "data": {
      "data_merchant_id": "merchant-1",
      "data_template_id": "11111111-1111-4111-8111-111111111111"
    },
    "date_window": {},
    "capabilities": []
  }
}
```

This shape is intentionally semantic. It avoids generic `merchant_id` fields in
the data object so callers do not confuse Copilot data scope with
`chat_session.merchant_id`.

### Merchant Scope Validation

The resolver validates the requested `data_merchant_id` against the
authenticated user's resolved merchant scope.

If the request includes a selected merchant and the user can access it, the
resolver accepts it. If the selected merchant is outside the user's scope, the
resolver rejects it.

If the request does not include a merchant, the resolver auto-selects only when
the authenticated user resolves to exactly one concrete merchant. Multi-merchant
and unrestricted scopes must provide an explicit selected merchant to avoid
ambiguous data access.

### Template Ownership Validation

When `data_template_id` is provided, the resolver verifies that the template
belongs to `data_merchant_id`. The request schema rejects non-UUID
`data_template_id` values before any database lookup is attempted.

```text
template.merchant_id == data_merchant_id
```

This check is required because the dashboard-provided merchant/template pair is
not authority. A malformed or tampered request could otherwise pair one
merchant's id with another merchant's template id.

The ownership lookup uses a lightweight template accessor that selects only
`merchant_id`. It deliberately avoids loading the full template flow,
configuration, or secrets for an authorization check.

### Date Window Normalization

The resolver accepts an explicit date range or falls back to the previous seven
calendar days in the requested timezone. The default window includes today as
`date_to` and six earlier days as `date_from`.

Invalid date ordering is rejected by schema validation. Invalid timezone names
are rejected as scope errors.

### Authenticated Dashboard Assumption

This PR does not add a backend analytics-permission gate inside Copilot scope
resolution. The dashboard owns Copilot visibility and only shows the Copilot
entry point to users that should have access to it.

Clairvoyance still validates the data boundary after authentication:

- selected merchant must be in the user's resolved merchant scope
- optional selected template must belong to the selected merchant
- ambiguous merchant scope must be made explicit

## Invariants

- Copilot data reads must use `scope.data.data_merchant_id`.
- Optional agent drill-down must use `scope.data.data_template_id`.
- `chat_session.merchant_id` must not be used as Copilot data scope.
- The dashboard Assist template merchant must not be used as Copilot data
  scope.
- Runtime template/merchant identity is not stored in `metadata.copilot`.
- Missing `data_template_id` means all agents under `data_merchant_id`.
- A provided `data_template_id` must be owned by `data_merchant_id`.

## Files In This PR

```text
app/schemas/breeze_buddy/copilot.py
app/services/breeze_buddy/copilot/scope.py
app/database/queries/breeze_buddy/template.py
app/database/accessor/breeze_buddy/template.py
tests/test_copilot_scope.py
```

## Out Of Scope

This PR does not create Copilot chat sessions, provision the dashboard Assist
template, register tools, execute analytics queries, stream typed events, or add
Loom UI. Those later pieces should consume the scope contract defined here.

The next backend slices can rely on `metadata.copilot` as the stable handoff
between session creation and tool execution.
