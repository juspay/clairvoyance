"""Redbus template iteration + DragonTTS cache warm-run harness.

Drives the redbus-llm-test conversation flow end-to-end WITHOUT a phone call:

  - scripted Hindi customer personas walk every branch of the flow
    (positive 4/5, negative 1/2/3 + reason, bad-trip-no-number, praise,
    invalid rating, who-is-calling, busy, wrong number, transliterated
    numbers, doubled numbers, vague replies),
  - the bot side is the REAL LLM (Azure or the Juspay Grid "deepseek"
    gateway) called with the template's rendered system messages + tools,
    mirroring the pipecat flows protocol (function call -> transition,
    end_node speaks one closing line),
  - every bot utterance is synthesized through the LOCAL DragonTTS server
    exactly the way the live call does it: the greeting via /tts/bytes
    (mulaw 8 kHz) and each conversation sentence via /tts/stream
    (pcm_s16le 8 kHz) — so phrases land in the DragonTTS cache under the
    same keys the real call will use,
  - rule checks score flow adherence per run (function timing, exact
    scripts, Hindi-only output, one-thought-per-turn), so the template can
    be iterated on evidence.

Usage:
  ./.venv/bin/python eleven_v3/warm_harness.py \
      --template eleven_v3/redbus-llm-test.json --llm azure --runs 1

  --llm azure | grid   override the template's llm_configurations
  --runs N             run all personas N times (cache warm mode)
  --no-tts             LLM-only fast pass (template iteration)
  --report FILE        write a structured JSON report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
ENV = dotenv_values(REPO / ".env")

DRAGONTTS = (ENV.get("DRAGONTTS_URL") or "http://127.0.0.1:8000").strip().strip('"')

AZURE_API_VERSION = "2024-10-21"

# ----------------------------------------------------------------------------
# Personas: scripted customer replies, in order. `expect` is the function that
# SHOULD be called; `rating` the number the customer actually says (if any).
# ----------------------------------------------------------------------------

PERSONAS: list[dict] = [
    {
        "name": "pos4_direct",
        "turns": ["हाँ, बोलिए"],
        "rating_turn": 1,
        "reply": "चार",
        "expect": "positive_feedback",
        "rating": 4,
    },
    {
        "name": "pos5_named",
        "turns": ["हाँजी, मैं ही रहा हूँ"],
        "rating_turn": 1,
        "reply": "पाँच",
        "expect": "positive_feedback",
        "rating": 5,
    },
    {
        "name": "neg2_reason",
        "turns": ["हाँ"],
        "rating_turn": 1,
        "reply": "दो",
        "reason": "बस दो घंटे लेट थी, स्टॉप पर खड़े रहे",
        "expect": "negative_feedback",
        "rating": 2,
    },
    {
        "name": "neg1_reason",
        "turns": ["हाँ बोलो"],
        "rating_turn": 1,
        "reply": "एक",
        "reason": "सीट टूटी हुई थी और बस बिल्कुल गंदी थी",
        "expect": "negative_feedback",
        "rating": 1,
    },
    {
        "name": "neg3_reason",
        "turns": ["जी"],
        "rating_turn": 1,
        "reply": "तीन",
        "reason": "ठीक ठाक थी, पर एक घंटा लेट थी",
        "expect": "negative_feedback",
        "rating": 3,
    },
    {
        "name": "badtrip_nonumber",
        "turns": ["हाँ"],
        "rating_turn": None,
        "reply": "अच्छी नहीं थी, बहुत खराब थी",
        "reason": "सफाई बिल्कुल नहीं थी और एसी भी खराब था",
        "expect": "negative_feedback",
        "rating": None,
    },
    {
        "name": "praise_then_num",
        "turns": ["हाँ"],
        "rating_turn": 2,
        "reply": "बहुत अच्छी थी, बढ़िया",
        "rating2": "चार",
        "expect": "positive_feedback",
        "rating": 4,
    },
    {
        "name": "invalid_then_valid",
        "turns": ["हाँ"],
        "rating_turn": 2,
        "reply": "सौ",
        "rating2": "पाँच",
        "expect": "positive_feedback",
        "rating": 5,
    },
    {
        "name": "who_calling_then_rate",
        "turns": ["कौन बोल रहा है?"],
        "rating_turn": 2,
        "reply": "अच्छा जी, बताइए",
        "rating2": "चार",
        "expect": "positive_feedback",
        "rating": 4,
    },
    {
        "name": "trip_details_then_rate",
        "turns": ["हाँ"],
        "rating_turn": 3,
        "reply": "कौन सी ट्रिप? कहाँ से कहाँ थी?",
        "mid": "अच्छा जी",
        "rating3": "तीन",
        "reason": "बस पुरानी थी और ड्राइवर रूखा था",
        "expect": "negative_feedback",
        "rating": 3,
    },
    {
        "name": "busy_now",
        "turns": [],
        "rating_turn": 0,
        "reply": "अभी व्यस्त हूँ, ड्राइव कर रहा हूँ. बाद में कॉल कीजिए",
        "expect": "user_busy",
        "rating": None,
    },
    {
        "name": "wrong_number",
        "turns": [],
        "rating_turn": 0,
        "reply": "नहीं जी, ये गलत नंबर है",
        "expect": "user_busy",
        "rating": None,
    },
    {
        "name": "translit_char",
        "turns": ["haan"],
        "rating_turn": 1,
        "reply": "char",
        "expect": "positive_feedback",
        "rating": 4,
    },
    {
        "name": "doubled_five",
        "turns": ["हाँ"],
        "rating_turn": 1,
        "reply": "पाँच पाँच",
        "expect": "positive_feedback",
        "rating": 5,
    },
    {
        "name": "vague_then_neg",
        "turns": ["हाँ"],
        "rating_turn": 2,
        "reply": "पता नहीं, ठीक था",
        "rating2": "दो",
        "reason": "ड्राइवर बहुत बदतमीज़ था",
        "expect": "negative_feedback",
        "rating": 2,
    },
    {
        "name": "scale_question_then",
        "turns": ["हाँ"],
        "rating_turn": 2,
        "reply": "रेटिंग कैसे दें? कौन सा नंबर अच्छा है?",
        "rating2": "चार",
        "expect": "positive_feedback",
        "rating": 4,
    },
]


def persona_script(p: dict) -> list[str]:
    """Flat ordered user replies for a persona."""
    out = list(p["turns"])
    if p.get("reply"):
        out.append(p["reply"])
    if p.get("mid"):
        out.append(p["mid"])
    if p.get("rating2"):
        out.append(p["rating2"])
    if p.get("rating3"):
        out.append(p["rating3"])
    if p.get("reason"):
        out.append(p["reason"])
    return out


# ----------------------------------------------------------------------------
# Hindi number extraction (STT artifacts included) — mirrors template §5.
# ----------------------------------------------------------------------------

_NUM_WORDS = {
    "एक": 1,
    "ek": 1,
    "one": 1,
    "11": 1,
    "दो": 2,
    "do": 2,
    "doh": 2,
    "door": 2,
    "dore": 2,
    "two": 2,
    "22": 2,
    "तीन": 3,
    "teen": 3,
    "three": 3,
    "33": 3,
    "चार": 4,
    "char": 4,
    "chaar": 4,
    "four": 4,
    "44": 4,
    "पाँच": 5,
    "पांच": 5,
    "paanch": 5,
    "panch": 5,
    "punch": 5,
    "five": 5,
    "55": 5,
}
_DIGITS = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5}


def extract_rating(text: str) -> int | None:
    """The 1-5 number the customer said, or None. Doubled = same number."""
    t = text.strip()
    if not t:
        return None
    for word in _NUM_WORDS:  # Devanagari keys must match as whole words
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", t, re.IGNORECASE):
            v = _NUM_WORDS[word]
            if 1 <= v <= 5:
                return v
    for d, v in _DIGITS.items():
        if re.search(rf"(?<!\d){d}(?!\d)", t):
            return v
    return None


def has_busy_cue(text: str) -> bool:
    cues = [
        "व्यस्त",
        "बाद में",
        "ड्राइव",
        "मीटिंग",
        "meeting",
        "busy",
        "call later",
        "गलत नंबर",
        "wrong number",
        "नहीं हैं यहाँ",
        "नहीं है यहाँ",
        "नहीं हूँ रहा",
        "यह नहीं है",
        "ये नहीं है",
        "कॉल करना पड़ेगा",
    ]
    low = text.lower()
    return any(c in low or c in text for c in cues)


# ----------------------------------------------------------------------------
# LLM clients (mirror the runtime: Azure direct, Grid = OpenAI-compatible).
# ----------------------------------------------------------------------------


class LLMClient:
    def __init__(self, kind: str, cfg: dict | None):
        self.kind = kind  # "azure" | "grid" | "breeze"
        self.cfg = cfg or {}
        self.top_p: float | None = None
        self.session_id: str | None = None  # breeze radix-cache affinity
        self.thinking_hits = 0  # responses carrying reasoning tokens
        if kind == "azure":
            self.model = self.cfg.get("model") or ENV.get(
                "AZURE_OPENAI_MODEL", "gpt-4.1"
            )
            self.endpoint = (
                (self.cfg.get("endpoint") or ENV.get("AZURE_OPENAI_ENDPOINT", ""))
                .strip()
                .strip('"')
            )
            self.api_key = (ENV.get("AZURE_OPENAI_API_KEY") or "").strip().strip('"')
            self.url = (
                f"{self.endpoint.rstrip('/')}/openai/deployments/{self.model}"
                f"/chat/completions?api-version={AZURE_API_VERSION}"
            )
            self.headers = {"api-key": self.api_key}
        elif kind == "breeze":
            # breeze-bench self-hosted SGLang (qwen3.8-27b-4bit) behind an
            # nginx bearer proxy. Sampling is LOCKED by their measurement
            # (temp 0.2 / top_p 0.8, no penalties) — template temperature
            # is deliberately ignored for this engine.
            self.model = self.cfg.get("model") or "qwen3.8-27b-4bit"
            # Env wins over the template endpoint so local runs can point
            # at the SSH tunnel (the VM's :8000 is firewalled off-machine).
            self.endpoint = (
                os.environ.get("BREEZE_LLM_BASE")
                or self.cfg.get("endpoint")
                or ENV.get("BREEZE_LLM_BASE")
                or "http://127.0.0.1:18000/v1"
            ).rstrip("/")
            self.api_key = (
                (
                    os.environ.get("BREEZE_LLM_API_KEY")
                    or ENV.get("BREEZE_LLM_API_KEY")
                    or ""
                )
                .strip()
                .strip('"')
            )
            self.url = f"{self.endpoint}/chat/completions"
            self.headers = {"Authorization": f"Bearer {self.api_key}"}
        else:
            self.model = self.cfg.get("model") or "deepseek"
            self.endpoint = (
                self.cfg.get("endpoint") or "https://grid.ai.juspay.net"
            ).rstrip("/")
            keyname = self.cfg.get("api_key_name") or "GRID_API_KEY"
            self.api_key = (ENV.get(keyname) or "").strip().strip('"')
            self.url = f"{self.endpoint}/chat/completions"
            self.headers = {"Authorization": f"Bearer {self.api_key}"}
        self.temperature = self.cfg.get("temperature")
        if self.temperature is None:
            self.temperature = 0.1 if self.cfg else 0.7
        self.max_tokens = self.cfg.get("max_tokens") or 500
        if kind == "breeze":
            self.temperature = 0.2  # locked by measurement (OPTIMIZATIONS.md §9.3)
            self.top_p = 0.8
            self.max_tokens = min(int(self.max_tokens or 300), 500)

    async def chat(
        self,
        client: httpx.AsyncClient,
        messages: list,
        tools: list | None,
        tool_choice: str = "auto",
    ) -> dict:
        body: dict = {
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if self.session_id and self.kind == "breeze":
            body["session_id"] = self.session_id
        if self.kind in (
            "grid",
            "breeze",
        ):  # OpenAI-compatible endpoints take model in body
            body["model"] = self.model
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": f["function_name"],
                        "description": f["description"],
                        "parameters": {
                            "type": "object",
                            "properties": f.get("properties", {}),
                            "required": f.get("required", []),
                        },
                    },
                }
                for f in tools
            ]
            body["tool_choice"] = tool_choice
        # A/B switch: sglang/Qwen3 thinking off (chat_template_kwargs is
        # applied by the server's chat template). Set BREEZE_DISABLE_THINKING=1.
        if os.environ.get("BREEZE_DISABLE_THINKING") == "1" and self.kind == "breeze":
            body["chat_template_kwargs"] = {"enable_thinking": False}
        last_err = None
        for attempt in range(3):
            try:
                r = await client.post(
                    self.url, headers=self.headers, json=body, timeout=90.0
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:400]}")
                data = r.json()
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                content = msg.get("content") or ""
                if str(reasoning).strip() or "<think" in str(content).lower():
                    self.thinking_hits += 1
                return data
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"LLM call failed after retries: {last_err}")


# ----------------------------------------------------------------------------
# DragonTTS client — greeting via /tts/bytes (mulaw 8k), sentences via
# /tts/stream (pcm_s16le 8k), matching the live telephony paths.
# ----------------------------------------------------------------------------


class TTSClient:
    def __init__(self, tts_cfg: dict):
        self.voice_id = tts_cfg["voice_id"]
        self.model = tts_cfg.get("model") or "eleven_v3_conversational"
        provider = tts_cfg.get("provider") or "elevenlabs"
        self.model_id = f"{provider}:{self.model}"
        self.language = tts_cfg.get("language") or ""
        params = {
            k: tts_cfg[k]
            for k in (
                "speed",
                "volume",
                "emotion",
                "pitch",
                "style_prompt",
                "enable_ssml_parsing",
                "similarity_boost",
                "stability",
            )
            if tts_cfg.get(k) is not None
        }
        self.params = params
        self.stats: dict[str, dict] = {}  # phrase -> {hits, misses, ms, bytes}

    def _body(self, text: str, encoding: str, rate: int) -> dict:
        return {
            "model_id": self.model_id,
            "transcript": text,
            "voice": {"mode": "id", "id": self.voice_id},
            "language": self.language,
            "output_format": {
                "container": "raw",
                "encoding": encoding,
                "sample_rate": rate,
            },
            "params": self.params,
        }

    def _note(self, phrase: str, hit: bool, ms: float, nbytes: int) -> None:
        s = self.stats.setdefault(
            phrase, {"hits": 0, "misses": 0, "ms": [], "bytes": 0}
        )
        s["hits" if hit else "misses"] += 1
        s["ms"].append(round(ms, 1))
        s["bytes"] = max(s["bytes"], nbytes)

    async def speak_greeting(self, client: httpx.AsyncClient, text: str) -> None:
        t0 = time.perf_counter()
        r = await client.post(
            f"{DRAGONTTS}/tts/bytes", json=self._body(text, "mulaw", 8000), timeout=90.0
        )
        r.raise_for_status()
        hit = r.headers.get("X-Cache") == "HIT"
        self._note(text, hit, (time.perf_counter() - t0) * 1000, len(r.content))

    async def speak_sentence(self, client: httpx.AsyncClient, text: str) -> None:
        """Stream one sentence like the live conversation path does."""
        t0 = time.perf_counter()
        nbytes = 0
        async with client.stream(
            "POST",
            f"{DRAGONTTS}/tts/stream",
            json=self._body(text, "pcm_s16le", 8000),
            timeout=90.0,
        ) as r:
            if r.status_code >= 400:
                detail = (await r.aread()).decode("utf-8", "replace")[:300]
                raise RuntimeError(f"/tts/stream {r.status_code}: {detail}")
            hit = r.headers.get("X-Cache") == "HIT"
            async for chunk in r.aiter_bytes():
                nbytes += len(chunk)
        self._note(text, hit, (time.perf_counter() - t0) * 1000, nbytes)


def split_sentences(text: str) -> list[str]:
    """Split a bot turn the way pipecat's SENTENCE aggregation feeds the TTS."""
    parts = re.split(r"(?<=[।!?\.])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


# ----------------------------------------------------------------------------
# Flow runner
# ----------------------------------------------------------------------------


def render(text: str, payload: dict) -> str:
    for k, v in payload.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def payload_from_template(tpl: dict) -> dict:
    return {
        k: v.get("example")
        for k, v in (tpl.get("expected_payload_schema") or {}).items()
        if isinstance(v, dict) and v.get("example")
    }


def closing_lines(tpl: dict) -> dict[str, str]:
    """Parse the end_node closing scripts keyed by function name."""
    end = next(n for n in tpl["flow"]["nodes"] if n["node_name"] == "end_node")
    text = end["task_messages"][0]["content"]
    lines = {}
    for m in re.finditer(
        r"If the last function was (\w+):\s*\n(.+?)(?=\n\n|\Z)", text, re.DOTALL
    ):
        lines[m.group(1)] = m.group(2).strip()
    return lines


class Runner:
    def __init__(self, tpl: dict, llm: LLMClient, tts: TTSClient | None, use_tts: bool):
        self.tpl = tpl
        self.llm = llm
        self.tts = tts
        self.use_tts = use_tts
        self.payload = payload_from_template(tpl)
        flow = tpl["flow"]
        self.nodes = {n["node_name"]: n for n in flow["nodes"]}
        self.initial = flow["initial_node"]
        self.greeting = render(
            (tpl.get("configurations") or {}).get("initial_greeting") or "",
            self.payload,
        )
        self.closing = closing_lines(tpl)
        tts_cfg = (tpl.get("configurations") or {}).get("tts_configuration") or {}
        self.ask_reason_marker = "कारण क्या था"
        # Every scripted line the bot may speak verbatim (§8 + the TRIP CONTEXT
        # "speak it exactly" lines). Used to exempt exact scripts from the
        # two-sentence turn-length rule — scripts are spoken as written.
        self.scripts: set[str] = set()
        for n in flow["nodes"]:
            for key in ("role_messages", "task_messages"):
                for msg in n.get(key, []):
                    c = render(msg["content"], self.payload)
                    for m in re.finditer(r"speak it exactly:\s*\n(.+)", c):
                        self.scripts.add(m.group(1).strip())
                    for m in re.finditer(
                        r"(?:RATING RE-ASK|INVALID RATING|SCALE EXPLAINED|"
                        r"ASK REASON|IDLE CHECK)[^\n]*\n(.+)",
                        c,
                    ):
                        line = m.group(1).strip()
                        if line and not line.startswith("जी. मैं"):
                            self.scripts.add(line)
        self.rating_request = next(
            (s for s in self.scripts if s.startswith("ठीक है. आपने")), None
        )
        self.why_calling = next(
            (s for s in self.scripts if s.startswith("जी. मैं")), None
        )

    async def run_persona(self, http: httpx.AsyncClient, persona: dict) -> dict:
        result = {
            "persona": persona["name"],
            "expect": persona["expect"],
            "turns": [],
            "function": None,
            "function_args": None,
            "violations": [],
            "bot_phrases": [],
        }
        v = result["violations"]
        script = persona_script(persona)
        node = self.initial
        history: list[dict] = [{"role": "assistant", "content": self.greeting}]
        ask_reason_at = None  # turn index when the bot spoke ASK REASON
        rating_request_spoken = False
        said_rating: int | None = None  # rating the customer actually gave
        fn_name, fn_args = None, None

        def bot_say(turn_idx: int, text: str) -> None:
            result["bot_phrases"].append(text)
            sents = split_sentences(text)
            # Exact scripted lines are spoken verbatim by design even when
            # they run past two sentences.
            is_script = text.strip() in self.scripts
            if len(sents) > 2 and not is_script:
                v.append(f"turn_too_long({len(sents)} sentences): {text[:60]}…")
            # Payload values (Mumbai, ABC travels…) are spoken as-is by design.
            stripped = text
            for val in self.payload.values():
                stripped = stripped.replace(str(val), "")
            if re.search(r"[A-Za-z0-9]", stripped):
                v.append(f"english_leak: {text[:60]}…")
            if self.ask_reason_marker in text:
                nonlocal ask_reason_at
                ask_reason_at = turn_idx
            if self.rating_request and text.strip() == self.rating_request:
                nonlocal rating_request_spoken
                rating_request_spoken = True

        turn_idx = 0
        user_text = None
        while turn_idx < len(script) + 1 and turn_idx < 12:
            if turn_idx < len(script):
                user_text = script[turn_idx]
                history.append({"role": "user", "content": user_text})
                # Track the rating the customer actually gave: only short
                # replies count (reasons like "एक घंटा लेट थी" carry stray
                # number words that must NOT overwrite the real rating).
                r_now = extract_rating(user_text)
                if r_now is not None and len(user_text.split()) <= 3:
                    said_rating = r_now

            node_msgs = [
                {"role": m["role"], "content": render(m["content"], self.payload)}
                for m in self.nodes[node].get("role_messages", [])
            ] + [
                {"role": m["role"], "content": render(m["content"], self.payload)}
                for m in self.nodes[node].get("task_messages", [])
            ]
            tools = self.nodes[node].get("functions") or []
            msg_out = await self.llm.chat(http, node_msgs + history, tools)
            choice = msg_out["choices"][0]["message"]
            content = (choice.get("content") or "").strip()
            tool_calls = choice.get("tool_calls") or []

            if len(tool_calls) > 1:
                v.append(f"multi_fn: {[c['function']['name'] for c in tool_calls]}")

            if tool_calls:
                fn_name = tool_calls[0]["function"]["name"]
                try:
                    fn_args = json.loads(
                        tool_calls[0]["function"].get("arguments") or "{}"
                    )
                except json.JSONDecodeError:
                    fn_args = {}
                    v.append("fn_args_not_json")
                if content:
                    v.append(f"spoke_on_fn_turn: {content[:60]}…")
                # Function timing rules (template §3/§5/§6) — against the
                # rating the customer actually said (said_rating), never a
                # number word that merely appears inside their reason.
                if fn_name == "positive_feedback":
                    if said_rating is None:
                        v.append(f"pos_without_number (customer never said one)")
                    elif said_rating not in (4, 5):
                        v.append(f"pos_wrong_bucket: customer said {said_rating}")
                    elif fn_args.get("rating") != said_rating:
                        v.append(
                            f"pos_rating_mismatch: arg={fn_args.get('rating')} "
                            f"customer={said_rating}"
                        )
                elif fn_name == "negative_feedback":
                    if ask_reason_at is None or ask_reason_at >= turn_idx:
                        v.append(
                            "neg_before_reason: no ASK REASON + answer before call"
                        )
                    fb = str(fn_args.get("feedback") or "").strip()
                    if not fb:
                        v.append("neg_feedback_empty")
                    elif extract_rating(fb) is not None and len(fb.split()) <= 2:
                        v.append(f"neg_feedback_is_number: {fb!r}")
                    if "rating" in fn_args and said_rating is None:
                        v.append("neg_invented_rating")
                    elif (
                        "rating" in fn_args
                        and said_rating is not None
                        and fn_args["rating"] != said_rating
                    ):
                        v.append(
                            f"neg_rating_mismatch: arg={fn_args['rating']} "
                            f"customer={said_rating}"
                        )
                elif fn_name == "user_busy":
                    if (
                        not has_busy_cue(user_text or "")
                        and persona["expect"] != "user_busy"
                    ):
                        v.append(f"busy_misfire (last user: {user_text!r})")
                else:
                    v.append(f"unknown_function: {fn_name}")
                result["function"] = fn_name
                result["function_args"] = fn_args
                if fn_name != persona["expect"]:
                    v.append(
                        f"function_mismatch: got {fn_name}, expected {persona['expect']}"
                    )

                # Record the tool exchange, then transition.
                history.append(
                    {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": [
                            {
                                "id": tool_calls[0]["id"],
                                "type": "function",
                                "function": tool_calls[0]["function"],
                            }
                        ],
                    }
                )
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_calls[0]["id"],
                        "content": "ok",
                    }
                )
                node = next(
                    (
                        f.get("transition_to")
                        for f in tools
                        if f["function_name"] == fn_name and f.get("transition_to")
                    ),
                    node,
                )
                # Closing node speaks exactly one line (no tools).
                closing_msgs = [
                    {"role": m["role"], "content": render(m["content"], self.payload)}
                    for m in self.nodes[node].get("task_messages", [])
                ] + [
                    {"role": m["role"], "content": render(m["content"], self.payload)}
                    for m in self.nodes[node].get("role_messages", [])
                ]
                msg_out = await self.llm.chat(http, closing_msgs + history, None)
                closing = (
                    msg_out["choices"][0]["message"].get("content") or ""
                ).strip()
                expected = self.closing.get(fn_name)
                if expected and closing != expected:
                    v.append(
                        f"closing_mismatch:\n  got      {closing[:90]}\n  expected {expected[:90]}"
                    )
                if closing:
                    result["bot_phrases"].append(closing)
                    if self.use_tts and self.tts:
                        for s in split_sentences(closing):
                            await self.tts.speak_sentence(http, s)
                result["turns"].append(
                    {
                        "user": user_text,
                        "fn": fn_name,
                        "args": fn_args,
                        "closing": closing,
                    }
                )
                break

            if content:
                bot_say(turn_idx, content)
                result["turns"].append({"user": user_text, "bot": content})
                history.append({"role": "assistant", "content": content})
                # After an identity confirmation the very first bot line must
                # be the exact RATING REQUEST script; if they asked who/why,
                # the correct first line is the exact WHY CALLING script and
                # RATING REQUEST comes on the NEXT turn.
                if turn_idx == 0 and self.rating_request:
                    first_user = script[0] if script else ""
                    asked_why = any(
                        w in first_user for w in ("कौन", "क्यों", "किसने", "who", "why")
                    )
                    expected = self.why_calling if asked_why else self.rating_request
                    if expected and content.strip() != expected:
                        v.append(
                            f"first_line_modified:\n"
                            f"  got      {content[:110]}\n"
                            f"  expected {expected[:110]}"
                        )
                if self.use_tts and self.tts:
                    for s in split_sentences(content):
                        await self.tts.speak_sentence(http, s)
            else:
                v.append(f"empty_bot_turn after user: {user_text!r}")

            turn_idx += 1
        else:
            v.append("no_function_ending: persona script exhausted")

        return result


