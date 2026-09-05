# Buddy Copilot Dashboard Session Creation

## Purpose

This slice wires Buddy Copilot into the existing Breeze Assist chat widget and
chat session behavior. Copilot is not a separate runtime and does not get a
separate session endpoint. It is a normal dashboard Assist chat bot whose
session can carry an optional, server-validated Copilot data scope.

The main result is the handoff between the BZN-40679 scope foundation and the
future BZN-40681 tool boundary:

```text
Dashboard-hosted Breeze Assist chat widget
  -> existing Assist chat session creation behavior
  -> existing POST /api/breeze_buddy/chat/session
  -> selected Assist template controls chat runtime
  -> optional copilot_scope requests dashboard data scope
  -> server resolves and persists metadata.copilot
  -> ChatAgent continues through normal Breeze Assist runtime
  -> resume/message paths revalidate metadata.copilot for the current user
  -> future Copilot tools load metadata.copilot before data reads
```

## Architecture Fit

Buddy Copilot has two identities that must stay separate:

- Assist runtime identity: the selected chat template and its merchant.
- Copilot data scope: the merchant and optional template whose dashboard data
  tools may read.

`CreateChatSessionRequest.template_id` remains the Assist template id. The
persisted `chat_session.template_id` and `chat_session.merchant_id` still come
from that Assist template. They control the normal ChatAgent behavior, prompt,
and session access rules.

`CreateChatSessionRequest.copilot_scope`, when present, is only a request for
dashboard data scope. The server resolves it through the Copilot scope
foundation and persists the resolved result under `metadata.copilot`.

That means downstream Copilot tools must read data scope from
`metadata.copilot.data`, not from `chat_session.merchant_id`.

## Request Shape

The dashboard should use the normal Breeze Assist chat widget/session creation
path. At the API layer, that remains the existing chat session endpoint:

```http
POST /api/breeze_buddy/chat/session
```

Normal Assist requests continue to work without any Copilot fields:

```json
{
  "template_id": "dashboard-assist-template-id",
  "template_vars": {},
  "metadata": {
    "source": "dashboard"
  }
}
```

A Copilot-enabled dashboard session adds `copilot_scope`:

```json
{
  "template_id": "dashboard-assist-template-id",
  "template_vars": {},
  "metadata": {
    "source": "dashboard"
  },
  "copilot_scope": {
    "data_merchant_id": "merchant-1",
    "data_template_id": "11111111-1111-4111-8111-111111111111",
    "timezone": "Asia/Kolkata",
    "date_range": {
      "date_from": "2026-07-01",
      "date_to": "2026-07-31"
    }
  }
}
```

`data_template_id` is optional. When it is omitted, later Copilot reads should
treat the scope as all agents under `data_merchant_id`.

## Persisted Metadata

Session creation now builds metadata from three sources:

- caller metadata that is safe for clients to provide
- transformed `template_vars`
- server-owned metadata, currently `metadata.copilot`

An abridged persisted shape is:

```json
{
  "source": "dashboard",
  "template_vars": {},
  "copilot": {
    "actor": {
      "user_id": "user-1"
    },
    "data": {
      "data_merchant_id": "merchant-1",
      "data_template_id": "11111111-1111-4111-8111-111111111111"
    },
    "date_window": {
      "timezone": "Asia/Kolkata",
      "date_from": "2026-07-01",
      "date_to": "2026-07-31",
      "source": "request",
      "label": "2026-07-01 to 2026-07-31"
    },
    "capabilities": [
      "get_analytics_summary",
      "query_conversations",
      "get_conversation_detail"
    ]
  }
}
```

`metadata.copilot` intentionally does not store runtime template or runtime
merchant fields. The runtime values are already represented by the
`chat_session` row. Duplicating them inside the Copilot scope would make it
easier for later tools to accidentally read the wrong boundary.

The persisted `metadata.copilot.actor` is intentionally minimal too. It stores
only `user_id` for audit correlation and does not persist the creator's
username, role, permissions, reseller scope, or merchant scope. Session metadata
is returned by session-resume APIs to any user who can access the chat session,
so durable Copilot metadata must not expose the original actor's permission
snapshot.

## Server-Owned Namespaces

Clients cannot provide these metadata keys:

- `metadata.copilot`
- `metadata.template_vars`

`metadata.copilot` is server-owned because it is the authority later tools will
trust. `metadata.template_vars` is server-owned because session creation stores
the transformed template variables there after applying the template payload
schema.

