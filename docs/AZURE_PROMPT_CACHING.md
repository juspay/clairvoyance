# Azure OpenAI Prompt Caching for Pipecat Voice Agents

A practical guide for enabling prompt caching on `gpt-4.1-mini` / `gpt-4o` in our Azure southindia deployment, via Pipecat.

---

## TL;DR

- Azure prompt caching is **auto-enabled** for our models. We're already getting partial benefits but probably leaving most of the value on the table due to unstable prefixes.
- Two changes to fix it:
  1. Freeze the system prompt + tool definitions byte-for-byte; move dynamic context (lead name, time, IDs) into the first user message.
  2. Pass `prompt_cache_key` via Pipecat's `extra` dict in `AzureLLMService.Settings`.
- **Expected impact**: ~80–200 ms TTFB reduction per turn (after turn 1) + ~44% reduction in input token cost.
- **Risk**: low. Behavioral change is minimal — only the system prompt structure shifts.

---

## 1. How Azure prompt caching works

| Property | gpt-4.1-mini | gpt-4o |
|---|---|---|
| Cache enabled by default | Yes | Yes |
| Minimum prompt size to trigger | 1024 tokens | 1024 tokens |
| Increments after that | 128-token | 128-token |
| Discount on cache hit | ~50% input tokens (Standard SKU) / up to 100% (PTU) | Same |
| Default TTL | 5–10 min idle, 1 h hard cap | Same |
| `prompt_cache_retention="24h"` | **Not supported** | **Not supported** |
| Per-tenant isolation | Per-subscription | Per-subscription |
| Hit detection field | `usage.prompt_tokens_details.cached_tokens` | Same |

**Important**: extended 24h retention is **NOT available on gpt-4.1-mini or gpt-4o**. Only gpt-4.1, gpt-5, gpt-5.1.x, gpt-5.2, gpt-5.3-codex, gpt-5.4 support it. For voice agents this is fine — our 5–15 s turn cadence keeps the cache warm within the 5–10 min idle window.

### How matching works

- Cache key: hash of the first ~256 tokens of the prefix, then byte-identical prefix match.
- A single character change in the first 1024 tokens → total cache miss.
- The cache holds the **longest matching prefix** — as the conversation grows, the system+tools+older-turns portion stays cached while only the newest turn is uncached.
- Tool results appended to history do NOT bust the prefix.

### Cache scope and routing

- Scope: per-Azure-subscription. Never shared across subscriptions/tenants.
- At high RPM, requests fan out across multiple backend pods. Each pod has its own cache.
- Without a `prompt_cache_key`, identical requests can land on different pods → cache miss sawtooth at peak load.
- **`prompt_cache_key`** pins routing: requests with the same key prefer the same pod, so the cached prefix gets reused.

---

## 2. Pipecat's current support (verified in pipecat repo)

| Feature | Status | Where |
|---|---|---|
| Read `cached_tokens` from API response | Supported | `services/openai/base_llm.py:442-456` |
| Expose in `LLMTokenUsage` | Supported | `metrics/metrics.py:63` (`cache_read_input_tokens`) |
| Tracing export | Supported | `utils/tracing/service_decorators.py:139-143` (`gen_ai.usage.cache_read.input_tokens`) |
| Context aggregator preserves prefix | Supported | `processors/aggregators/llm_context.py:372` — append-only, system at index 0 |
| `prompt_cache_key` as first-class param | **Not first-class** | Pass via `extra={...}` |
| `prompt_cache_retention` as first-class param | **Not first-class** (and N/A for our models) | Pass via `extra={...}` |
| Documentation / examples | None | Gap |

Pipecat observes caching but doesn't actively configure it. The `extra` dict pass-through at `services/openai/base_llm.py:353` lets you inject anything, including `prompt_cache_key`.

---

## 3. Implementation steps

### Step 1 — Audit current system prompt for cache busters

For caching to work, the prefix must be **byte-identical** across turns. Common cache busters in voice agents:

