# Fleet census — 2026-07-11 (prod, read-only)

Source: `scripts/fleet_census.sql` + `scripts/orphan_merchants.sql` + two ad-hoc
read-only detectors, run against prod (`clairvoyance_db`) on 2026-07-11.
Purpose: size the "remove shared templates → one template per merchant" backfill.

## Landscape

| Reseller | Active merchants | Shared templates | Merchant-owned templates |
|---|---|---|---|
| **BB_SHOPIFY** | **539** | **9** | 61 |
| breeze | 20 | 0 | 80 |
| nammayatri | 3 | 0 | 36 |
| acme | 0 (no merchants rows) | 1 | 57 |
| others (redbus, fresh-bus, woocommerce, …) | ≤1 each | 0–1 | few |

Shared templates exist only under BB_SHOPIFY (9), acme (1), BB_SUPER_MONEY (1).
The backfill problem is effectively **a BB_SHOPIFY problem**.

## BB_SHOPIFY shared templates — what actually matters

30-day lead traffic on shared rows:

| Shared template | Leads/30d | Distinct merchants served | Own copies exist |
|---|---|---|---|
| **order-confirmation** | **53,068** | **225** | 31 (508 of 539 lack one) |
| abandoned-checkout | 1,591 | 8 | 1 |
| whatsapp-recovery-test | 102 | 1 | 0 |
| abandoned-recovery | 1 | 1 | 0 |
| other 5 (Pantaloon-test, apr21, demo-chat, mcp-test, cc-upsell) | 0 | 0 | 0 |

**Intelligent-scope conclusion**: only `order-confirmation` (all merchants) and
`abandoned-checkout` (8 active + product call on the rest) deserve per-merchant
copies. The other seven shared rows are test/demo debris — archive
(`is_active=false`) after a final traffic check; do NOT multiply them ×539.

## Green lights

- **Every lead in the last 30 days carries `merchant_id`** — pushers always send
  merchant identity; the merchant-first resolution fallback can work.
- **Zero widget configs point at shared templates** — no chat cutover surface.
- **501/539 merchants already have their own calling config** — config backfill
  is ~38 rows plus verification.
- Copies carrying `outbound_number_id` is existing practice (the 31
  order-confirmation copies hold numbers, 7 distinct).

## Flags

1. **Pinned template_id suspicion (decides the cutover mechanism).** 11
   merchants received copies on 07-02/07-06 yet their leads kept resolving to
   the SHARED row for days after (291/264/115 leads), stopping ~07-06/07.
   Consistent with the pusher sending a pinned `template_id` whose per-merchant
   mapping was updated late. **Must confirm what the lead pusher sends
   (template name vs template_id) before designing the flip.** If id-pinned,
   creating copies changes nothing until the pusher's mapping updates — the
   cutover would be driven upstream, not by the DB fallback.
2. **notclass gap**: 514 orphan merchants (502 BB_SHOPIFY, 11 breeze, 1 with a
   NULL reseller_id), onboarded 2026-03-24 → 2026-07-09, 198 with live traffic.
   Growing continuously — without an onboarding hook or reconciler the backfill
   rots the day it finishes. `scripts/orphan_merchants.sql` is the standing
   detector.
3. **breeze-reseller orphans have NO fallback** ("no shared templates either") —
   11 merchants incl. `redbus` (which also exists as its own reseller) — likely
   broken or misfiled onboarding. Hygiene.
4. **nammayatri: 19,494 leads/30d whose `template_id` matches NO template row**
   (deleted templates?) — analytics/debugging is broken for these. Separate
   hygiene alarm, not part of this backfill.
5. Naming hygiene: `SuperMoneyBreeze` vs `SUPERMONEY_BREEZE`; one merchant with
   NULL reseller_id; `merchants` table has no rows for legacy resellers
   (acme etc.) — orphan report is only trustworthy for the notclass era.

## Proposed backfill work-list (BB_SHOPIFY first)

0. **Preflight**: confirm pusher semantics (flag 1); read the inbound
   `telephony/answer` number→template routing to define how copies + shared
   number coexist; confirm Cloud SQL backup/snapshot before any write.
1. **order-confirmation → 508 copies** (539 minus 31 existing), each stamped
   `lineage: {family, base_version}` in flow metadata, prompt restructured into
   managed-CORE / MERCHANT-ADDITIONS sections at copy time; keep the shared
   `outbound_number_id`; create calling configs for the ~38 merchants missing
   one.
2. **abandoned-checkout** → copies for the 8 active merchants (product call on
   the remaining 531).
3. **Archive the 7 junk shared rows** after a 7-day zero-traffic confirmation.
4. **Retire the shared rows** for migrated families only after per-merchant
   traffic is confirmed flowing to copies (the fallback keeps them harmless as
   a safety net until then; deleting them is the last step, not the first).
5. **Provisioning reconciler** for notclass-created merchants (drain the orphan
   report automatically) — otherwise repeat forever.

## Rules of engagement used

- Every DB action announced in chat beforehand; session forced
  `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` for all census work.
- Credentials never written to disk; provided per-session by the operator.
