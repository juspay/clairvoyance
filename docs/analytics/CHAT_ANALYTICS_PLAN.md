# Chat Analytics — execution plan

**Status:** Phase 1 in progress (this branch `feat/chat-analytics-conversational-logs`).
**Scope:** Backend for two product surfaces, modelled on the reference UI
(Chatbase-style: *Activity → Chat logs* and *Analytics → Chats*):

1. **Conversational logs** — per agent (= template): the full transcript of a
   session (what the user asked, what the LLM said, **and the UI cards the user
   actually saw**), with **per-turn latency**. List + detail.
2. **Per-template aggregates** — total conversations, total messages, and a
   "chats started" time-series, grouped/filtered by template.

> **v1 scope (decided):** logs + aggregates only. Thumbs-up/down, Topics, and
> Sentiment are **deferred** — they are net-new capture/NLP not wired anywhere
> today. v1 does not add any capture for them.

## What already exists (no work needed)

The substance of deliverable 1 is already persisted per turn:

- `chat_session` (`migration 027`/`030`) — `template_id` (**indexed**),
  `reseller_id`, `merchant_id`, `status`, `outcome`, `created_at`,
  `last_activity_at`, `ended_at`, `current_channel`. Dashboard index
  `idx_chat_session_dashboard` + `idx_chat_session_template_id`.
- `chat_message` (append-only, `(session_id, idx)` PK) — `role`, `content`
  (prose), `content_blocks` (canonical Anthropic blocks incl. tool_use/result),
  **`ui_blocks`** (the rendered SpecStream ops = *what the user saw*), `created_at`.
- `GET /chat/session/{id}/transcript` already returns the full message list with
  `ui_blocks` (sanitized to strip `visibility=internal` blocks). The log-detail
  view reuses this verbatim.

## The gaps

| Need | Status | Plan |
|---|---|---|
| Full transcript content (user / LLM / UI) | ✅ have it | `/transcript` reused as-is |
| Per-turn **latency** | ⚠️ logged only (`[CHAT_METRICS]` → OpenObserve) | **persist** a joinable row (Phase 1) |
| **List sessions** for a template (the log rail) | ❌ absent | new `GET /chat/sessions` (Phase 1) |
| Per-template **aggregates** (counts, time-series) | ❌ absent (`/analytics` is voice-only) | new `CHAT_BASED` analytics type (Phase 2) |
| Thumbs / Topics / Sentiment | ❌ absent | **deferred** (out of v1) |

## Why latency goes in the DB (not just logs)

`[CHAT_METRICS]` is a backend log line — right for ops/aggregate dashboards,
wrong for a per-conversation product view: a log line can't be cleanly joined to
"message #7", and logs have retention limits. So Phase 1 mirrors the *same*
`TurnMetrics` object the router already computes into a small **side-channel
table** keyed by the assistant message's `idx`. The hot message-insert path and
the agent's turn loop are untouched — this is a pure addition.

## Phase 1 — Conversational logs (this branch)

### 1A · List endpoint (read-only, no schema change)
- `GET /chat/sessions` — paginated, filter by `template_id` / `status` /
  `date_from` / `date_to`; scoped by the caller's reseller/merchant via the
  existing `apply_hierarchical_filters`. Returns `ChatSessionSummary`
  (session fields + `message_count` + last-message `preview`) + pagination.
- Query/accessor/decoder mirror the existing chat trio; reuses
  `idx_chat_session_dashboard` / `idx_chat_session_template_id`.

### 1B · Persist per-turn latency (additive side-channel)
- **Migration `032_create_chat_turn_metrics.sql`** — `chat_turn_metrics`
  keyed `(session_id, idx)`, FK to `chat_session ON DELETE CASCADE`, holding
  the structural fields `TurnMetrics` already computes (`ttft_ms`, `ttfui_ms`,
  `ttlui_ms`, `total_ms`, `ui_ops`, `ui_dropped`, `healer_applied`,
  `tool_calls`, `prose_chars`, `ui_chars`, `status`, `phase`).
- **Agent**: `turn_end` SSE event gains an additive `assistant_idx` (the final
  assistant `chat_message.idx` of the turn) so the router can key the row even
  on UI-only / no-prose turns. No other agent change.
- **`TurnMetrics`**: captures `assistant_idx` from `turn_end`; stays a passive
  observer.
- **Router**: in the stream's `finally`, *after* lock release, best-effort
  persist the metrics row (only when `assistant_idx` is present). Never blocks
  or breaks the turn; `[CHAT_METRICS]` log keeps firing as before.
- **Surfacing**: `/transcript` and `GET /chat/sessions` detail can `LEFT JOIN`
  the metrics so the FE shows latency next to each assistant turn. (v1 exposes
  metrics via a sibling list; the FE joins by `idx`.)

## Phase 2 — Per-template aggregates (DONE)
- New `CHAT_BASED` analytics type in the existing `/analytics` dispatcher,
  mirroring the voice pattern. `build_chat_analytics_where_clause` reuses the
  voice IST date handling (`convert_ist_to_utc`, inclusive `date_to`) over
  `chat_session`; `get_chat_analytics_summary_query` (+ `group_by='template'`)
  → `COUNT(DISTINCT cs.id)` total conversations, status breakdown,
  `COUNT(m.session_id)` total messages, `total_agents`, avg msgs/conv;
  `get_chat_analytics_trends_query` → `DATE_TRUNC(day|week|month, created_at)`
  "chats started" series. `get_chat_based_analytics` handler reuses
  `_format_time_bucket`. New `queries/breeze_buddy/chat_analytics.py` +
  `accessor/breeze_buddy/chat_analytics.py`; `template_id` added to
  `AnalyticsFilters`. 6 query-builder tests. pyrefly clean.
- **FE (Loom):** `(app)/chats/analytics` — stat cards (total conversations /
  messages / ended / avg) + a "Chats started" layerchart area chart
  (`ChatsStartedChart.svelte`) + a by-agent table; agent/range/granularity
  filters. Nav "Chats → Analytics" added. `fetchChatAnalytics` posts
  `type:"chat-based"` to the shared `/analytics` endpoint.

## Frontend (Loom — separate repo)
- *Chat logs*: session-list rail (`GET /chat/sessions`) + transcript pane
  (`/transcript`) that **replays `ui_blocks` through the same `applyOp` renderer
  the live widget uses**, so the log shows the real cards; latency badge per
  turn from the metrics join.
- *Analytics → Chats*: stat cards + "chats started" chart from the Phase-2
  endpoint.
- Not implemented from this repo (Loom is not mounted here); specced above.

## Non-negotiables
- **Pure addition.** Nothing already-live changes behavior; new tables/columns
  and new endpoints only. Logs never carry user/tool payload content.
- **Reuse the conventions.** Three-layer DB pattern, the analytics RBAC, the
  existing transcript sanitization.
- **Don't over-engineer.** Defer thumbs/topics/sentiment until the basics ship
  and the data says they're worth it.