| Cache buster | Where it leaks | Fix |
|---|---|---|
| `"You are talking to {lead_name}"` | System prompt | Move to first user message |
| `"Current time: 2026-05-18 16:30 IST"` | System prompt | Move to first user message |
| `call_sid` / `conversation_id` in instructions | System prompt | Move to first user message |
| Tool order varying between calls | Tools array | Sort alphabetically, freeze |
| Few-shot examples reordered | System prompt | Freeze order |
| Summary of older history injected at top | System prompt | Keep summaries at end, or only refresh on session boundaries |

### Step 2 — Restructure messages

```python
# BEFORE — every call's prefix is different
system = f"""You are BreezeBuddy speaking with {lead_name}.
Current time: {now_iso}.
Order context: {order_json}.
[... rules + 8 tool descriptions ...]"""

# AFTER — prefix is frozen, dynamic content moved
system = STATIC_SYSTEM_PROMPT  # No interpolation. Loaded once at module init.
context.add_message({"role": "system", "content": system})

# Dynamic context as the FIRST USER MESSAGE (after the prefix)
context.add_message({
    "role": "user",
    "content": (
        f"[Call metadata] lead={lead_name} time={now_iso} order={order_id}\n"
        "Acknowledge the customer and proceed with the flow."
    )
})
```

### Step 3 — Lock tool order

In tool registration (e.g. `chat/agent.py`):

```python
# Sort by name once, at module load — never reorder per call
TOOLS = sorted(raw_tools, key=lambda t: t["function"]["name"])
```

If tools change dynamically per flow node, this is the biggest hidden cache buster. Options:
- Freeze the tool set per "agent template" version
- Accept lower hit rate for nodes with dynamic tools
- Group tools into static + dynamic; the static portion still caches

### Step 4 — Pass `prompt_cache_key` via Pipecat

```python
from pipecat.services.azure.llm import AzureLLMService

llm = AzureLLMService(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    endpoint="https://breeze-automatic.openai.azure.com",
    api_version="2024-10-21",  # or newer
    settings=AzureLLMService.Settings(
        model="gpt-4.1-mini",  # your deployment name
        extra={
            # Per prompt-template version, NOT per session/user.
            # Per-user would defeat caching entirely.
            "prompt_cache_key": "breeze-buddy-prompt-v3",
        },
    ),
)
```

**Cache key rule**: per-prompt-template, not per-user. A per-user key gives every call its own pod with zero shared cache. We want all calls on the same template sharing the same backend pod.

When the system prompt or tool set changes meaningfully (template version bump), increment the key suffix (`-v4`).

---

## 4. Expected impact

For a 3000-token prefix (system + 8 tools + few-shots) with ~200 tokens of conversation growth per turn, on `gpt-4.1-mini` Standard regional in southindia:

| Metric | Before (unstable prefix) | After (cache-stable) | Delta |
|---|---|---|---|
| TTFB (turn 2+) | ~500 ms | **~300–420 ms** | **−80 to −200 ms** |
| Input tokens billed at full rate | ~3200 / 3200 (100%) | ~400 / 3200 (12%) | ~88% cached |
| Effective input cost reduction | — | **~44%** | |
| Cache hit rate (after turn 1) | Sawtooth, 30–50% | **>95%** | |

Cold cache on turn 1 still pays the full TTFB. For a voice agent that's fine — the first turn is usually a greeting.

---

## 5. Monitoring

Pipecat already exposes the metric. Add to phase logger or any metrics observer:

```python
# In on_metrics_data or equivalent
if metrics.tokens:
    cached = metrics.tokens.cache_read_input_tokens or 0
    total = metrics.tokens.prompt_tokens
    hit_rate = (cached / total) if total else 0
    logger.info(f"[CACHE] {cached}/{total} = {hit_rate:.1%}")
```

**Target**: `hit_rate > 0.80` from turn 2 onward.

**Quick smoke test**: send the same 1500-token-prefix request twice back-to-back. Second response's `cached_tokens` should be ~1408. If it's 0 on turn 2, cache isn't being hit.

