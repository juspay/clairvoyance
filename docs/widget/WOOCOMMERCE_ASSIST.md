# Buddy Assist on a WooCommerce store

Set up Buddy Assist for a WooCommerce merchant by creating one template and
one widget config. No code change is needed per merchant.

In the examples, replace `<host>` with the store host (e.g.
`www.example.com`) and `<store>` with a short store name (e.g. `example`).

## How it works

Shopify serves the six commerce tools itself, at `https://{shop}/api/ucp/mcp`.
WooCommerce has no such endpoint, so clairvoyance hosts one, at
`/mcp/woocommerce/<host>`, an MCP endpoint. The template's tool server names that URL, and
our engine answers it in process, with no request to our own API
(`mcp/in_process.py`). It reads the store's public Store API with GET
requests only (`assist/platforms/woocommerce/`).

```
Widget → clairvoyance → LLM
             ↓ tool call (search_catalog, get_product, create_cart, ...)
      tool server …/mcp/woocommerce/<host> → answered in process
             → GET https://<host>/wp-json/wc/store/v1/...
             ↓ same product and cart shapes as Shopify's tools
      product cards, cart card → widget
```

| | Shopify | WooCommerce |
|---|---|---|
| Tool server URL | `https://{shop_url}/api/ucp/mcp` | `https://api.breezebuddy.ai/mcp/woocommerce/<host>`, answered in process |
| Cart | Stored in Shopify | Stored in the cart id, e.g. `1128598:2,1315323:1` |
| Checkout button | `ui_intents.urls.checkout_page` plus the `cart` cookie | The cart's `continue_url`: `https://<host>/checkout-link/?products=<cart id>` |
| Order tracking (`flavor.ucp.features.order_tracking`) | Looked up through nautilus | Read from the store's REST API with the merchant's key (section 6) |

The engine, widget and voice are the same for both platforms.

## 1. Check the store

All three must pass.

```bash
# Store API is open. Expect: 200 application/json, and a JSON list of products
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" "https://<host>/wp-json/wc/store/v1/products?per_page=1"

# WooCommerce version. Expect: 10.0 or later (needed for checkout links)
curl -s "https://<host>/" | grep -o 'content="WooCommerce [0-9.]*"'

# Checkout links work. Expect: 302 to /cart/ with "checkout link was out of date"
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" "https://<host>/checkout-link/"
```

Reading the first check:

| Result | Meaning |
|---|---|
| `200 application/json` | WooCommerce with an open Store API. Continue. |
| `200 text/html` | Not WooCommerce. The site answers every path with its own page (e.g. a single-page app on another platform). The endpoint cannot serve it. |
| `403` | WooCommerce, but a firewall or security plugin blocks the Store API. The merchant must allow public GET requests to `/wp-json/wc/store/v1/`. |
| `404` | Wrong host, or not WooCommerce. Try the other form (`www.` or bare domain). |

Use for `<host>` the exact host that returned `200 application/json`.

## 2. Create the template

Copy the default Assist blueprint
(`tests/assist/fixtures/buddy-assist-default.v2.json`, or the default Assist
template in the database) and apply the changes below.

### 2.1 Top-level fields

| Field | Value |
|---|---|
| `reseller_id` | The merchant's reseller, e.g. `BB_ASSIST` |
| `merchant_id` | The merchant's id in the `merchants` table |
| `name` | `<store>-assist` |
| `secrets` | `{"shop_url": "<host>"}` |
| `expected_payload_schema` | `{"shop_url": {"type": "string", "example": "<host>", "description": "Storefront domain used by the assistant's commerce tools."}}` |

### 2.2 System prompt: `flow.system_prompt`

| Find | Change to |
|---|---|
| `{{brand_identity_section}}` | The brand block below |
| `{{#shopify_operating_section}}` … `{{/shopify_operating_section}}` | Delete the whole block, markers included |
| `https://{{shop_domain}}/pages/contact` | The store's real contact page URL |
| Any other `{{shop_domain}}` | `<host>` |
| `{{ui_primitives_section}}` | Keep as is. Without it, no cards render. |

Brand block (the format onboarding writes):

```
## Brand identity

- **Assistant name:** <Store name> Assist
- **Brand:** <Store name>
- **Storefront:** `{shop_url}`

### Verified website context

5-10 lines: what the store sells, main categories, brands, currency.
```

### 2.3 Tool server: `configurations.mcp.servers[0]`

