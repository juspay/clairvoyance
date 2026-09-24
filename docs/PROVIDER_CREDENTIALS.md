# Provider credentials — the account a template runs on

*23–24 Sep 2026.* A template names its LLM, STT and TTS provider, model and
voice. It cannot name the **account**: every provider key is one value per
process (env), so running the same script on two ElevenLabs accounts, or
giving one merchant its own Azure deployment, means a second deployment.

The change lands in four phases, each deployable on its own:

| phase | what it brings | this document's section |
|---|---|---|
| 1 | a credential row may hold a **provider account** | *The words on a credential* |
| 2 | a template block may **name** an account (`credential_id`), checked at save | *On a template* |
| 3 | the **LLM** runs on the account | *What the engine does* |
| 4 | **STT and TTS** run on the account | *What the engine does* |

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
| `elevenlabs` | `api_key` — the host is the deployment's (phase 4) | |
| `google` (Cloud STT, Chirp TTS, Gemini TTS) | `credentials_json` | |

`credential_type` is `custom`. Scope is as for every credential: global (no
reseller, admin-only), reseller-wide, or one merchant. Vocabulary lives in
`accounts/types.py` (`SHAPES`), never in a CHECK. A value is exactly its
provider's fields — an unknown key is refused, so a host cannot hide under
another name — and an endpoint is `https://` or `wss://` only.

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

*Not in this phase.* A `credential_id` on `llm_configurations`,
`llm_configurations.realtime`, `stt_configuration`, `tts_configuration`,
each `tts_configuration_overrides.<provider>` entry and an observer's
`llm` block, checked at save. Until phase 2 lands the word is not parsed.

## What the engine does (phases 3 and 4)

*Not in this phase.* Every call still reads its keys from the environment.

## Rolling out phase 1

1. Deploy. `079_credentials_provider.sql` adds a nullable `provider` column
   and a partial index. No data moves; every template behaves as before.
2. Create the account rows with `provider` and the value fields above. They
   are inert until a template names them (phase 2) and the engine honours
   the name (phases 3–4).

## Rolling back

Old code ignores `provider` on a credential. Nothing to move.
