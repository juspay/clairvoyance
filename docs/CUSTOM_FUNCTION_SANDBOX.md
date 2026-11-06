# Custom Function Sandbox Mode — Architecture

**Status:** Proposed · **Owner:** Voice Platform · **Last updated:** 2026-10-25

Run template **custom functions** (`python_code`) out of process, in a small
service with no secrets, no network and hard limits, so the feature can be
turned back on safely.

Related code: `app/ai/voice/agents/breeze_buddy/utils/parser.py` (compile),
`template/global_function.py` (gate + execute),
`handlers/internal/custom_python_code_handler.py` (call site).

---

## 1. Problem

A template can carry a **custom function**: a short `python_code` snippet defining `handler(args, context)`. The LLM calls it like any other global function; it receives the LLM's arguments plus plain lead data and returns a dict.

Today the snippet is compiled with RestrictedPython and runs **inside the Clairvoyance process** — the same memory, environment, network and CPU as live calls. RestrictedPython is a useful filter, but an in-process interpreter is not an isolation boundary (the module security note in `parser.py` says so). That is why the feature has been gated off by default (`ENABLE_CUSTOM_PYTHON_FUNCTIONS`, PT-01): templates that contain custom functions currently have them skipped at build time.

Two gaps besides isolation:

- **Timeouts do not stop code.** A sync handler runs via `asyncio.to_thread`; on timeout the caller stops waiting, but the thread keeps running.
- **No memory cap.** A handler can allocate until the process is under pressure.

What we verified about today's system (important for the design):

- There is **one compile site** (`global_function.py`, behind the flag) and **one call site** (`execute_custom_function`). One seam to move.
- The handler's `context` is **plain data** (lead dict, `call_sid`, `lead_id`) — no live objects — so it serialises to JSON as-is.
- Existing custom functions in templates are **pure data logic** (string, list and dict operations, own helper functions); none use imports. They can run unchanged in a separate process.
- With the flag off, those functions are **skipped at build**, so the feature is effectively unavailable to the templates that use it.

## 2. Goals and non-goals

**Goals**

1. A custom function can only compute: data in (args + lead data), JSON out. No secrets, no network, no filesystem, no access to Clairvoyance memory.
2. A slow, looping or memory-hungry function can never affect live calls or other merchants.
3. Existing custom-function code runs unchanged (same `handler(args, context)` contract, same RestrictedPython rules as the first filter).
4. Off by default; enabled per merchant; removable without a deploy if anything goes wrong.

**Non-goals**

- New capabilities for custom functions (imports, HTTP, storage). Anything that needs I/O stays an HTTP function or MCP tool.
- Changing how templates are authored or stored.

## 3. Design overview

```
voice-agent / clairvoyance                         fn-runner (separate small image)
  custom function called by LLM                      supervisor
    └── POST /run  (in-cluster only) ─────────────▶    ├── admission control (limits below)
          code, args, lead data, limits                ├── per-merchant warm worker pool
    ◀── {result} | {error, reason} ────────────────    └── worker = one process, one call at a time
```

- **fn-runner** is its own service and its own image (Python + RestrictedPython only; none of the Clairvoyance app, no provider SDKs, no credentials).
- Clairvoyance no longer executes custom code itself; it sends the code and inputs to fn-runner and passes the result (or a short error) back to the LLM.

## 4. Isolation layers (fn-runner)

| Layer | Control |
|---|---|
| Placement | Dedicated node pool (tainted; only fn-runner tolerates it), so it never shares CPU or memory with call-handling pods. Run under a sandboxed container runtime (gVisor / GKE Sandbox) so a process-level escape still does not reach the host kernel. |
| Secrets | No env values, no mounted credentials, `automountServiceAccountToken: false`. Nothing worth reading exists in the pod. |
| Network | NetworkPolicy: ingress only from Clairvoyance workloads on the runner port; **no egress at all** (including DNS). |
| Container | Read-only root filesystem, runs as non-root, all Linux capabilities dropped, seccomp `RuntimeDefault`, `allowPrivilegeEscalation: false`. |
| Worker process | Started by the supervisor with an empty environment and a scratch working directory; kernel limits set before any user code runs: address space (memory), CPU time, no child processes, no file creation, few file descriptors. |
| Code filter | Same RestrictedPython compile and guards as today (`parser.py`), kept as the first layer. |

## 5. Worker model

- **Per-merchant pools.** A worker only ever serves one merchant, so one merchant's code never shares a process with another's.
- **Fresh namespace per call.** The snippet is compiled and executed into a new namespace on every call (~0.5 ms), so nothing carries over between calls or leads.
- **Recycled.** A worker exits after N calls or T minutes (initially 500 calls / 5 min) and is replaced from the warm pool.
- **Killed on any timeout or error.** The supervisor sends `SIGKILL` at the wall-clock deadline; the process really stops, unlike today's thread. The worker is never reused after a kill.

Single-use workers (one process per call) are a stricter option. They cost ~20 ms CPU per call for process start-up, so they are a configuration choice, not the default.

## 6. Limits and admission control

