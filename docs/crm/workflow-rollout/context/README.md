# context/ — everything an agent needs to know before touching a phase

| File | What it is | Read when |
|---|---|---|
| `reading-notes.md` | The complete read-through of `app/crm` (shared, platform, identity, record, connectivity, outreach), migrations 048–061, the CI boundary guard and the workflow tests (§0–8); verification results (§9); the one-page model of the event → run → send path (§10); the tally of 4 bugs / 10 probable issues (§11); the abandoned-cart flow, the three follow-up questions and the PR #1041 review (§12); the loan-onboarding funnel and Option A (clocks) vs Option B (one board) (§13); Option B written out, the plain-language reasoning, industry practice, the version-pinning canon decision, the recommendation (§14); the end-to-end plan and what "one system" means (§15); both flows in the final vocabulary and the functional gaps G1–G12 (§16). | Always, in full, before phase 00. Then the sections each phase cites. |
| `nits.md` | N1–N21: consistency, hygiene and doc points. Several are picked up by phases (N18–N20 in 00); the rest sit in `99-backlog.md`. | Before phase 00; skim later. |

How the notes map to the queue:
- §11 bugs B1–B4 → phases 01, 02; P1 → 03; P2 → 04; P9/P10 → 00.
- §12 cart flow + Q2 (cart→order attribution) → 06, 07; Q1 (debounce) → 00.
- §13/§14 loan funnel: Option A → 07 (clocks now); Option B prerequisites → 15, 16, 17; version pinning → 10–14.
- §16.3 gaps: G1 → 19; G2/G3 → 18; G7 → 06; G8 → 16; G9 → 09; G12 → 08; G4, G5, G6, G10, G11 → 99-backlog.

Vocabulary the notes use:
- **Clock** = a small plan (one wait + one action) with runs of minutes; **board** = a multi-node journey plan with runs of days–weeks. Same engine (`crm_workflow`); the difference is document size and run length.
- **Pin vs migrate** = whether in-flight runs keep the definition version they entered under (pin, ADR in phase 10) or follow the newest publish (migrate, today's behaviour, validator-gated).
- **Spine** = `crm_event_raw`; **letter** = one event row; **token** = a `crm_workflow_enrollment` row; **walker** = the outreach worker that moves tokens; **gate** = the dispatcher's send-time permission check.

Decisions recorded here that later agents must not reopen: the in-call WhatsApp builtin is dropped; goals have tiers with a payload key (06); `pin` is the default publish mode (10/11); the loan funnel ships as clocks first and becomes a board at phase 17.
