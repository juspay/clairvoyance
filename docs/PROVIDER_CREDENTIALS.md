# Provider credentials — the account a template runs on

*23–24 Sep 2026.* A template names its LLM, STT and TTS provider, model and
voice. Until now it could not name the **account**: every provider key was
one value per process (env), and the only per-template hook was
`llm_configurations.api_key_name`, a global dynamic-config key with no
tenant scope. To run the same script on two ElevenLabs accounts, or to give
one merchant its own Azure deployment, meant a second deployment.

Now a template block may say `credential_id`, naming a row in the
`credentials` table whose `provider` matches. One document is copied and
pointed at another account by changing one id. Nothing else moves.

## The words

**On a credential** (`POST /credentials`): `provider`, the service the row
holds an account for, and a `value` carrying what that service reads. The
API refuses an unknown provider (400) and a value missing that provider's
fields (422) at the write.

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
| `elevenlabs` | `api_key` — the host is the deployment's: `BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY` (true by default) puts every ElevenLabs account on the India-resident cluster, on the live path and the pre-synthesis path alike | |
| `google` (Cloud STT, Chirp TTS, Gemini TTS) | `credentials_json` | |

`credential_type` is `custom`. Scope is as for every credential: global (no
reseller, admin-only), reseller-wide, or one merchant. Vocabulary lives in
`app/ai/voice/agents/breeze_buddy/provider_credentials.py` (`SHAPES`), never
in a CHECK. `GET /credentials?provider=elevenlabs` lists the accounts a
picker may offer.

**On a template**: `credential_id` on `llm_configurations`,
`llm_configurations.realtime`, `stt_configuration`, `tts_configuration`,
each `tts_configuration_overrides.<provider>` entry, and an observer's
`llm` block. A block without the word behaves exactly as today.

```json
"tts_configuration": {"provider": "elevenlabs", "voice_id": "…",
                      "credential_id": "1c1d…"}
```

Three laws on the block, enforced wherever it is parsed (save, chat,
playground): `credential_id` is stored in the one canonical spelling; an
`endpoint` beside a `credential_id` is refused (the account's endpoint is
used); a `region` is a name (`asia-south1`), never a host.

## What the engine does

- **One resolver per call.** `Accounts` is built at each entry point from
  the **call's** tenant — the lead's for a voice call (kept across
  transfers, so a transfer target's template never resolves accounts
  against its own tenant), the template's for chat, the greeting job and
  IVR. Each block is resolved lazily, once: a chat turn reads at most the
  LLM row.
- **Typed accounts that carry their host.** A row resolves to a
  `KeyAccount(api_key, endpoint)`, `AzureAccount`, `VertexAccount`,
  `GcpAccount` or `BedrockAccount`; so does the environment when the block
  names no row. Every factory reads the key and the host from that object
  and never touches an environment variable itself — a key never travels
  apart from its host, and a row can never be right for one service and
  wrong for another. Where a service cannot use the account's host it
  refuses the row: an `openai` account with a gateway endpoint on an STT
  block, an ElevenLabs account on an STT block while the deployment's
  cluster is the India-resident one.
- **Fail closed.** A row serves only when it exists, is active, sits in the
  caller's tenant (its merchant, its reseller, or global), names the
  block's provider and has that provider's shape. Anything else raises
  `AccountRefused` and no service is built: a call on the wrong account, or
  on another tenant's account, is never placed.
- **The same check at save.** `POST /templates`, `PUT /templates/{id}` and
  a version rollback run `Accounts.problems` on the document and answer 422
  with every bad reference, named by block, instead of failing on the first
  call.
- **DragonTTS.** A voice with an account is synthesized by its nested
  provider directly (the proxy holds its own keys and would bill its own
  account): `resolve_voice_config` unwraps `"model": "elevenlabs:…"` once,
  after template, override and payload have been merged, and the row is
  checked against the provider that really synthesizes. A DragonTTS voice
  without an account is the proxy path, untouched. A DragonTTS block with
  an account must carry `model: "<provider>:<model>"`.
- **Observers** run on their own row when they name one, else on the
  conversation's account only when they are the same connection (same
  provider, no endpoint of their own), else on the environment's.
- **Provider rows are never template variables.** The `{name}` flattening
  and the pre-check context read only rows with no `provider` (in SQL), so
  an account's key is never readable through `{api_key}` in a prompt or an
  MCP header.
- **A credential write enforces its own rules.** Create and update validate
  the provider and the merged value's shape; an update that sends the key
  masked cannot change the row's `endpoint` (whoever sets the host must
  hold the key). Deleting a row, switching it off, or re-labelling its
  provider while a template names it is refused by the statement itself —
  an exact `jsonb_path_exists` match, atomic with the write — and answers
  409.
- **Gemini prompt cache.** A CachedContent belongs to one GCP project, so
  the chat prompt cache is keyed by the client's project and location as
  well — two Vertex accounts never share an entry.

## Rolling it out

1. Deploy. No migration data moves: `078_credentials_provider.sql` adds a
   nullable `provider` column and an index. Every template behaves as
   before until it names an account.
2. Create the account rows with `provider` and the value fields above.
3. Copy the template, set `credential_id` on the blocks that should run on
   the new account, save. A bad id is refused at save.

## Rolling back

Old code ignores `credential_id` on a template and `provider` on a
credential: templates fall back to env keys. Nothing to move.

## Not covered

- The inbound multi-template IVR menu (several templates behind one
  number) synthesizes its menu with env keys: there is no single template
  to take the account from.
- The widget's one-shot transcription (`stt/transcribe.py`) reads env keys.
- The block-redirect message played when a pre-check blocks a call:
  platform audio on the platform's keys (the template is not loaded yet).
- DragonTTS per-request keys (above).