LLM_CONFIGS = {
    "azure": {
        "provider": "azure",
        "model": "gpt-4.1",  # gpt-4o deployment absent on this endpoint
        "temperature": 0.1,
        "max_tokens": 500,
    },
    "grid": {
        "provider": "openai",
        "model": "deepseek",
        "endpoint": "https://grid.ai.juspay.net",
        "api_key_name": "GRID_API_KEY",
        "temperature": 0.1,
        "max_tokens": 500,
    },
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--llm", choices=["azure", "grid", "template"], default="template")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--persona", help="run a single persona by name")
    ap.add_argument("--report")
    args = ap.parse_args()

    tpl = json.loads(Path(args.template).read_text())
    llm_cfg = (
        tpl.get("configurations", {}).get("llm_configurations")
        if args.llm == "template"
        else LLM_CONFIGS[args.llm]
    )
    if not llm_cfg:
        print(
            "template has no llm_configurations — pass --llm azure|grid",
            file=sys.stderr,
        )
        return 2
    llm = LLMClient("azure" if llm_cfg.get("provider") == "azure" else "grid", llm_cfg)
    tts_cfg = dict(tpl["configurations"]["tts_configuration"])
    use_tts = not args.no_tts
    tts = TTSClient(tts_cfg) if use_tts else None
    runner = Runner(tpl, llm, tts, use_tts)

    personas = [p for p in PERSONAS if not args.persona or p["name"] == args.persona]
    print(
        f"harness: llm={llm.kind} model={llm.model} temp={llm.temperature} "
        f"tts={'on' if use_tts else 'off'} voice={tts_cfg['voice_id']} "
        f"model_id={tts.model_id if tts else '-'} personas={len(personas)} runs={args.runs}"
    )

    report = {
        "llm": llm.kind,
        "model": llm.model,
        "template": args.template,
        "runs": [],
    }
    async with httpx.AsyncClient() as http:
        if use_tts and tts:
            await tts.speak_greeting(http, runner.greeting)
        for run_no in range(args.runs):
            for p in personas:
                t0 = time.perf_counter()
                res = await runner.run_persona(http, p)
                res["secs"] = round(time.perf_counter() - t0, 1)
                report["runs"].append(res)
                status = "OK " if not res["violations"] else "BAD"
                print(
                    f"[{status}] {p['name']:<22} fn={str(res['function']):<18} "
                    f"{res['secs']:>5}s turns={len(res['turns'])} "
                    f"viol={len(res['violations'])}"
                )
                for viol in res["violations"]:
                    print(f"       - {viol}")

    # Summary
    total_viol = sum(len(r["violations"]) for r in report["runs"])
    ok_runs = sum(1 for r in report["runs"] if not r["violations"])
    print(
        f"\nruns={len(report['runs'])} clean={ok_runs} "
        f"violations={total_viol} ({total_viol / max(1, len(report['runs'])):.2f}/run)"
    )
    if tts:
        phrases = tts.stats
        total_req = sum(s["hits"] + s["misses"] for s in phrases.values())
        hits = sum(s["hits"] for s in phrases.values())
        misses = sum(s["misses"] for s in phrases.values())
        hit_ms = [
            m for s in phrases.values() for m in s["ms"][s["misses"] :]
        ]  # after first
        print(
            f"tts: unique_phrases={len(phrases)} requests={total_req} "
            f"hits={hits} misses={misses} hit_rate={hits / max(1, total_req) * 100:.0f}%"
        )
        if misses:
            print("uncached phrases (misses):")
            for ph, s in sorted(phrases.items(), key=lambda kv: -kv[1]["misses"]):
                if s["misses"]:
                    print(f"  [{s['misses']} miss/{s['hits']} hit] {ph[:80]}")
        report["tts"] = {
            "unique_phrases": len(phrases),
            "hits": hits,
            "misses": misses,
            "phrases": {ph: {k: v for k, v in s.items()} for ph, s in phrases.items()},
        }

    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"report: {args.report}")
    return 0 if total_viol == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
