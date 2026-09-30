# Provider credentials — the account a template runs on

*23–24 Sep 2026.* A template names its LLM, STT and TTS provider, model and
voice. It cannot name the **account**: every provider key is one value per
process (env), so running the same script on two ElevenLabs accounts, or
giving one merchant its own Azure deployment, means a second deployment.

The change lands in five phases, each deployable on its own:

| phase | what it brings | this document's section |
|---|---|---|
| 1 | a credential row may hold a **provider account** | *The words on a credential* |
| 2 | a template block may **name** an account (`credential_id`), checked at save | *On a template* |
| 3 | the **LLM** runs on the account | *What the engine does* |
| 4 | **STT and TTS** run on the account | *What the engine does* |
| 5 | **telephony** (Plivo) runs on the account | *Telephony (phase 5)* |

Code lives in `app/ai/voice/agents/breeze_buddy/accounts/` — the package is
the door, other modules import from it and never from the files inside.

## The words on a credential (phase 1)

`POST /credentials` takes `provider`, the service the row holds an account
for, and a `value` carrying what that service reads. The API refuses an
unknown provider (400) and a value missing that provider's fields (422) at
the write. A row with no `provider` is a placeholder credential, exactly as
before.

| provider | value fields (required) | optional |
|---|---|---|
| `azure_openai` | `api_key`, `endpoint` | |
| `openai` | `api_key` | `endpoint` (an OpenAI-compatible gateway) |
| `google_vertex` | `credentials_json`, `project_id` | |
| `aws_bedrock` | `api_key` (a Bedrock API key, the bearer token) — omit for the pod's AWS credential chain | |
| `openai_realtime`, `xai_realtime` | `api_key` | |
| `azure_openai_realtime` | `api_key`, `endpoint` | |
| `gemini` (realtime) | `api_key` | |
| `deepgram`, `soniox`, `sarvam`, `assemblyai`, `cartesia` | `api_key` | |
| `smallest` (STT) | `api_key` — one fixed public host; a row naming an `endpoint` is refused | |
| `elevenlabs` | `api_key` — the host is the deployment's per-service one (`ELEVENLABS_TTS_URL` for a voice, `ELEVENLABS_STT_URL` for Scribe) | |
| `google` (Cloud STT, Chirp TTS, Gemini TTS) | `credentials_json` | |
| `plivo` (telephony) | `auth_id` (20 characters, `MA…` or `SA…`), `auth_token` | |

`credential_type` is `custom`. Scope is as for every credential: global (no
reseller, admin-only), reseller-wide, or one merchant. Vocabulary lives in
`accounts/types.py` (`SHAPES`), never in a CHECK. A value is exactly its
provider's fields — an unknown key is refused, so a host cannot hide under
another name — and an endpoint is `https://` or `wss://` only. An
`elevenlabs` row is a key only: an `endpoint` on it is refused, since the
deployment decides the host. These are laws on ROWS (shared secrets); a
template's own `endpoint` with its `api_key_name` is the author's contract
and is taken as written, as today.

**Two listings.** `GET /credentials?provider=elevenlabs` is the account
picker: the rows a template's `credential_id` may name for that service,
in the caller's scope. A tenant's plain `GET /credentials?reseller_id=R`
keeps listing placeholder credentials only (`provider IS NULL`, in SQL):
it is the same read that feeds template variables and the UAP, and an
account's key must never surface there. The admin's unscoped list shows
every row. Provider accounts are managed through the picker.

What phase 1 guarantees, on its own:

- **Provider rows are never template variables.** The `{name}` flattening
  and the pre-check context read only rows with no `provider` (in SQL), so
  an account's key is never readable through `{api_key}` in a prompt or an
  MCP header. The pre-check read is tenant-checked too: a hook may name
  only a row its template's reseller or merchant may use (global rows are
  everyone's). **This is a behaviour change for a pre-check that names a
  row outside its template's tenant**: it used to receive that row's
  values and now receives none, with a warning in the log. Before
  deploying, list such references on the target environment and re-point
  them:

  ```sql
  select t.id, t.name, t.reseller_id, t.merchant_id,
         pc->>'credential_id' as credential_id,
         c.reseller_id as cred_reseller, c.merchant_id as cred_merchant
  from template t,
       jsonb_array_elements(
         coalesce(t.configurations->'call_execution_config'->'pre_checks','[]')) pc
  join credentials c on c.id::text = pc->>'credential_id'
  where c.reseller_id is not null
    and (c.reseller_id <> t.reseller_id
         or (c.merchant_id is not null
             and c.merchant_id is distinct from t.merchant_id));
  ```
