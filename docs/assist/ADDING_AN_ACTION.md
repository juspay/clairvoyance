# Adding an action to Buddy Assist

An **action** is something the assistant can do for a shopper beyond talking:
a tool the model calls, usually a card that shows the result, and sometimes
buttons on that card. WISMO (order tracking) is the worked example throughout;
[WISMO_V2.md](WISMO_V2.md) is how it is switched on for one template.

Paths are relative to `app/ai/voice/agents/breeze_buddy/` in clairvoyance and
to `packages/breeze-buddy-assist-widget/src/lib/ui/` (written `W/`) in loom,
unless they say otherwise.

## Decide the shape first

| Question | Usually | WISMO's answer |
|---|---|---|
| Where does the tool live? | A template `flow.functions` HTTP tool, so each template opts in and no deploy is needed to wire a merchant. Code only when the logic is ours (built-in handlers, `handlers/internal/builtin_dispatcher.py`). | Two HTTP tools: `get_order_status` (nautilus) and `read_page_content` (a page reader). |
| Does it need a card? | Yes if the result is structured and the shopper will act on it. No if one sentence answers it. | `OrderStatus`. |
| Which flavor owns the card? | An existing one (`commerce`) if the vocabulary fits. A new flavor only for a new vertical. | `commerce`. |
| Bound data or literal fields? | **Bind** whatever a tool returned, by reference, so the model cannot retype it. Use **literal fields** only for things no tool returns as data (text the model transcribes). | `order` is bound from `$tool:get_order_status#/orders/0`; `eta_display`, `latest_update`, `updates` are literal fields transcribed from the courier page. |
| Buttons? | Each button is an intent: client-side (navigate), direct (server runs a tool and shows the result), or an agent turn. | "Track your order" is the client-side `track_order` intent. |

## Backend (clairvoyance), in order

1. **The tool.** Add it to the template's `flow.functions`. HTTP tools are
   `GlobalHttpFunction` in `template/types.py`: `name`, `description`
   (required, and it is what the model reads), `properties`, `required`,
   `expected_fields` (`static` values from template vars, `llm` values from
   the call), `http_request`, and `expected_response_schema`.
   Placeholders in `http_request` resolve only from `expected_fields`, and
   every `llm` field must be present in the call.
2. **A role for it.** If the flavor's machinery needs to recognise the tool
   (step labels, forced render, annotations), add a role constant and its
   default tool name in `assist/commerce/ucp/roles.py`. Templates that rename
   a tool map it back with `configurations.ui_intents.tools`. The engine
   never hard-codes a tool name.
3. **The card's schema.** In a flavor module (WISMO:
   `assist/commerce/ucp/wismo.py`), subclass the catalog base with
   `data_bound = True`. Declare the props, any `literal_fields`, and a
   validator that derives presentation (headline, tone, display dates) on
   the server, so the widget only displays.
4. **Register it.** One `register_…` function in the flavor module, called
   from the flavor's schemas module at lazy import
   (`assist/commerce/ucp/schemas.py` calls `register_commerce_wismo`):
   - `register_primitives(group, {"OrderStatus": OrderStatus})`:
     `template/ui_catalog.py`
   - `register_result_annotator`: `chat/tools/result_annotators.py`
   - `register_step_labels` ("Checking your order" / "Found your order"):
     `chat/steps/labels.py`
   - `register_tool_annotations` (for example `read_only`):
     `chat/tools/annotations.py`
   - optionally `register_tool_verifier`: `chat/steps/verification.py`
5. **The render_ui pack.** A flavor's `RenderUiFlavorPack`
   (`chat/ui/render_ui_tool.py`; commerce's is in
   `assist/commerce/ucp/render_ui.py`) holds everything the model is told
   about binding and rendering. For a new card, add its sentence to
   `bind_component_coaching`. That coaching appears only when the component
   is offered, so a template that opts out leaves no trace. Also extend
   `summarize` (what the model hears back), `merge_repeat_render` (a second
   render in the same turn becomes a `replace`), and `default_force_after`
   if the tool's success should force a render. Literal fields need a
   `verify_literal_fields` hook. **Without one every literal field drops**
   (fail closed). The first enabled group with a pack wins.
6. **Intents** (if the card has buttons). Add an `IntentPolicy` in
   `assist/commerce/ucp/intents.py`. `CLIENT` is for navigation; the server
   entry exists only to return a typed error if one ever arrives. Update
   the shared fixture `tests/assist/fixtures/intent_payloads.json` and its
   checksum pin in `tests/assist/test_intent_payload_contract.py`.
7. **A new flavor only.** Add the group to `LAZY_GROUPS`
   (`template/ui_catalog.py`), its intent module to
   `FLAVOR_INTENT_MODULES` (`chat/intents/router.py`), and its roles via
   `register_flavor_roles` (`chat/flavors.py`). A process that serves only
   core templates must never import it: `tests/assist/test_lazy_loading.py`
   holds that line.

## Widget (loom), in order

8. **The component**, `W/<flavor>/X.svelte`. It displays what the server
   derived and decides nothing. Styles go in the flavor's `styles.ts`, never
   a `<style>` block, and every colour is a role token
   (`docs/WIDGET-TOKEN-CONTRACT.md`; `pnpm check` rejects colour literals).
