# Automatic → Breeze Buddy Feature Parity

**Goal:** Retire the **Automatic** voice agent and serve its functionality from **Breeze Buddy** (BB), since BB can now do everything Automatic does through its template + chat/voice-as-chat architecture.

**Status:** ✅ **Code removed.** The entire Automatic agent tree, its Daily room/voice-agent process pools, its router + endpoint, its schemas, `validate_automatic_request`, and all Automatic-only config were deleted (562 tests pass, `pyrefly` clean, `app.main` imports clean). Analytics functionality is migrated to an **Automatic template-driven Breeze Buddy workflow** (analytics tools as global HTTP functions / MCP on a template). Internet search is provided the same way; TEST-mode dummy data was dropped.

**Remaining (deferred — not blocking the removal):**
- **Charts / UI generation** — BB chart components not yet built (deferred, low priority).
- **Push-to-talk** — must be implemented in BB voice-as-chat for full web-voice parity.
- **Live frontend caller** — whatever still POSTs to the old `/agent/voice/automatic` must be migrated to the BB widget/voice surface (operational, to be closed separately).

---

## What each agent is

| | Automatic | Breeze Buddy |
|---|---|---|
| Purpose | Free-form **analytics voice assistant** (web/mobile) | **Template/flow-driven** telephony + chat + widget agent |
| Transport | Daily.co WebRTC + RTVI; process/room pre-warm pools | Telephony (Twilio/Plivo/Exotel), Daily web, chat over SSE, voice-as-chat STREAM mode |
| Conversation | Open-ended Q&A, no script | Authored node graph, LLM-driven transitions |
| Tools | ~30 **hardcoded** analytics/ops tools | Extensible: global HTTP functions + MCP |

**Entry points to remove eventually:**
- `POST /agent/voice/automatic` and `app/api/routers/automatic.py` (wired in `app/main.py:394-584`)
- `app/ai/voice/agents/automatic/` (whole tree, 64 py files)
- `app/helpers/automatic/` (daily_room_pool, process_pool, session_manager)
- `app/schemas/automatic_voice/`
- `validate_automatic_request` in `app/core/security/jwt.py:257`
- Automatic-specific config in `app/core/config/static.py` / `dynamic.py`

---

## Parity matrix

| Capability | Automatic (today) | Breeze Buddy | Status |
|---|---|---|---|
| STT / TTS / LLM providers | Soniox/Deepgram/Sarvam/Google/OpenAI; ElevenLabs/Cartesia/etc. | Same set, template-configured | ✅ **Parity** |
| HITL (write-op confirmation) | RTVI confirm/reject for dangerous ops | `template/approval.py` — chat + voice, globals + MCP | ✅ **Parity** |
| MCP / dynamic tool calling | Breeze MCP client | Full MCP client (discovery + declared schemas, pooling, transforms) | ✅ **BB superior** |
| External API tool calls | Hardcoded HTTP per tool | Global HTTP functions (auth, JMESPath, SSE, async) | ✅ **BB superior** |
| Context summarization | `ContextSummarizer` (on by default) | `chat/context_compactor.py` | ✅ ~Parity |
| Cross-session memory (mem0) | **Dead code** — pkg removed in PR-582 (`__init__.py:50,507`) | None | ✅ **Moot** (neither has it) |
| **Analytics tool suite (~30 tools)** | Juspay/Euler offers + analytics (12), Breeze analytics (6), Breeze config (3), system | Authored as a BB **template** (HTTP global functions / MCP) | ✅ **Resolved** — template created |
| **Internet search** | `gemini_search_fn`, `ENABLE_SEARCH_GROUNDING=true` | Provided via the template (HTTP/MCP) | ✅ **Resolved** — template created |
| TEST-mode dummy data | ~25 dummy analytics fns for demos | n/a | ⚪ **Dropped** — not needed |
| Web/widget voice transport | Daily WebRTC + RTVI | Voice-as-chat STREAM mode, RTVI wired | 🟡 **Mostly** — see push-to-talk |
| **Charts / UI generation** | 4 chart tools + highlight filter + RTVI emission (`ENABLE_CHARTS`) | UI catalog has Carousel/Tile/Table; chart components are commented-out TODO (`ui_catalog.py:726`) | ❌ **Open — deferred** |
| **Push-to-talk (PTT)** | `processors/ptt_vad_filter.py` + RTVI ptt-start/end/sync | Not implemented in BB voice-as-chat | ❌ **Open — must do** |

---

## Open items (block deletion)

### 1. Charts / UI generation — *deferred, not a priority*
- **Automatic side:** `tools/charts/` (generate_bar/line/donut/single_stat), `features/charts/` (turn limiter, highlight filter, RTVI emission). Gated by `ENABLE_CHARTS` (default `false`).
- **BB side:** UI catalog (`template/ui_catalog.py:726`) lists `LineChart/BarChart/PieChart/AreaChart` as commented-out future work. The `<ui_stream>` wire contract + UI block system already exist; only the chart component schemas + client rendering are missing.
- **Decision:** Match later. Not blocking near-term migration, but must exist before any Automatic flow that ships charts is cut over.

### 2. Push-to-talk — *required, must implement*
- **Automatic side:** `processors/ptt_vad_filter.py` drops VAD frames while PTT is held (0.5s cooldown, 60s stuck-safety), driven by RTVI `ptt-start` / `ptt-end` / `ptt-sync` client messages.
- **BB side:** the voice-as-chat STREAM bridge does not yet honor a PTT contract.
- **Decision:** We will have to build PTT into BB voice-as-chat for full web-voice parity.

---

## Operational gate (not a code item)

A live frontend still calls `POST /agent/voice/automatic`. It is **not** the `anchor` repo and **not** the loom widget (already pivoted to BB voice-as-chat) — most likely the Breeze/Euler merchant dashboard. It depends on the endpoint's request schema (`eulerToken`, `breezeToken`, `ttsService`, `mode=TEST/LIVE`) and the RTVI/web-voice contract.

**Before deletion:** find this caller, migrate it to the BB widget/voice surface + analytics template, dark-launch behind a flag, then remove the Automatic tree.

---

## Removal checklist

- [x] STT/TTS/LLM, HITL, MCP, HTTP tool-calling parity confirmed
- [x] Analytics tool suite re-authored as a BB template
- [x] Internet search provided via template
- [x] TEST mode confirmed not needed
- [x] Delete `automatic/` tree, pools, router, schema, `validate_automatic_request`, and Automatic-specific config
- [ ] Charts / UI generation built in BB (deferred)
- [ ] Push-to-talk implemented in BB voice-as-chat
- [ ] Identify the live frontend caller of `/agent/voice/automatic`
- [ ] Migrate that caller to BB; validate web-voice E2E (RTVI, PTT)
- [ ] Dark-launch behind a flag

## What was deleted (for reference)

- `app/ai/voice/agents/automatic/` (whole agent tree)
- `app/helpers/automatic/` (daily_room_pool, process_pool, session_manager)
- `app/schemas/automatic_voice/`
- `app/api/routers/automatic.py` + the `POST /agent/voice/automatic` endpoint and pool init/shutdown in `app/main.py`
- `validate_automatic_request` in `app/core/security/jwt.py`
- Automatic-only constants in `app/core/config/static.py` (AUTOMATIC_*, MEM0_*, the Daily/voice-agent pool config, charts/summarization/search/HITL-confirmation/Juspay-analytics/BRET-MCP vars)
- Schema re-exports in `app/schemas/__init__.py` and `app/schemas.py`; `**/automatic/**` exclude in `pyproject.toml`