| Field | Change |
|---|---|
| `name` | `woocommerce-store` |
| `url` | `https://api.breezebuddy.ai/mcp/woocommerce/<host>`. Write `<host>` literally, not `{shop_url}`: the widget can set `shop_url`, and a placeholder there is never answered in process. The engine answers this URL in process; `api.breezebuddy.ai` is the address the endpoint has when it is opened to other clients. On beta, use `https://api.beta.breezebuddy.ai`. |
| `auth` | `{"type": "none"}`, as for Shopify |
| `default_args` | `{}`. The blueprint's `meta.ucp-agent.profile` is for Shopify; our endpoint does not use it. |
| `tool_response_transforms` | In `create_cart`, `get_cart` and `update_cart`, delete the `derive_field` rule (it builds the Shopify `cart_token`). Keep every `scale_by_exponent` rule. |
| `tool_ui_instructions` | In `create_cart`, `get_cart` and `update_cart`, delete `,{prop:'cart_token',ref:'$tool:<tool>#/cart_token'}` and `, and cart-cookie sync itself`. |
| `tool_schemas` | Replace the Shopify GID descriptions with `Product id`, `Variant id` and `Cart id`. WooCommerce ids are numbers. |
| `tool_schemas` → `search_catalog` → `description` | Append the search hint below. |

Search hint:

```
 This store's search matches words in product TITLES only: send 1-3 product
 words as they appear in a title (e.g. 'wireless mouse', 'neckband'), never a
 sentence; put budgets in filters.price (minor units, e.g. 100000 = Rs 1,000).
 If a search returns nothing, retry once with fewer or more common words.
```

### 2.4 Other configuration

| Field | Change | Why |
|---|---|---|
| `configurations.flavor` | `{"ucp": {"connectors": ["woocommerce"], "features": {}}}` | Required. It stops Shopify-only data fixes from running on this store's data. |
| `configurations.ui_intents.urls.checkout_page` | Delete the key | A configured page wins over the checkout link, and would open an empty cart |

### 2.5 Final check

The edited JSON must not contain `shopify`, `cart_token`, `{shop_url}` inside
`configurations`, or any `{{…}}` except `{{ui_primitives_section}}`.

## 3. Save the template and widget config

Both calls need an admin or reseller-owner login.

1. `POST /agent/voice/breeze-buddy/templates` with the edited JSON
   (`reseller_id`, `name`, `merchant_id`, `is_active`, `flow`,
   `expected_payload_schema`, `expected_callback_response_schema`,
   `configurations`, `secrets`, `supported_channels`). Note the template id.
2. `POST /agent/voice/breeze-buddy/widget-config` with `reseller_id`,
   `merchant_id`, `template_id` and `allowed_origins` (every origin the store
   uses, e.g. `["https://<host>"]`). The response has the `public_widget_key`.

## 4. Install on the store

WooCommerce has no app embed. The merchant pastes this into the site footer
(a header/footer plugin or the theme):

```html
<breeze-buddy-assist tenant="<public_widget_key>" shop="<host>"
  api-base="https://clairvoyance.breezelabs.app"></breeze-buddy-assist>
<script src="https://breezebuddy.ai/widget/assist.js" async></script>
```

## 5. Test

Open the store (or any page with the embed, served from an origin in
`allowed_origins`).

| Test | Expected |
|---|---|
| Search with a budget, e.g. "wireless mouse under 1000" | Cards with correct prices (₹699, not ₹6.99) |
| Add to cart on a simple product | Adds directly |
| Add to cart on a variable product | A picker with the real variations; sold-out ones are disabled |
| "+" or remove on the cart card | Quantity and total update |
| Add by chat, e.g. "add the blue one" | The assistant adds it and shows the cart |
| Review and checkout | Opens `https://<host>/checkout-link/?products=…`; the store checkout shows the same items and total |

## 6. Order tracking

This step is optional. The assistant can answer "Where is my order?" for a
WooCommerce store. It reads one order from the store's REST API, checks the
phone or email on it, and shows the order card with tracking.

### 6.1 Get a REST API key from the merchant

The merchant creates it in **WooCommerce → Settings → Advanced → REST API →
Add key**, with **Read** permission. They send the consumer key, which starts
with `ck_`, and the consumer secret, which starts with `cs_`.

### 6.2 Store the key

Create one provider-account row for the merchant with
`POST /agent/voice/breeze-buddy/credentials`:

```json
{
  "reseller_id": "<template reseller_id>",
  "merchant_id": "<template merchant_id>",
  "name": "woocommerce-<host>",
  "credential_type": "custom",
  "provider": "woocommerce",
  "value": {
    "consumer_key": "ck_...",
    "consumer_secret": "cs_...",
    "endpoint": "https://<host>"
  }
}
```

| Rule | Why |
|---|---|
| `merchant_id` is the template's own | The lookup only reads this merchant's row. A reseller-wide row is not used. |
| `endpoint` host is the `<host>` in the template's tool server URL (section 2.3), or `secrets.shop_url` for a template with no tool server | The lookup refuses a key for another store |
| Exactly one such row | Two rows make the lookup refuse, rather than guess |
| `name` names the store, e.g. `woocommerce-www.shopyvision.com` | Credential names must be unique within their scope; the lookup finds the row by `provider`, not by name |