9. **Narrowing.** Add a narrower in `W/<flavor>/helpers.ts` that drops the
   card when a required prop is missing and keeps URLs only if they are
   https (`narrowOrder` is the model).
10. **Registration.** Add `{component, mapProps}` in `W/<flavor>/index.ts`.
11. **The flavor map.** Add the type to `TYPE_TO_FLAVOR` in
    `W/flavor_manifest.ts`. Without it the widget shows an "Unknown
    primitive" chip instead of lazy-loading the chunk.
12. **Intent routing.** Add the button's intent to `W/<flavor>/policy.ts`
    (`navigateIntents`, `directSafeIntents`, detail overlay), and the
    payload builder to `helpers.ts`.
13. **The intent fixture.**
    `packages/client-sdk/src/lib/chat/__fixtures__/intent_payloads.json` must
    stay byte-identical to clairvoyance's copy, with its checksum pinned in
    `…/__tests__/intent-fixture-checksum.test.ts`.

## Turn it on for a template

Nothing is global. A template gets the action when:

- its catalog enables the flavor's **group**
  (`configurations.ui_catalog.enabled_groups`). A lazy flavor's component
  named alone in `enabled_primitives` is silently ignored. To opt one card
  out, use `disabled_primitives`.
- `configurations.render_ui.enabled` is true and the session is v2. Voice
  sessions strip data-bound cards.
- its `flow.functions` carries the tool(s), plus any `state_reducers` /
  `tool_arg_injection` that chain one tool's output into the next call's
  input (WISMO lifts `tracking_url` into state and injects it as the page
  reader's `url`, so the model cannot redirect it).
- its prompt says when to call the tool and how to render the result. Keep
  `{{ui_primitives_section}}` in place.
- secrets come from a reseller-wide or global credential, or from
  `template.secrets`. Merchant-scoped credentials are invisible to chat
  turns.

## Tests

Clairvoyance: add a `tests/test_<action>.py` in the shape of
`tests/test_wismo_order_status.py`, covering schema derivation, the literal
gate, annotations and step labels. Run it with the suites the pattern
touches:

```bash
uv run pytest tests/test_wismo_order_status.py tests/test_render_ui_tool_flavor.py \
  tests/test_flavor_scoping.py tests/test_ui_catalog_groups.py tests/test_step_labels.py \
  tests/assist/test_lazy_loading.py tests/assist/test_intent_payload_contract.py \
  tests/assist/test_commerce_intents.py
```

Loom, inside `packages/breeze-buddy-assist-widget`: a component test beside
`W/commerce/__tests__/order-status.test.ts`, then `pnpm check` and
`pnpm vitest run`. `W/__tests__/flavor_manifest.test.ts` pins parity with
the backend's component list and keeps flavor code out of the main bundle.
Build the client SDK first in a fresh checkout
(`pnpm --filter ./packages/client-sdk run build`), or the widget will not
resolve it.

Then drive it for real: wire a local template (backup, `PUT`, re-`GET`),
open the console preview or a dev page, and watch the SSE stream for
`function_call_completed` → `ui_decision` → `ui_op`. A green suite has never
been proof that a card renders.

## Improving an existing action

Most changes are one layer, and picking the right layer is most of the work:

| You want | Change | Where |
|---|---|---|
| Different wording, routing or tone | Prompt | The template's prompt section (for WISMO, the section in [WISMO_V2.md](WISMO_V2.md#5-the-prompt-section)). Roll a fleet-wide change through every template, not one. |
| A different tool timeout, header or endpoint | Template | `flow.functions[].http_request` |
| The model to always render after a step it sometimes skips | Pack or template | Add the role to `default_force_after` in the pack (every template), or to `render_ui.force_after` in one template. WISMO's page read is not forced today. |
| A new field on the card | Schema + widget | Add it to the schema (derived or literal) and the pack coaching, then the component and its narrower. Literal fields also need the gate to accept them. |
| Stricter truth for transcribed values | Gate | The flavor's `verify_literal_fields`. WISMO's only shapes and truncates today. |
| Data the upstream does not return (for example every shipment of a split order) | Upstream | Nautilus `api/wismo/order` maps only `fulfillments[0]`. The card follows the data. |
| An on/off switch per merchant | Planned | A per-platform features registry (a `features` block with a `PATCH /templates/{id}/features` endpoint and a console toggle) is planned, not built. Until it exists, "enabled" means "the template carries the tools". |

## Rules learned the hard way

- **Bind what tools return; never let the model retype it.** Literal fields
  are the exception, and each one needs a gate.
- **Coaching follows the component.** Text that tells the model to use a card
  must disappear when the template does not offer it, or the model keeps
  trying.
- **Fail closed on unverified values.** No gate means nothing renders.
- **The server derives, the widget displays.** Anything with a judgement in
  it (a headline, a tone, a status) belongs in the schema validator.
- **Flavors load by group, lazily.** Test that a core-only process never
  imports yours.
- **Cross-repo contracts are copies with pinned checksums.** Intent payloads
  and the widget appearance vocabulary each live in more than one repo. Change
  every copy in the same set of PRs, and let the pin fail when one is missed.
