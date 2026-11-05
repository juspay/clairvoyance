# TTS Voice Catalog

## What it is

The TTS voice catalog is a curated list of "safe to offer in the dashboard" TTS
voices, separate from the raw provider/voice/model combinations a merchant
template can already reference directly. It is **static, versioned data** —
`app/ai/voice/tts/catalog.json`, parsed and validated once per process by
`app/ai/voice/tts/catalog.py` — not a database table. Voices are curated,
change rarely, and belong under code review: adding or editing one is a normal
PR with rollback for free.

One entry per `(provider, voice_id)`:

```json
{
  "provider": "elevenlabs",
  "voice_id": "fG9s0SXJb213f4UxVHyG",
  "display_name": "Rachel",
  "models": ["eleven_flash_v2_5"],
  "languages": ["en-IN", "hi"],
  "tags": ["female", "default"]
}
```

Optional fields: `enabled` (default `true`), `residency`, `style_params`.
Entries are validated against `CatalogVoiceEntry`
(`app/schemas/breeze_buddy/tts_catalog.py`); unknown providers and duplicate
`(provider, voice_id)` pairs fail loading (and the test suite —
`tests/test_tts_catalog_static.py`). A catalog that fails to parse or
validate **fails app startup** (the lifespan primes it without a fallback):
it's checked-in code, so a broken file is a bad build, not a runtime
condition to limp through.

The only **mutable** state in the system is the *preview manifest* —
`tts-previews/manifest.json`, stored in the same backend as the preview WAVs
(`app/services/preview_storage.py`). It maps `"provider/voice_id"` to that
voice's generated preview entries (`{language, url, content_key, format}`, or
`{language, error, content_key}` for a failed attempt). The reconcile endpoint
is its only writer.

The catalog covers the 6 **voice-owning** providers: `elevenlabs`, `cartesia`,
`sarvam`, `gemini`, `google`, `soniox`. `dragontts` is a caching proxy: the app
forwards `model: "<provider>:<model>"` as-is (`app/ai/voice/tts/dragontts.py`);
the external DragonTTS service parses the compound id server-side. It owns no
voices, so it's deliberately excluded from the catalog and from
`ProviderFilter` on the listing endpoint (`provider=dragontts` is a `422`, not
an empty result).

Two endpoints, both mounted under the Breeze Buddy router
(`app/api/routers/breeze_buddy/tts_catalog/__init__.py`, included with prefix
`/agent/voice/breeze-buddy` in `app/main.py`):

- **`GET /agent/voice/breeze-buddy/tts/voices`** — list enabled catalog voices,
  optionally filtered by `provider` (exact match, one of the 6) and/or
  `language` (bare/regional match, see below). Requires any authenticated RBAC
  user (`get_current_user_with_rbac`), not admin-only. Response is ETag-cached
  client-side for 5 minutes (`Cache-Control: private, max-age=300`); a matching
  `If-None-Match` gets a `304` with no body. The ETag hashes the filter params
  plus the full response content, which folds in both the resolved per-provider
  defaults (`is_default`) and the manifest state (`previews`) — so a Redis
  default flip or a reconcile run yields new ETags. Each returned voice carries
  an `is_default` flag (does this voice match the provider's current live
  default per `BB_VOICE_PROVIDER_DEFAULTS`, fetched concurrently for all 6
  providers; `gemini` has no `BB_SPEECH_PROVIDER_DEFAULTS` entry, so an empty
  lookup falls back to `GEMINI_FALLBACK_DEFAULT = "Kore"` in `handlers.py`) and
  a `previews[]` array of `{language, url}` pairs — only previews that
  generated successfully are exposed; failed/pending ones are omitted from the
  response (still visible internally in the manifest).
- **`POST /agent/voice/breeze-buddy/tts/voices/reconcile`** — admin-only
  (`require_admin_access`, 403 for non-admin). Walks every enabled catalog
  voice and regenerates any preview whose manifest `content_key` doesn't match
  the hash of the voice's *resolved* preview configuration, or whose preview is
  simply missing. See "Ops notes" for throttling and failure handling.

Previews themselves are short, browser-playable WAV clips (16-bit mono,
native/requested PCM rate wrapped as WAV exactly once — providers that return a
WAV container, like Sarvam, are unwrapped first) of a canonical per-language
sentence, synthesized by `app/ai/voice/tts/preview.py` and persisted by
`app/services/preview_storage.py`. This path is deliberately separate from the
runtime telephony TTS dispatch (mu-law 8 kHz) — it never touches the live call
pipeline.

## Add a voice

1. **Add an entry to `app/ai/voice/tts/catalog.json`** (see the shape above).
   `models`/`languages`/`tags` can be empty lists where they don't apply:
   gemini voices carry `models: []` because the model comes from
   `GEMINI_TTS_MODEL` at synth time; google Chirp3-HD voices encode model +
   locale into the voice name itself (e.g. `en-IN-Chirp3-HD-Despina`).

   Verify the id against the real provider surface before adding it — the
   current sarvam speakers were checked against the installed pipecat SDK's
   `SarvamTTSSpeakerV3` enum, and the gemini names against
   `GeminiTTSService.AVAILABLE_VOICES` (`pipecat.services.google.tts`).

