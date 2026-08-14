# Questions for team lead — Template Lineage & Family

*(My notes before our review. Current status: everything below is already built and working on the feature branch — nothing is merged yet, so any answer can still change the design cheaply.)*

## 1. Family table stores the template content inside it — confirm this is what you wanted

You told me the family table should store the template itself, not just an ID. So `template_family` now has the same content columns as `template` (flow, configurations, schemas, supported channels). The parent template is NOT a row in the `template` table — which also means it can never receive a call by accident. Is this what you had in mind?

## 2. I also created a `template_family_version` table — so the family template itself can be reverted. Was that right?

While building, I realized: child templates get version history and rollback, but the family's own template content had no history — if someone edits the family template wrongly, there was no way to get the old content back. So I added a `template_family_version` table (same idea as `template_version`: every time someone edits the family, the old content is saved, and there's a rollback API for it). It's also needed later for the merge feature. Is this table OK, or do you think it's too much?

## 3. When someone reverts a bulk operation, should the FAMILY template also revert automatically?

Today it works like this: reverting a bulk operation puts every CHILD template back to its previous version. That holds as long as each child's pre-operation snapshot still exists — for an older rollout whose snapshots have since been pruned by retention, the revert is refused outright (all-or-nothing: it reports the affected members and writes nothing, rather than reverting some children and leaving others). But the family template stays as it is, because maybe the lead wants to fix the family content and re-apply instead of losing his edit. I made the family revert OPTIONAL — a checkbox on the revert screen ("also revert the family template"). Question: should it stay optional like this, or should the family always revert together with the children automatically?

## 4. Bulk operation history has no description field — should we add one?

The rollout history page shows: which family, which templates, what changed (the exact patch), who did it, and when. But there's no place to write a human note like "added recording disclosure line to all greetings". Should I add an optional description box when applying, so the history is easier to read later? (One small column, easy to add now since nothing is deployed.)

## 5. We keep only the last 10 versions of each template — is 10 the right number?

Old versions get deleted automatically (with a safety rule: versions needed for reverting a recent bulk operation are never deleted). The number 10 is configurable by env variable. Should it stay 10? And should the family template versions use the same limit?

## 6. Merge conflicts are resolved field-by-field, not line-by-line — OK?

When a family update conflicts with a merchant's own edit, the person resolving picks per FIELD (for example, the whole greeting prompt): keep the merchant's text, take the family's text, or write a combined/custom text. We don't merge individual lines inside a prompt automatically, because auto-merged prompts can come out grammatically broken and go straight into live calls. Is that acceptable?

## 7. The raw bulk-update API is admin-only and not shown in the dashboard — OK?

There are two ways to change many templates: (a) the dashboard flow — edit family, preview, resolve conflicts, apply; (b) a raw API where an admin sends a JSON patch directly — meant only for emergencies (like a dead webhook URL on 200 templates at 2 AM). The dashboard never uses (b). Keep it that way?

## 8. Secrets are never copied into the family — confirming

When a family is created by copying an existing template, we deliberately do NOT copy secrets, and auth tokens inside configurations are stored and returned masked. Merchants' real secrets stay only on their own templates. Just confirming this matches what you expect.

## 9. Should the Families tab and its read APIs be admin-only, or stay viewable for everyone?

Right now it's split: creating/editing families and running bulk operations is admin-only, but *viewing* families (the Families tab, and the two read APIs behind it) is open to any console user — scoped to their own reseller, so nobody can see another reseller's families. The idea was that non-admins might find it useful to see what the canonical family template looks like. Should it stay like this, or do you want the whole Families tab and its read endpoints locked to admin-only as well? (The Rollouts tab with the revert button is already admin-only either way.)

## 10. Only `flow` and `configurations` flow from family into child templates — is that enough?

When a family update is applied to children (bulk update today, the merge flow later), only two things can be pushed into the children: the flow (prompts/nodes) and the configurations (voice, language, STT/TTS settings). Everything else NEVER flows: payload schema, callback schema, supported channels, names, phone number pins, secrets, active flag. My reasoning: payload/callback schemas are the contract with each merchant's integration — bulk-changing them could suddenly start rejecting merchants' lead pushes; and routing/identity/secrets being bulk-editable is how one mistake becomes a 200-template outage. Question: is flow + configurations enough, or do you see a case where the payload schema should also propagate (e.g. all merchants of one use case share the exact same payload format)?

## 11. New dashboard tabs — is the visibility split right?

Five new screens were added to the dashboard. Current visibility:

- **Families** (list + create): under Tools in the sidebar, visible to all console users — but the list only shows families of the user's own reseller, and the Create button appears for admins only.
- **Family detail** (parent template, members, family version history): visible to users with access to that family's reseller; all edit actions admin-only.
- **Propagation flow** (preview → resolve conflicts → apply): opened from family detail; every action admin-only.
- **Rollouts** (bulk history + revert): lives in the sidebar's admin section — admin-only tab.
- **Template version history** (view old versions + restore): on each template, visible to whoever can access that template; restore is admin/reseller-owner only.

In short: viewing is open (but scoped to your own reseller), changing anything is admin-only. Same split as question 9 — should the two viewing tabs (Families, template versions) also become admin-only, or is this split fine?

## 12. Families exist under a reseller only — not under a merchant. Is that the right scope?

Currently a family always belongs to a reseller (`reseller_id` on the family), and its members are templates from that reseller's merchants. There is no merchant-level family — because the whole idea was grouping the same use case ACROSS merchants. A merchant-level user can't create or own a family. Is reseller-only scoping right, or do you see a case where a single merchant with many templates would need their own family (e.g. one big merchant with 20 templates of the same flow)? If yes, we'd add an optional merchant_id to the family.