The key is stored encrypted and never reaches the model, a prompt or the
session.

### 6.3 Turn it on

Set `configurations.flavor.ucp.features.order_tracking: true`. The template
needs nothing else: the order tools come from the flag, and the store is the
`<host>` in the tool server URL (section 2.3), the store the catalog tools read.
A template with no tool server (order tracking without catalog tools) needs
`secrets.shop_url` set to the store host instead.

### 6.4 Test

| Test | Expected |
|---|---|
| "Where is my order?", then a real order number and its phone or email | The order card with status, items and tracking |
| The same order with a wrong phone | "I couldn't match that order…" |
| An order number that does not exist | The same message. The assistant never says whether an order exists. |

## Public endpoint (off by default)

The same URL can be served over HTTP to other clients, with one JSON-RPC
`tools/call` POST per tool call. The route is off unless
`MCP_PUBLIC_ENDPOINT_ENABLED=true`, because it has no caller auth yet.
Do not turn it on before auth is added to `app/api/routers/mcp.py`.

When it is on, it has no caller check: any store host, no merchant check,
capped at 600 calls a minute per store. It serves `tools/call` only (no
`initialize` or `tools/list`; the tool schemas are in the template).

```bash
curl -s https://api.breezebuddy.ai/mcp/woocommerce/<host> \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"search_catalog","arguments":{"catalog":{"query":"mouse","pagination":{"limit":2}}}}}'
```

| Answer | Meaning |
|---|---|
| `result.content[0].text` | The tool data, in the same product and cart shapes as Shopify's tools, as JSON text |
| `error` with code `-32000` | The tool failed; `message` says why (store HTTP error, timeout, bad arguments) |
| HTTP 404 | The route is off, the platform is not one we host, or `<host>` is not a host name |
| HTTP 429 | Over 600 calls a minute for this store |

## Limits

- Order tracking finds an order by its ID, which is the order number on a
  default WooCommerce store. A store whose order numbers differ from its IDs
  (an order-numbering plugin) is not supported: WooCommerce's order search
  scans every order and is too slow for a chat.
- Order tracking: the tracking number and link come from the Shipment
  Tracking or Advanced Shipment Tracking plugin. A store without either shows
  the order with no tracking. The "View order" link opens the store's account
  page, so a guest shopper must log in to use it.
- Store search matches title words only; natural sentences can return nothing.
- The endpoint reads `https://<host>/wp-json/wc/store/v1`. A store installed
  under a subdirectory is not supported yet.
- Each Store API call takes about 1 second. A search with variable products
  also reads their variations: one call per page of variations, at most 10
  pages, 4 at a time.
- The store's cart icon shows assistant items only after checkout opens.
- The checkout link replaces the shopper's existing store cart.
- Tax, shipping and final stock are checked on the store's checkout page.
- An item that goes out of stock stays in the assistant cart, with a warning, until the shopper removes it; the store's checkout refuses it until it is back in stock. A product the store no longer lists is dropped with a warning.
- The assistant cart uses catalog prices. A store with cart-level price rules
  (e.g. a dynamic pricing plugin) can show a different total at checkout.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Session call returns 403 | Page origin not in `allowed_origins` | Add the exact origin |
| "Upstream HTTP 404" on every tool | The URL does not match `/mcp/woocommerce/<host>` (for example it holds `{shop_url}`), so the engine sends it over HTTP to the closed public route | Write the URL exactly as in section 2.3 |
| "The store has no WooCommerce Store API at this address" | The host is not WooCommerce, or its Store API is off | Run the checks in section 1 |
| "The store answered HTTP 403" | The store blocks our server | Merchant allows `/wp-json/wc/store/v1/` |
| Prices 100 times too small | A `scale_by_exponent` rule was changed | Keep the blueprint's rules |
| Checkout opens an empty cart | `checkout_page` is still set | Delete `ui_intents.urls.checkout_page` |
| Cart card does not render after a chat add | A `cart_token` bind is left in `tool_ui_instructions` | Delete it |
| "Where is my order?" always says the tracking system is unreachable, and the log says `store answered 401` | The REST key is wrong or has no Read permission, or the web server drops the `Authorization` header before WordPress sees it | Check the key in WooCommerce. If the key is right, ask the merchant's host to pass the `Authorization` header to PHP. |
| The log says `0 woocommerce accounts` or `account is not for <host>` | No credential row for this merchant, or its `endpoint` names another host | Create one row as in 6.2, with the host from the tool server URL or `secrets.shop_url` |
| The log says `no store for this template` | The template has no enabled `/mcp/woocommerce/<host>` tool server and no `secrets.shop_url`, or has two tool servers for different stores | Write the URL exactly as in section 2.3, or set `secrets.shop_url` |