| Limit | Initial value | Why |
|---|---|---|
| CPU time per call | 200 ms | Typical functions need under 1 ms; 200x headroom |
| Wall-clock deadline | template `timeout_seconds`, capped at 2 s | Kill, not just stop waiting |
| Memory per worker | 64 MB | ~12 MB measured at rest |
| Input size (code + args + lead data) | 256 KB | Bounded request |
| Output size | 64 KB | Bounded result to the LLM |
| In flight per merchant | 4 | One busy merchant cannot starve others |
| Queue wait | 200 ms, bounded length | Overload becomes a fast "busy", never a pile-up |

**No-OOM rule.** Per pod, `max running workers = (pod memory limit − supervisor) ÷ per-worker memory cap`. The supervisor never starts more than that; extra requests wait in the bounded queue or get "busy". Memory therefore cannot exceed the pod limit by construction.

**CPU** is bounded by the per-call CPU limit, the pod's CPU limit, and the dedicated node pool.

## 7. Sizing

Measured locally with the current RestrictedPython setup, on a text-matching function of the kind used in templates today:

| Step | Cost |
|---|---|
| Python start + RestrictedPython import | ~17 ms (paid only when a worker starts) |
| Compile snippet | ~0.5 ms |
| One call (~1 KB of text) | ~0.4 ms |
| Worker memory at rest | ~12 MB |

Concurrency = arrival rate × time per call (~1 ms with warm workers):

| Rate | Busy workers on average | CPU |
|---|---|---|
| 15 calls/s | ~0.02 | ~0.02 core |
| 150 calls/s | ~0.15 | ~0.2 core |
| 1,000 calls/s | ~1 | ~1 core |

Initial deployment: **2 small pods** (second for availability), ~0.25 CPU and 256–512 MiB each, HPA on in-flight requests. Load numbers to be re-checked against real invocation metrics after enablement.

## 8. API contract

`POST /run` (in-cluster only)

```json
{
  "merchant_id": "string",
  "function_name": "string",
  "code": "string",
  "code_sha256": "string",
  "args": {},
  "context": {"lead": {}, "call_sid": "string", "lead_id": "string"},
  "timeout_ms": 2000
}
```

Response:

```json
{"status": "ok", "result": {}, "duration_ms": 0.6}
{"status": "error", "reason": "timeout | memory | cpu | compile | busy | runtime | too_large", "message": "short, no internals"}
```

`merchant_id` comes from server-side state (the template / lead), never from template or shopper input.

## 9. Changes in Clairvoyance

1. `template/global_function.py`: `execute_custom_function` calls fn-runner instead of executing in-process. Compile-time validation stays in Clairvoyance (fail early at build); execution moves out.
2. Config: `CUSTOM_FUNCTION_RUNNER_URL` (static) and a per-merchant allow-list for enabling custom functions (dynamic config, so it can be turned off without a deploy).
3. Gate: custom functions build only when the global flag is on, the merchant is allow-listed **and** a runner URL is configured. Otherwise skipped, as today.
4. Failure handling: runner unreachable / busy / timeout returns the same short error the LLM gets for a timeout today; the call continues.
5. After rollout: remove the in-process execution path so custom code can only run in fn-runner.

## 10. Rollout

The change lands in phases, each deployable on its own:

| phase | what it brings | behaviour change |
|---|---|---|
| 1 | this document | none |
| 2 | fn-runner image + manifests (own node pool, NetworkPolicy, limits), deployed idle | none |
| 3 | Clairvoyance client (section 9) behind the per-merchant allow-list | none until a merchant is listed |
| 4 | enable for one merchant; watch latency, error reasons, kills, busy rejects | that merchant's custom functions run again |
| 5 | widen the allow-list | per merchant |
| 6 | remove the in-process execution path | custom code can only run in fn-runner |

Rollback at any step: remove the merchant from the allow-list (custom functions are skipped again, as today).

## 11. Tests

- **fn-runner, unit:** limits enforced (CPU, memory, output, input), kill on deadline, worker replaced after kill, fresh namespace per call, per-merchant pools never mix, admission control never exceeds the memory budget, bounded queue returns "busy".
- **fn-runner, in-cluster checks (CI against a test deployment):** a worker has no environment values, cannot resolve DNS, cannot open any outbound connection (internet, cluster services, node metadata), cannot write the filesystem.
- **Clairvoyance:** client maps every runner reason to the existing LLM error shape; gate builds functions only when all three conditions hold; existing `tests/test_custom_python_sandbox.py` keeps passing.
- **Compatibility:** existing custom functions in templates produce the same results through fn-runner as in-process (golden comparison on a sample).

## 12. Observability

Metrics per merchant and function: invocations, latency (p50/p95/p99), result by reason (`ok`, `timeout`, `memory`, `cpu`, `busy`, `compile`, `runtime`), worker kills, queue wait. Alert on sustained `busy` or kill rate. Also log (at warn) every custom function skipped at build, so a disabled feature is visible instead of silent.

## 13. Open questions

1. Single-use vs reused workers as the default (section 5) — reused is proposed for CPU cost.
2. Where the in-cluster runner URL and allow-list live (static vs dynamic config).
3. Whether chat mode (API pods) and voice (voice-agent pods) share one runner deployment or get one each.