**Azure-side**: Azure Monitor → Azure OpenAI resource → Metrics → split `Processed Prompt Tokens` vs `Cache Read Input Tokens`. Track the ratio over time.

---

## 6. Rollout checklist

Before pushing to production:

- [ ] Move dynamic content (lead name, time, IDs) out of system prompt, into first user message.
- [ ] Freeze the system prompt — load once, no per-call interpolation.
- [ ] Sort tools alphabetically at module load. Never reorder per-call.
- [ ] Add `extra={"prompt_cache_key": "breeze-buddy-prompt-v3"}` to `AzureLLMService.Settings`.
- [ ] Verify the LLM still references the caller correctly in test conversations (since the lead name moved out of the system prompt).
- [ ] Confirm `cache_read_input_tokens > 0` in observability after warm-up.
- [ ] A/B on 5% traffic for 24h. Compare TTFB P50/P95 + tool-call accuracy vs control.
- [ ] Roll out gradually: 5% → 25% → 100%.

---

## 7. Things to NOT do

- ❌ **Don't use a per-session `prompt_cache_key`** — defeats sharing across calls, kills hit rate.
- ❌ **Don't try `prompt_cache_retention="24h"`** on gpt-4.1-mini / gpt-4o — silently ignored or errors. Only works on gpt-4.1, gpt-5, gpt-5.1.x, gpt-5.2, gpt-5.3-codex, gpt-5.4.
- ❌ **Don't restructure tools dynamically** between calls unless you accept the lower hit rate.
- ❌ **Don't put summarized history at the start of the prefix** — summarization output changes per call and busts cache. Keep summaries at the END of the static region, or only refresh on session boundaries.
- ❌ **Don't skip the `extra={}` step** — without `prompt_cache_key`, at our 10–50 RPM peak we see sawtooth hit rates as different calls land on different pods.
- ❌ **Don't put `Date.now()` or any timestamp in the system prompt** — single biggest cache buster.

---

## 8. Other caching options (orthogonal)

| Layer | What | Win for our use case |
|---|---|---|
| **TTS audio cache** | Pre-rendered audio for common phrases (greetings, "please hold", goodbyes) | **Biggest practical win for telephony** — saves entire TTS round-trip |
| **Application FAQ cache** | Short-circuit deterministic intents before LLM | Wins on ~20% of turns if applicable |
| **Context summarization** | Compress older turns | Reduces token cost; orthogonal to caching |
| **Azure APIM semantic cache** | Response cache keyed on embedding similarity | Mostly for FAQ bots; less useful for stateful voice |

These are complementary to prompt caching — they target different parts of the latency budget.

---

## 9. Key file references (pipecat upstream)

- `src/pipecat/services/azure/llm.py` — Azure service entry point (inherits OpenAI settings)
- `src/pipecat/services/openai/base_llm.py:126` — `OpenAILLMSettings.extra` field
- `src/pipecat/services/openai/base_llm.py:353` — `params.update(self._settings.extra)` (where `extra` is merged into the API call)
- `src/pipecat/services/openai/base_llm.py:442-456` — `cached_tokens` capture from streaming usage
- `src/pipecat/metrics/metrics.py:63` — `LLMTokenUsage.cache_read_input_tokens`
- `src/pipecat/processors/aggregators/llm_context.py:372` — append-only context (prefix-safe)
- `src/pipecat/utils/tracing/service_decorators.py:139-143` — tracing export

---

## 10. Sources

- [Microsoft Learn — Prompt caching with Azure OpenAI](https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/prompt-caching)
- [Microsoft Foundry — Prompt caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching)
- [OpenAI — Prompt Caching in the API](https://openai.com/index/api-prompt-caching/)
- [Azure OpenAI Pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/)
- [MS Q&A — Extended Prompt Cache Retention support](https://learn.microsoft.com/en-us/answers/questions/5807188/does-azure-openai-support-extended-prompt-cache-re)
