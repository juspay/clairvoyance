# Security end-to-end suites

These launch the **real application** with Uvicorn on loopback and drive the
**real mounted routes** over real HTTP, with provider signatures produced by the
providers' own SDKs. Unit tests build Starlette requests in process; only these
prove the wiring behaves when a real client talks to a real server.

```bash
uv run pytest tests/e2e_security/ -v
```

## Isolation

Every run uses an explicit allowlisted environment (`conftest.sealed_env`). The
developer's environment and `.env` are **not** inherited — either could carry a
real database URL or provider credential into the run. Postgres, Redis, the
dispatcher, background tasks and tracing are all off; all credentials are
synthetic values defined in `conftest.py`.

Nothing here may contact a non-loopback address. The SSRF suite asserts on a
local server's request ledger, which is the only way to show that a blocked
destination was never contacted — a mocked transport cannot demonstrate that.

## What these cannot prove

- Real Twilio/Plivo/Exotel delivery from provider infrastructure.
- Deployed ingress path-prefix rewriting (`TELEPHONY_WEBHOOK_PATH_PREFIX`).
- Redirect-hop re-validation: strict mode blocks the loopback first hop, and the
  private-egress hatch disables per-hop checks, so neither posture exercises it
  over real sockets. Covered deterministically in `tests/test_ssrf_egress.py`.

`test_ssrf_egress_e2e.py` skips until the SSRF egress guard lands (PR #987).