2. **Run the tests** (`uv run pytest tests/test_tts_catalog_static.py`) — they
   enforce provider validity, `(provider, voice_id)` uniqueness, and that every
   hardcoded provider default (`BB_SPEECH_PROVIDER_DEFAULTS`) stays covered.

3. **Ship it** (normal PR + deploy). The voice is immediately visible to
   `GET /tts/voices` — with `previews: []` until reconciled.

4. **Trigger preview generation** against the deployed environment:

   ```bash
   curl -X POST https://<host>/agent/voice/breeze-buddy/tts/voices/reconcile \
     -H "Authorization: Bearer <admin JWT>"
   ```

   Mint an admin JWT via the login endpoint (`POST
   /agent/voice/breeze-buddy/login`) or, for a scripted credential, `POST
   /agent/voice/breeze-buddy/auth/s2s/token` (admin-only, up to 365 days).

5. **Verify:**

   ```bash
   curl "https://<host>/agent/voice/breeze-buddy/tts/voices?provider=elevenlabs" \
     -H "Authorization: Bearer <JWT>"
   ```

   Confirm the new voice appears with a non-empty `previews[]` array for each
   of its configured languages (or a single `en` entry if `languages` is
   empty).

To **remove** a voice from the picker without deleting its entry (and its
curation history), set `"enabled": false` and ship.

There is deliberately **no no-deploy prod path** anymore: the old
insert-a-row-live escape hatch traded review for speed and is exactly what the
static design removes. An urgent voice addition is a one-line JSON PR.

## Preview storage config

Previews are stored in **GCS only** — there is no local-filesystem backend.
Two env vars govern it (`app/core/config/static.py`, read once at import — not
a live Redis toggle):

| Var | Default | Purpose |
|---|---|---|
| `TTS_PREVIEW_GCS_BUCKET` | `""` | GCS bucket for previews. Unset = previews disabled: the catalog still serves voices (without preview URLs) and any attempt to *write* a preview raises naming this variable. Deliberately **not** validated at import, so deployments that never use the voice catalog still boot. |
| `TTS_PREVIEW_PUBLIC_BASE_URL` | `""` | Optional. Overrides the host previews are served from, for deployments fronting the bucket with a CDN or custom domain. When unset, URLs default to the bucket's own public URL (`https://storage.googleapis.com/<bucket>/...`). |

`TTS_PREVIEW_GCS_BUCKET` is deliberately a separate bucket from the general
`GCS_BUCKET` (default `"atoms-sdk"`, used for call recordings) —
`preview_storage.py` calls `get_gcs_bucket()` directly with its own bucket name
rather than reusing `GCSStorage`/`upload_file_to_gcs`, since those are
hardcoded to `GCS_BUCKET`.

Objects are stored at `{provider}/{voice_id}/{language}-{key}.wav`, plus
`manifest.json`, all under the `tts-previews/` prefix. Previews are served
straight from the bucket, so the app exposes no `/tts-previews` route.

**Changing `TTS_PREVIEW_PUBLIC_BASE_URL` requires regenerating previews.** The
served URL is computed once, at generation time, and written into the manifest
— it isn't derived on read. To force full regeneration: delete the
`tts-previews/manifest.json` blob, then re-run the reconcile endpoint.

**GCS bucket expectation:** the upload code never calls `make_public()` or
generates a signed URL — it uploads and trusts the constructed URL to be
reachable. The bucket (or whatever fronts `TTS_PREVIEW_PUBLIC_BASE_URL`, e.g. a
CDN) must already allow public/anonymous read on the `tts-previews/` prefix
before use in prod, or every preview URL will 403 for end users. This is an
operational precondition the app doesn't check. The manifest lives in that same
public location by design — it contains only public preview URLs and
content-key hashes.

All four path components (`provider`, `voice_id`, `language`, `key`) are
validated against `[A-Za-z0-9._-]+` and rejected if they contain `..`, so
neither backend's path can be escaped by a crafted value.

**Content-key naming, and why regenerated audio can never be served stale:**
`resolve_preview_config()` in `app/ai/voice/tts/preview.py` first resolves the
*effective* synthesis configuration — catalog model or the provider's default,
dynamic toggles (ElevenLabs Indian residency, Sarvam preprocessing), the full
Sarvam request payload, fixed sample rates, and the canonical sentence — and
`content_key()` SHA-256-hashes that resolved config (truncated to 16 hex
chars). The same config object then drives synthesis, so hashing and synthesis
can never disagree. During reconcile, the freshly computed key is compared
against the manifest entry's `content_key` for the same language: a match (with
a populated `url`) means "skip, still fresh"; anything else — missing,
mismatched, or a prior failure — triggers regeneration. Because the key is a
pure function of everything that affects the audio (and nothing else — no
timestamp), any change forces a *different* key → a *different* storage path.
The new audio is written there and the manifest URL updated; the old blob is
left behind unreferenced. A client can therefore never be served stale audio at
a URL the catalog is currently advertising.