If a client tries to set either key directly, session creation rejects the
request with HTTP 422 before any chat session row is persisted.

## Failure Behavior

When `copilot_scope` is absent, session creation follows the ordinary Buddy
Assist path.

When `copilot_scope` is present, the handler resolves it before persisting the
session. Scope errors are returned as structured HTTP errors:

```json
{
  "code": "unauthorized_merchant",
  "message": "..."
}
```

The same pattern is used for ambiguous merchant selection, unauthorized
template ownership, invalid timezone, and other scope-resolution failures.

For existing sessions, normal chat-session RBAC is still checked first. If the
session contains `metadata.copilot`, dashboard resume/message/approval/cancel/
end/transcript paths also revalidate the persisted Copilot data merchant and
optional data template against the current user before continuing. A user who
can still access the runtime Assist session but no longer has access to the
stored Copilot data scope receives the same hidden 404 shape used by chat
session RBAC.

## Why There Is No Copilot Session Endpoint

Copilot is a full Breeze Assist chat bot. Adding a separate Copilot session
endpoint would duplicate the chat widget/session lifecycle and make Copilot
drift away from the Assist architecture.

This slice keeps the existing lifecycle intact:

- the dashboard hosts the normal Assist chat widget behavior
- the selected Assist template controls runtime behavior
- `POST /chat/session` creates the session
- `POST /chat/message` runs the normal ChatAgent
- tool availability and behavior remain template/runtime concerns
- Copilot data safety is carried as `metadata.copilot`

Routing Copilot-specific tools should happen through the agent template and
the future tool provider/guard, not through a special session API.

## Connection To BZN-40681

BZN-40681 can treat `metadata.copilot` as the durable server-owned data-scope
contract for tool execution.

This slice already revalidates that stored scope on normal dashboard session
access. BZN-40681 should still revalidate and inject the scope at tool
execution time, because the tool boundary is the last server-owned boundary
before analytics or conversation data is read.

The expected tool flow is:

```text
ChatAgent
  -> asks CopilotToolProvider for read-only schemas
  -> model emits tool call without data_merchant_id/data_template_id
  -> provider/guard loads chat_session.metadata.copilot
  -> guard injects scope.data into the executor
  -> executor queries only the scoped merchant/template data
  -> tool result returns scoped structured data with provenance
  -> normal Assist ChatAgent renders prose or existing AI UI
```

The LLM must never be allowed to provide or override
`data_merchant_id` or `data_template_id` in tool arguments. Those values are
already persisted by this session creation slice and must be injected by the
tool boundary.

Copilot should not introduce a Loom-specific rendering contract. If the Copilot
Assist template enables the right `ui_catalog` groups and gives the bot clear
UI instructions, the existing Assist chat UI path can render smart UI through
normal `ui_op` events and persisted `ui_blocks`. Without those template
instructions, the same tool result may simply become a prose answer.

## Invariants

- Copilot uses the existing Buddy Assist chat session endpoint.
- `template_id` is always the Assist runtime template id.
- `chat_session.merchant_id` is runtime identity, not Copilot data scope.
- `copilot_scope` is optional and only affects server-owned metadata.
- `metadata.copilot.data.data_merchant_id` is the data merchant for tools.
- `metadata.copilot.data.data_template_id` is the optional selected agent.
- `metadata.copilot.actor` stores only `user_id`; permission snapshots stay out
  of durable session metadata.
- Clients cannot write or override `metadata.copilot`.
- Invalid Copilot scope requests fail before the session row is persisted.
- Existing Copilot sessions revalidate `metadata.copilot.data` for the current
  user before resume/message/approval/cancel/end/transcript operations.
- Normal chat session creation and RBAC remain unchanged when Copilot is not
  requested.

## Files In This PR

```text
app/api/routers/breeze_buddy/chat/handlers.py
app/api/routers/breeze_buddy/chat/__init__.py
app/schemas/breeze_buddy/chat.py
app/schemas/breeze_buddy/copilot.py
app/services/breeze_buddy/copilot/scope.py
tests/test_copilot_scope.py
tests/test_copilot_session.py
```

## Out Of Scope

This slice does not register Copilot tools, execute analytics or conversation
queries, add AI UI instructions, or add Loom-specific UI behavior.

Those later pieces should consume `metadata.copilot` and preserve the same
runtime-vs-data boundary documented here and in `scope_foundation.md`.