- **A credential write enforces its own rules.** Create and update validate
  the provider and the merged value's shape; an update that sends the key
  masked cannot change the row's `endpoint` (whoever sets the host must
  hold the key). Deleting a row, switching it off, or re-labelling its
  provider while a template names it (by `credential_id`, phase 2) is
  refused by the statement itself — an exact `jsonb_path_exists` match,
  atomic with the write — and answers 409.

## On a template (phase 2)

`credential_id` on `llm_configurations`, `llm_configurations.realtime`,
`stt_configuration`, `tts_configuration`, each
`tts_configuration_overrides.<provider>` entry, and an observer's `llm`
block. A block without the word behaves exactly as today.

```json
"tts_configuration": {"provider": "elevenlabs", "voice_id": "…",
                      "credential_id": "1c1d…"}
```

Three laws on the block, enforced wherever it is parsed (save, chat,
playground): `credential_id` is stored in the one canonical spelling; an
`endpoint` beside a `credential_id` is refused (the account's endpoint is
used); a `region` is a name (`asia-south1`), never a host.
A text-LLM block may instead name a deployment account, `account` (see *Named deployment accounts* below).
An `account` beside a `credential_id` or an `endpoint` is refused as well.

**The same check at save.** `POST /templates`, `PUT /templates/{id}`, a
version rollback and the Assist onboarding save run `Accounts.problems` on the document and answer 422
with every bad reference, named by block: the row must exist, be active,
sit in the template's tenant (its merchant, its reseller, or global), name
the block's provider and carry that provider's shape; a DragonTTS block
with an account must carry `model: "<provider>:<model>"`.

**The resolver** (`accounts/resolve.py`): `Accounts(reseller, merchant)`
resolves a block to a typed account that carries its host — the row's
when the block names one, else the environment's (`env_account`, the only
place an env key is read for a call). It fails closed with
`AccountRefused`. Where a service cannot use the account's host it refuses
the row: an `openai` account with a gateway endpoint on an STT block. An
ElevenLabs row brings only its key and runs on the deployment's host for
that service (`ELEVENLABS_TTS_URL` for a voice, `ELEVENLABS_STT_URL` for
Scribe), as the environment's own key does.

Every block's name is honoured by the engine (phases 3 and 4).

## What the engine does (phase 3: the LLM; phase 4: speech)

- **One resolver per call.** `Accounts` is built at each entry point from
  the **call's** tenant — the lead's for a voice call (kept across
  transfers, so a transfer target's template never resolves accounts
  against its own tenant), the template's for chat, the greeting job and
  the upsell follow-up. Each block is resolved lazily, once: a chat turn
  reads at most the LLM row.
- **Typed accounts that carry their host.** A row resolves to a
  `KeyAccount(api_key, endpoint)`, `AzureAccount`, `VertexAccount`,
  `GcpAccount` or `BedrockAccount`; so does the environment when the block
  names no row. Every factory reads the key and the host from that object
  and never touches an environment variable itself — a key never travels
  apart from its host.
- **Fail closed.** `AccountRefused` and no service is built: a call on the
  wrong account, or on another tenant's account, is never placed.
- **Phase 3 — the LLM.** `get_llm_service` (Azure, OpenAI and gateways,
  Gemini and Claude on Vertex, Bedrock: the row's key is the bearer token,
  region and model stay on the block), the realtime factory (OpenAI, xAI,
  Azure, Gemini Live), the Gemini opening line, chat turns and the upsell
  follow-up (the template's tenant). Hold-transfer summaries are a
  platform job with no template in hand: they run on the environment's
  Azure account, as before. `api_key_name` (a named dynamic-config
  key) is honoured inside `env_account`, superseded by `credential_id`.
- **Observers** run on their own row when they name one, else on the
  conversation's account only when they are the same connection (same
  provider, no endpoint of their own), else on the environment's.
  A named `account` follows the same rule as a row.
- **Named deployment accounts.** A text-LLM block on the `openai` provider may name `account` instead of a row.
  The block holds only the name; the host and the key are the deployment's own, the key read from the pod environment only (`static.py`), never from dynamic config.
  `grid-topics` is the Grid gateway (`LITELLM_BASE_URL`) on `GRID_TOPICS_API_KEY`, for topic evaluations only: a template or observer naming it is refused at save and at call time.
  The names live in `accounts/llm.py` (`named_account`); adding one is one branch there, one pod variable and its key in `NAMED_ACCOUNT_KEYS`.
  An unknown name, another provider, or a missing key is `AccountRefused`, and `Accounts.problems` checks a named account at save as it checks a row.
  Each account's key serves only that account: an `api_key_name` naming one (`NAMED_ACCOUNT_KEYS`) is refused at save and at call time.
