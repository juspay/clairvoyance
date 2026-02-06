# Breeze Buddy Public Demo Endpoint — Frontend Integration Guide

Endpoint
- `POST /agent/voice/breeze-buddy/demo/connect` (no auth)

Request body (all required unless noted)
- `agent`: must be `"order-confirmation-demo-agent"` (any other value is rejected)
- `customer_name`: string
- `customer_mobile_number`: string (stored only for the demo payload)
- `shop_name`: string
- `total_price`: string
- `items`: array of `{ "product_name": string, "quantity": number >= 1 }` (min 1 item)
- `language` (optional): string hint, e.g., `"English"`

Data retention
- Demo payloads (including customer_mobile_number) are stored as part of the lead payload, flagged with `is_demo=true` and `demo_expires_at` (7 days by default via `DEMO_TTL_DAYS`). Intended for temporary demo use and downstream cleanup.

Responses
- 200: `{ room_url, token, session_id, lead_id, template, ttl_days }`
- 4xx: validation error (missing/invalid fields, unsupported agent)
- 404: demo template not configured for `website-demo`
- 429: rate limit exceeded (10/hour, 50/day per IP; Redis-backed)
- 502: failed to start Daily session (lead is aborted automatically)

Behavior
- Creates a demo lead with merchant/shop `website-demo`, execution_mode `DAILY_TEST`, `is_demo=true`.
- Starts a Daily session and returns credentials; if room/token creation fails, the lead is aborted to avoid backlog clutter.

Frontend checklist
- Always send the required fields above (no server defaults).
- Restrict the agent picker to `"order-confirmation-demo-agent"` for now.
- Handle 429 and 502 gracefully (show retry/limit messaging).
- Do not expose phone/email beyond the fields listed; avoid large payloads.