**Manifest read/write semantics:** `GET /tts/voices` reads the manifest through
a per-process TTL cache (300 s, matching the endpoint's `Cache-Control`) and
*fails soft* — on a storage error it serves the last cached value (or no
previews) rather than a 500. Reconcile goes the other way: it starts from a
*fresh* read (a backend failure aborts the run **before any paid provider
call**), and every `update_previews` does a fresh read-modify-write that
**fails hard** if the manifest can't be read, so a transient storage error can
never cause a partial manifest to overwrite a good one. A manifest that exists
but doesn't *parse* is treated as corrupt, not transient: logged loudly and
loaded as `{}`, so that run regenerates previews and its successful write
repairs the file (self-healing) instead of every future write wedging behind
the same parse error. Concurrent reconciles on different pods are
last-write-wins per manifest — an accepted trade for an admin-only, idempotent
operation (just re-run it).

## Language filter semantics

`language_matches(filter_code, voice_languages)`
(`app/api/routers/breeze_buddy/tts_catalog/matching.py`) governs
`GET /tts/voices?language=...`:

- **Empty `voice_languages`** (e.g. every gemini voice in the catalog has
  `languages: []`) means the voice is language-agnostic — it matches any
  filter value, including no filter at all.
- Otherwise, matching is case-insensitive and works **both directions** across
  the bare/regional boundary: a bare filter (`en`) matches a regional voice
  language (`en-IN`), and a regional filter (`en-IN`) matches a bare voice
  language (`en`), in addition to an exact match.
- The boundary check requires the literal `-`, so it's not a naive string
  prefix: filtering on `en` does **not** match a voice language of `enx-XX`.

`provider` is a separate, exact-match filter — a `Literal` of the 6 catalog
providers, so `provider=dragontts` (or any provider outside the 6) is a `422`,
not an empty result.

## Curation notes — never add these voice_ids

Each looks plausible in the codebase but is dead or unreachable (see also the
docstring in `app/ai/voice/tts/catalog.py`):

- `app/core/config/static.py`'s `ELEVENLABS_VOICE_ID` (default
  `"bQQWtYx9EodAqMdkrNAc"`), and the identical default echoed into
  `.env.example` — nothing in the runtime TTS dispatch or catalog code reads
  this var.
- `app/core/config/static.py`'s `GOOGLE_BRET_VOICE` / `GOOGLE_MIA_VOICE` —
  declared with env-overridable defaults, never read anywhere else.
- The template generator's placeholder `voice_id: "iB2rIwm9cQCRGWoKDRtX"`
  (`app/ai/voice/agents/breeze_buddy/template/generator/prompts.py`) — a
  made-up example id used only inside LLM prompt examples.
- Sarvam's inline `"manisha"` fallback (`SARVAM_TTS_VOICE_ID()` in
  `app/core/config/dynamic.py`, and the `.get("voice_id", "manisha")` default
  in `app/ai/voice/tts/sarvam.py`) — unreachable in practice, since
  `BB_SPEECH_PROVIDER_DEFAULTS["sarvam"]["voice_id"]` is always `"shreya"`.

## Ops notes

- **Reconcile is idempotent.** Any voice/language whose manifest `content_key`
  already matches the current resolved config is skipped with zero provider
  calls; only missing or stale previews trigger synthesis and storage. Safe to
  re-run at any time, including immediately after a failed run.
- **Reconcile is throttled to one provider call per second**
  (`RECONCILE_DELAY_SECS = 1.0`, applied via `asyncio.sleep` after each
  generate+store attempt, not after a skip) — there's no dedicated rate-limit
  infrastructure in the TTS layer, so this fixed pacing is the only protection
  against tripping a provider's rate limit during a full reconcile run.
- **Per-voice/per-language failures are recorded, not hidden — and never
  destructive.** A failure adds a `"failed"` entry (error message truncated to
  300 chars) to the response `report[]`. If the voice had **no** working
  preview for that language, an error entry (`content_key`, no `url`) is
  persisted; if it **did** have one, the last-known-good entry is left
  untouched — `GET /tts/voices` keeps serving the old audio, and its stale
  `content_key` makes the next reconcile retry. A failed *manifest write* for
  one voice is likewise logged and reported (`"manifest_write_failed"`)
  without aborting the remaining voices. Re-running reconcile retries
  whatever's still failed, missing, or stale.
- **ElevenLabs residency toggle:** `resolve_preview_config()` resolves
  residency for every ElevenLabs voice from the global
  `BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY` Redis toggle — and because that
  resolved value is part of the hashed config, flipping the toggle changes
  every ElevenLabs content key, so the next reconcile regenerates all
  ElevenLabs previews through the new endpoint/key. The per-voice `residency`
  field in catalog.json is informational; preview generation doesn't read it.