- **Topic evaluations** build their model through `get_llm_service` like every other caller.
  The topic configuration (`evaluation_config.configuration`) takes `account` and `extra_body`; on the `openai` provider a configuration that names no account runs on `grid-topics`, where every topic evaluation ran before.
  `PATCH /templates/{id}/topics/configuration` resolves the account it is given and answers 400 when this deployment cannot serve it.
- **Gemini prompt cache.** A CachedContent belongs to one GCP project, so
  the chat prompt cache is keyed by the client's project and location as
  well — two Vertex accounts never share an entry.
- **Phase 4 — speech.** STT (deepgram, soniox, sarvam, assemblyai, openai,
  elevenlabs, google) and TTS (elevenlabs, cartesia, sarvam, soniox, gemini,
  google) on the live path, the greeting and IVR pre-synthesis, and the
  batch TTS helpers. Where a service cannot use the account's host it
  refuses the row: an `openai` account with a gateway endpoint on an STT
  block.
- **ElevenLabs host.** One (url, key) pair per service in the environment,
  no flag: `ELEVENLABS_TTS_URL` for a voice, `ELEVENLABS_STT_URL` for
  Scribe, both bare hosts. A row brings only its key and runs on that
  host, on the live path (wss) and the pre-synthesis path (https) alike.
- **DragonTTS.** A voice with an account is synthesized by its nested
  provider directly (the proxy holds its own keys and would bill its own
  account): `resolve_voice_config` unwraps `"model": "elevenlabs:…"` once,
  after template, override and payload have been merged, and the row is
  checked against the provider that really synthesizes. A DragonTTS voice
  without an account is the proxy path, untouched.
- **IVR.** The walker builds `Accounts` from the lead's tenant; a row that
  stopped serving since the template was saved ends the call as any IVR
  error does — with an outcome and a closed socket.

## Telephony (phase 5)

*29 Sep 2026.* Every Plivo REST call was signed with the environment's one
account (`PLIVO_AUTH_ID`). Plivo lets only the account that owns a number
transfer or hang up a call on it, so a number bought in another org — a US
number for Barclays, in `juspay-us` — reached us but could not be
transferred.

A template names its Plivo account exactly as it names its STT account:

```json
"telephony_configuration": {"provider": "plivo", "credential_id": "9c5f…"}
```

- **At save**, the block is one more account block: `Accounts.problems`
  judges it (exists, active, a `plivo` row, in the template's tenant,
  complete) and a bad reference is a 422. A row a template names cannot be
  deleted, switched off or re-labelled (409) — the existing guard finds
  `credential_id` anywhere in the configurations.
- **At a call**, the call's `Accounts` (the lead's tenant) resolves the
  block where it resolves the STT and TTS blocks, once per call; the
  call's Plivo client switches to that account, and the transfer
  (`connect_to_live_agent`), pipecat's auto hang-up and a hold-and-consult
  dial run on it (so a hold number must live in that account too). A row
  that may not serve ends the call, exactly as a refused STT or TTS row
  does — nothing runs on the environment's keys in its place.
- **Outbound.** The dispatcher asks the provider to use the template's
  account (`use_template_credentials`, the hook the live call uses) before
  it takes any capacity, and dials with it (so the template's number must
  live in that account). A refused row ends the lead `NUMBER_UNAVAILABLE`.
- **After the call** (MPC callback, recording download), the account is
  the one the lead's template names. Recording keys go only to
  `https://media.plivo.com` (or a regional host like `aps1.`).
- A template without the block is resolved to the environment's account,
  as before.

## Rolling it out

1. Deploy. `079_credentials_provider.sql` adds a nullable `provider` column
   and a partial index. No data moves; every template behaves as before.
2. Create the account rows with `provider` and the value fields above.
3. Copy the template, set `credential_id` on the blocks that should run on
   the new account, save. A bad id is refused at save.
4. Telephony (phase 5): create a `plivo` row with the org's `auth_id` /
   `auth_token`, then set `telephony_configuration.credential_id` on the
   template. No migration.

## Rolling back

Old code ignores `credential_id` on a template and `provider` on a
credential. Nothing to move.

## Not covered

- The inbound multi-template IVR menu (several templates behind one
  number) synthesizes its menu with env keys: there is no single template
  to take the account from.
- The widget's one-shot transcription (`stt/transcribe.py`) reads env keys.
- The block-redirect message played when a pre-check blocks a call:
  platform audio on the platform's keys (the template is not loaded yet).
- DragonTTS per-request keys (above).
- Telephony: anything before the template is loaded (the multi-template
  IVR menu and its redirect) runs on the environment's account. Plivo
  number search and buy stay on the environment's account. Twilio and
  Exotel have no telephony block yet.
- Telephony: the paths after the call read the lead's template as it is
  then — editing its `telephony_configuration` mid-call, or an
  agent-to-agent transfer to a template on another account, can leave
  them on the wrong account.
