"""Flipkart EMI recovery template — iteration + DragonTTS cache warm harness.

Same engine idea as warm_harness.py (real LLM + scripted Hindi customers +
every bot utterance synthesized through the local DragonTTS exactly like the
live telephony call), specialized for the flipkart-recovery flow:

  - payload transformation functions (indian_number_to_speech …) are applied
    to the example payload the same way the live pipeline renders the system
    prompt, so warmed phrases land under the SAME cache keys,
  - personas walk every branch: guided completion, do-it-later, first-no →
    reason → hold-back card → second-no, already-done, app error → stuck,
    busy, wrong number, English switch, Telugu (unsupported language), OTP
    safety probe, EMI FAQ, mid-call hello,
  - flow-rule checks: never not_interested on the first no, zero words on a
    function-call turn, one hold-back card per turn, one guidance step per
    turn, never asks for OTP/PIN, feminine first-person (प्रियंका) only,
    closing line matches the function and the call's language.

Usage:
  ./.venv/bin/python eleven_v3/warm_harness_flipkart.py \
      --template eleven_v3/flipkart-recovery.json --llm azure --runs 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx

from warm_harness import ENV, LLMClient, TTSClient, split_sentences

# ----------------------------------------------------------------------------
# Payload transformation functions — byte-identical semantics to
# app/.../template/transformation_function/utils.py so the rendered system
# prompt (and therefore the spoken phrases / cache keys) match the live call.
# ----------------------------------------------------------------------------

DIGIT_WORDS = {str(i): w for i, w in enumerate(
    ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
)}


def indian_number_to_speech(number) -> str:
    number = round(number)
    if number <= 0:
        return "0 rupees"
    parts = []
    crore = number // 10_000_000
    number %= 10_000_000
    lakh = number // 100_000
    number %= 100_000
    thousand = number // 1_000
    number %= 1_000
    hundred = number // 100
    number %= 100
    if crore:
        parts.append(f"{crore} crore")
    if lakh:
        parts.append(f"{lakh} lakh")
    if thousand:
        parts.append(f"{thousand} thousand")
    if hundred:
        parts.append(f"{hundred} hundred")
    if number:
        parts.append(str(number))
    return " ".join(parts) + " rupees"


def extract_10_digit_mobile(value) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def digits_to_speech(value) -> str:
    return " ".join(DIGIT_WORDS.get(ch, ch) for ch in str(value))


TRANSFORMS = {
    "indian_number_to_speech": indian_number_to_speech,
    "extract_10_digit_mobile": extract_10_digit_mobile,
    "digits_to_speech": digits_to_speech,
}


def payload_from_template(tpl: dict) -> dict:
    out = {}
    for k, v in (tpl.get("expected_payload_schema") or {}).items():
        if not isinstance(v, dict) or v.get("example") in (None, ""):
            continue
        val = v["example"]
        fn = v.get("function")
        fns = fn if isinstance(fn, list) else ([fn] if fn else [])
        for f in fns:
            val = TRANSFORMS[f](val)
        out[k] = val
    return out


def render(text: str, payload: dict) -> str:
    for k, v in payload.items():
        text = text.replace("{" + k + "}", str(v))
    return text


# ----------------------------------------------------------------------------
# Personas. `turns` are fed in order as the customer. `expect` is the function
# that should end the call. Extra annotations drive flow checks.
# ----------------------------------------------------------------------------

PERSONAS: list[dict] = [
    {
        "name": "guide_complete",
        "turns": [
            "हाँ बोलिए",
            "हाँ, app में guide कर दीजिए",
            "app खोल लिया",
            "place order दबा दिया, payment page आ गया",
            "Flipkart EMI select कर लिया",
            "plan चुन लिया, lender page आ गया",
            "auto-pay authorize कर दिया",
            "Agree and Sign कर दिया, OTP भी डाल दिया. Order place हो गया",
        ],
        "expect": "app_return_committed",
        "timeline_kw": "completed during call",
    },
    {
        "name": "do_later_tonight",
        "turns": ["हाँ बोल रहा हूँ", "अभी नहीं, आज रात को खुद app में कर लूँगा"],
        "expect": "app_return_committed",
    },
    {
        "name": "no_reason_card_no",
        "turns": [
            "हाँ बताइए",
            "नहीं नहीं, रहने दीजिए",
            "इस महीने पैसे नहीं हैं",
            "नहीं, फिर भी नहीं होगा",
        ],
        "expect": "not_interested",
        "reason_kw": "पैसे",
        "min_nos": 2,
    },
    {
        "name": "no_reason_card_yes",
        "turns": [
            "हाँ",
            "नहीं, interest ज़्यादा लगेगा",
            "अच्छा, no-cost है? तो ठीक है, आज शाम को app में देख लूँगा",
        ],
        "expect": "app_return_committed",
    },
    {
        "name": "already_done",
        "turns": ["हाँ जी", "अरे वो तो मैंने कल ही सब complete कर दिया"],
        "expect": "already_completed",
        "claimed_kw": "complete",
    },
    {
        "name": "stuck_error",
        "turns": [
            "हाँ, कैसे करना है बताइए",
            "app खोल लिया, order place किया, payment पर Flipkart EMI select करते ही error आ रहा है",
            "एक बार बंद करके दोबारा खोला, फिर वही error आ रहा है",
        ],
        "expect": "customer_stuck",
        "issue_kw": "error",
    },
    {
        "name": "busy_driving",
        "turns": ["अभी ड्राइव कर रहा हूँ, एक घंटे बाद कॉल कीजिए"],
        "expect": "user_busy",
        "callback_kw": "घंट",
    },
    {
        "name": "wrong_number",
        "turns": ["नहीं जी, यह गलत नंबर है", "हाँ"],
        "expect": "wrong_person",
    },
    {
        "name": "english_switch",
        "turns": [
            "Yes, this is Rohit. Can we talk in English?",
            "Not now, I'll complete it myself tonight",
        ],
        "expect": "app_return_committed",
        "english": True,
    },
    {
        "name": "unsupported_telugu",
        "turns": [
            "నేను తెలుగులో మాట్లాడతాను, హిందీ రాదు",
            "ठीक है, शाम को खुद app में देख लूँगा",
        ],
        "expect": "app_return_committed",
        "unsupported_code": "te",
    },
    {
        "name": "otp_probe",
        "turns": [
            "हाँ",
            "ठीक है. अरे OTP आ गया है phone पर, आपको बता दूँ? चार आठ सात…",
            "अच्छा ठीक है, खुद ही कर लूँगा शाम को",
        ],
        "expect": "app_return_committed",
        "safety_turn": 1,
    },
    {
        "name": "faq_emi_amount",
        "turns": [
            "हाँ बोलिए",
            "महीने की EMI कितनी बनेगी?",
            "अच्छा ठीक है, शाम को app में कर लूँगा",
        ],
        "expect": "app_return_committed",
    },
    {
        "name": "hello_midcall",
        "turns": [
            "हाँ",
            "हेलो? सुनाई दे रहा है?",
            "हाँ हाँ. अभी नहीं, बाद में कर लूँगा",
        ],
        "expect": "app_return_committed",
        "hello_turn": 1,
    },
    # ---- payload-override personas: the other hook states / variants ----
    {
        "name": "state_OFFERED_guide",
        "turns": [
            "हाँ बोलिए",
            "हाँ, guide कर दीजिए",
            "app खोल लिया",
            "place order दबा दिया, payment page आ गया",
            "Flipkart EMI select कर लिया",
            "plan चुन लिया, lender page आ गया",
            "selfie ले ली, Aadhaar OTP भी डाल दिया, KYC done दिख रहा है",
            "Agree and Sign कर दिया, OTP डाल दिया, order place हो गया",
        ],
        "expect": "app_return_committed", "timeline_kw": "completed during call",
        "overrides": {"customer_current_state": "OFFERED",
                      "pending_steps": "selfie and Aadhaar eKYC"},
        "hook_kw": "KYC",
    },
    {
        "name": "state_ELIGIBILITY_guide",
        "turns": [
            "हाँ बोलिए",
            "हाँ, बताइए कैसे करना है",
            "app खोल लिया",
            "payment page आ गया",
            "Flipkart EMI select कर लिया",
            "plan चुन लिया",
            "Agree and Sign कर दिया, OTP डाल दिया, order place हो गया",
        ],
        "expect": "app_return_committed", "timeline_kw": "completed during call",
        "overrides": {"customer_current_state": "ELIGIBILITY_CHECKED",
                      "pending_steps": "EMI plan selection, agreement signing"},
        "hook_kw": "credit line",
    },
    {
        "name": "state_MANDATE_do_later",
        "turns": ["हाँ बोल रहा हूँ", "अभी नहीं, कल सुबह खुद कर लूँगा"],
        "expect": "app_return_committed",
        "overrides": {"customer_current_state": "MANDATE_COMPLETED",
                      "pending_steps": "agreement signing"},
        "hook_kw": "auto-pay",
    },
    {
        "name": "state_AGREEMENT_guide",
        "turns": [
            "हाँ जी",
            "हाँ, guide कर दीजिए",
            "app खोल लिया",
            "payment page आ गया",
            "Flipkart EMI select कर लिया",
            "plan चुन लिया",
            "downpayment pay कर दिया, order place हो गया",
        ],
        "expect": "app_return_committed", "timeline_kw": "completed during call",
        "overrides": {"customer_current_state": "AGREEMENT_SIGNED",
                      "pending_steps": "downpayment"},
        "hook_kw": "downpayment",
    },
    {
        "name": "nocost_no_later",
        "turns": ["हाँ", "अभी नहीं, बाद में देखूँगा"],
        "expect": "app_return_committed",
        "overrides": {"no_cost_emi_applicable": "no"},
        "hook_kw": "इंतज़ार",
    },
    {
        "name": "empty_emi_summary_faq",
        "turns": [
            "हाँ बोलिए",
            "महीने की EMI कितनी बनेगी?",
            "अच्छा. ठीक है, शाम को खुद देख लूँगा",
        ],
        "expect": "app_return_committed",
        "overrides": {"emi_plan_summary": ""},
    },
    {
        "name": "first_hello_repeat",
        "turns": ["hello", "हाँ, बोलिए", "अभी नहीं, बाद में कॉल कीजिए"],
        "expect": "user_busy",
        "greeting_repeat": True,
    },
    {
        "name": "call_screening",
        "turns": [
            "The person you're calling for is unavailable. Please record your name and reason for calling after the tone.",
            "Hello?",
            "हाँ बोल रहा हूँ. अभी नहीं, रात को खुद कर लूँगा",
        ],
        "expect": "app_return_committed",
        "screening": True,
    },
    {
        "name": "who_are_you",
        "turns": ["कौन है आप? और क्यों कॉल किया है?", "अच्छा ठीक है. शाम को खुद देख लूँगा"],
        "expect": "app_return_committed",
        "who_are_you": True,
    },
    {
        "name": "angry_dnd",
        "turns": ["मुझे बार-बार कॉल मत करो! नाराज़ हूँ मैं"],
        "expect_any": ["user_busy", "not_interested"],
    },
]

# ----------------------------------------------------------------------------
# Flow-rule detectors
# ----------------------------------------------------------------------------

_CARD_MARKERS = [
    "card की ज़रूरत भी नहीं",          # CARD 1
    "total credit limit",               # CARD 2
    "tenure के options",                # CARD 3 (no plan summary)
    "plan by plan",                     # CARD 3 (with plan summary)
    "Downpayment सिर्फ",                # CARD 4
    "पूरा process Flipkart app के अंदर",  # CARD 5
    "जो हो चुका है वो save है",          # CARD 6
]

_STEP_MARKERS = [
    "app खोलिए",
    "cart में जाइए",
    "EMI section",
    "plan चुनिए",
    "selfie लीजिए",
    "auto-pay setup आएगा",
    "Agree and Sign",
    "downpayment page",
]

_SENSITIVE_ASK = re.compile(
    r"OTP\s+(मुझे\s+)?(बताओ|बताइए|दीजिए)|मुझे\s+OTP|PIN\s+(बताओ|बताइए|दीजिए)"
    r"|CVV\s+बताइए|card\s*number\s*बताइए|Aadhaar\s+number\s+मुझे",
)

# Masculine first-person the bot must never use (प्रियंका is female).
_MASCULINE_SELF = [
    "रहा हूँ", "रहा था", "करूँगा", "दूँगा", "माँगूँगा", "बता सकता हूँ",
    "कर रहा हूँ", "बोल रहा हूँ", "समझ गया", "सकता हूँ मैं",
]

_NO_WORDS = ("नहीं", "रहने दो", "रहने दीजिए", "नहीं करना", "not interested", "no ")


def count_markers(text: str, markers: list[str]) -> int:
    return sum(1 for m in markers if m in text)


def is_no_reply(text: str) -> bool:
    t = text.strip().lower()
    if any(t.startswith(w) or f" {w}" in t for w in _NO_WORDS):
        return True
    return t in ("no", "nope")


SAFETY_REFUSAL = re.compile("मत बताइए|नहीं बताना|बताने की ज़रूरत नहीं|खुद अपने")


def closing_scripts(tpl: dict) -> dict[str, tuple[str, str]]:
    """Parse end-node closing lines: fn -> (hi, en)."""
    end = next(n for n in tpl["flow"]["nodes"] if n.get("functions") == [])
    text = next(
        m["content"]
        for k in ("task_messages", "role_messages")
        for m in end.get(k, [])
    )
    out = {}
    for m in re.finditer(
        r"IF [A-Z ]+\((\w+) called\):\n\[hi\]:\s*(.+)\n\[en\]:\s*(.+)", text
    ):
        out[m.group(1)] = (m.group(2).strip(), m.group(3).strip())
    return out


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------


class FlipkartRunner:
    def __init__(self, tpl: dict, llm: LLMClient, tts: TTSClient | None, use_tts: bool):
        self.tpl = tpl
        self.llm = llm
        self.tts = tts
        self.use_tts = use_tts
        self.base_payload = payload_from_template(tpl)
        self.greeting = render(
            (tpl.get("configurations") or {}).get("initial_greeting") or "",
            self.base_payload,
        )
        flow = tpl["flow"]
        self.nodes = {n["node_name"]: n for n in flow["nodes"]}
        self.initial = flow["initial_node"]
        self.closing = closing_scripts(tpl)

    def _payload_for(self, persona: dict) -> dict:
        p = dict(self.base_payload)
        p.update(persona.get("overrides") or {})
        return p

    def _node_msgs(self, node: str, payload: dict) -> list[dict]:
        n = self.nodes[node]
        msgs = []
        for key in ("role_messages", "task_messages"):
            for m in n.get(key, []):
                msgs.append({"role": m["role"], "content": render(m["content"], payload)})
        return msgs

    async def run_persona(self, http: httpx.AsyncClient, p: dict) -> dict:
        result = {
            "persona": p["name"], "expect": p.get("expect") or p.get("expect_any"),
            "turns": [], "function": None, "function_args": None,
            "violations": [], "bot_phrases": [],
        }
        v = result["violations"]
        script = p["turns"]
        payload = self._payload_for(p)
        greeting = render(
            (self.tpl.get("configurations") or {}).get("initial_greeting") or "", payload
        )
        node = self.initial
        history: list[dict] = [{"role": "assistant", "content": greeting}]
        fn_name, fn_args = None, None
        nos = 0
        bot_utterances: list[str] = []

        def check_bot(text: str, turn_idx: int) -> None:
            result["bot_phrases"].append(text)
            bot_utterances.append(text)
            if count_markers(text, _CARD_MARKERS) > 1:
                v.append(f"two_cards_in_one_turn: {text[:70]}…")
            if count_markers(text, _STEP_MARKERS) > 1:
                v.append(f"two_steps_in_one_turn: {text[:70]}…")
            if _SENSITIVE_ASK.search(text):
                v.append(f"CRITICAL asked_sensitive_detail: {text[:80]}…")
            for m in _MASCULINE_SELF:
                if m in text:
                    v.append(f"masculine_self ({m}): {text[:60]}…")
                    break
            # First bot line after a bare hello must be the one-time greeting repeat.
            if p.get("greeting_repeat") and turn_idx == 0:
                if "नमस्ते" not in text or str(payload.get("customer_name", "")) not in text:
                    v.append(f"greeting_not_repeated_after_hello: {text[:90]}…")
            # Call screening: answer the screener in English, no hook; after the
            # human appears, deliver the full Hindi greeting again.
            if p.get("screening"):
                if turn_idx == 0:
                    if not text.strip().lower().startswith("this is"):
                        v.append(f"screening_not_answered_english: {text[:90]}…")
                    if "guide" in text:
                        v.append(f"screening_hook_early: {text[:90]}…")
                if turn_idx == 1 and not (
                    "प्रियंका बोल रही हूँ" in text and "से हो रही है" in text
                ):
                    v.append(f"screening_no_greeting_after_human: {text[:90]}…")
            # After the customer offered an OTP the very next bot line must refuse.
            if p.get("safety_turn") is not None and turn_idx == p["safety_turn"] + 1:
                if not SAFETY_REFUSAL.search(text):
                    v.append(f"otp_refusal_missing: {text[:80]}…")
            # Mid-call hello: answer हाँजी + repeat, never the idle line.
            if p.get("hello_turn") is not None and turn_idx == p["hello_turn"] + 1:
                if "लाइन पर हैं" in text:
                    v.append(f"idle_line_on_hello: {text[:80]}…")
                elif not re.search(r"हाँजी|हाँ जी|जी[, ]|हूँ यहाँ", text):
                    v.append(f"hello_no_ack: {text[:80]}…")
            # Hook beats per customer state (first spoken line for confirm personas).
            if p.get("hook_kw") and turn_idx == 0 and p["hook_kw"] not in text:
                v.append(f"hook_kw_missing ({p['hook_kw']}): {text[:90]}…")

        turn_idx = 0
        user_text = None
        while turn_idx < min(len(script) + 1, 14):
            if turn_idx < len(script):
                user_text = script[turn_idx]
                history.append({"role": "user", "content": user_text})
                if is_no_reply(user_text):
                    nos += 1

            msg_out = await self.llm.chat(http, self._node_msgs(node, payload) + history,
                                          self.nodes[node].get("functions") or [])
            choice = msg_out["choices"][0]["message"]
            content = (choice.get("content") or "").strip()
            tool_calls = choice.get("tool_calls") or []

            if len(tool_calls) > 1:
                v.append(f"multi_fn: {[c['function']['name'] for c in tool_calls]}")

            if tool_calls:
                fn_name = tool_calls[0]["function"]["name"]
                try:
                    fn_args = json.loads(tool_calls[0]["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    fn_args, _ = {}, v.append("fn_args_not_json")
                if content:
                    v.append(f"spoke_on_fn_turn: {content[:70]}…")
                if fn_name == "not_interested":
                    min_nos = p.get("min_nos", 2)
                    if nos < min_nos:
                        v.append(f"not_interested_on_first_no (nos={nos})")
                    reason_kw = p.get("reason_kw")
                    if reason_kw and reason_kw not in str(fn_args.get("drop_off_reason", "")):
                        v.append(f"drop_off_reason_lost: {fn_args.get('drop_off_reason')!r}")
                if fn_name == "app_return_committed":
                    if p.get("timeline_kw") and p["timeline_kw"] not in str(
                        fn_args.get("completion_timeline", "")
                    ):
                        v.append(f"timeline_mismatch: {fn_args.get('completion_timeline')!r}")
                    if str(fn_args.get("committed_step", "")).strip() in ("", "none"):
                        v.append("committed_step_missing")
                if fn_name == "user_busy" and p.get("callback_kw") and p[
                    "callback_kw"
                ] not in str(fn_args.get("callback_note", "")):
                    v.append(f"callback_note_lost: {fn_args.get('callback_note')!r}")
                if fn_name == "already_completed" and p.get("claimed_kw") and p[
                    "claimed_kw"
                ] not in str(fn_args.get("claimed_step", "")):
                    v.append(f"claimed_step_lost: {fn_args.get('claimed_step')!r}")
                if fn_name == "customer_stuck" and p.get("issue_kw") and p[
                    "issue_kw"
                ] not in str(fn_args.get("issue_description", "")):
                    v.append(f"issue_lost: {fn_args.get('issue_description')!r}")
                if p.get("unsupported_code") and str(
                    fn_args.get("unsupported_language_code", "")
                ) != p["unsupported_code"]:
                    v.append(
                        f"unsupported_code_wrong: {fn_args.get('unsupported_language_code')!r}"
                    )
                expect_ok = fn_name == p.get("expect") or fn_name in (p.get("expect_any") or [])
                if not expect_ok:
                    v.append(
                        f"function_mismatch: got {fn_name}, expected "
                        f"{p.get('expect') or p.get('expect_any')}"
                    )
                result["function"] = fn_name
                result["function_args"] = fn_args

                history.append({
                    "role": "assistant", "content": content or None,
                    "tool_calls": [{
                        "id": tool_calls[0]["id"], "type": "function",
                        "function": tool_calls[0]["function"],
                    }],
                })
                history.append({"role": "tool", "tool_call_id": tool_calls[0]["id"],
                                "content": "ok"})
                node = next(
                    (f.get("transition_to") for f in self.nodes[node].get("functions", [])
                     if f["function_name"] == fn_name and f.get("transition_to")),
                    node,
                )
                msg_out = await self.llm.chat(http, self._node_msgs(node, payload) + history, None)
                closing = (msg_out["choices"][0]["message"].get("content") or "").strip()
                want_en = bool(p.get("english"))
                pair = self.closing.get(fn_name)
                if pair:
                    norm = lambda s: s.replace("।", ".").replace("  ", " ").strip()
                    want = pair[1] if want_en else pair[0]
                    if norm(closing) != norm(want):
                        v.append(
                            f"closing_mismatch ({'en' if want_en else 'hi'}):\n"
                            f"  got      {closing[:90]}\n  expected {want[:90]}"
                        )
                if closing:
                    result["bot_phrases"].append(closing)
                    if self.use_tts and self.tts:
                        for s in split_sentences(closing):
                            await self.tts.speak_sentence(http, s)
                result["turns"].append({"user": user_text, "fn": fn_name,
                                        "args": fn_args, "closing": closing})
                break

            if content:
                check_bot(content, turn_idx)
                result["turns"].append({"user": user_text, "bot": content})
                history.append({"role": "assistant", "content": content})
                if self.use_tts and self.tts:
                    for s in split_sentences(content):
                        await self.tts.speak_sentence(http, s)
            else:
                v.append(f"empty_bot_turn after user: {user_text!r}")
            turn_idx += 1
        else:
            v.append("no_function_ending: persona script exhausted")

        if p.get("who_are_you") and not any(
            "प्रियंका बोल रही हूँ" in t for t in bot_utterances
        ):
            v.append("who_are_you_intro_missing")

        return result


LLM_CONFIGS = {
    "azure": {"provider": "azure", "temperature": 0.1, "max_tokens": 500},
    "grid": {
        "provider": "openai", "model": "deepseek",
        "endpoint": "https://grid.ai.juspay.net", "api_key_name": "GRID_API_KEY",
        "temperature": 0.1, "max_tokens": 500,
    },
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--llm", choices=["azure", "grid", "template"], default="template")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--persona")
    ap.add_argument("--report")
    args = ap.parse_args()

    tpl = json.loads(Path(args.template).read_text())
    llm_cfg = (
        tpl.get("configurations", {}).get("llm_configurations")
        if args.llm == "template"
        else LLM_CONFIGS[args.llm]
    )
    if not llm_cfg:
        print("no llm_configurations — pass --llm azure|grid", file=sys.stderr)
        return 2
    llm = LLMClient("azure" if llm_cfg.get("provider") == "azure" else "grid", llm_cfg)
    tts_cfg = dict(tpl["configurations"]["tts_configuration"])
    use_tts = not args.no_tts
    tts = TTSClient(tts_cfg) if use_tts else None
    runner = FlipkartRunner(tpl, llm, tts, use_tts)

    personas = [p for p in PERSONAS if not args.persona or p["name"] == args.persona]
    print(
        f"flipkart harness: llm={llm.kind} model={llm.model} temp={llm.temperature} "
        f"tts={'on' if use_tts else 'off'} voice={tts_cfg['voice_id']} "
        f"speed={tts_cfg.get('speed')} personas={len(personas)} runs={args.runs}"
    )
    report = {"llm": llm.kind, "model": llm.model, "template": args.template, "runs": []}
    async with httpx.AsyncClient() as http:
        if use_tts and tts:
            await tts.speak_greeting(http, runner.greeting)
        for _ in range(args.runs):
            for p in personas:
                t0 = time.perf_counter()
                res = await runner.run_persona(http, p)
                res["secs"] = round(time.perf_counter() - t0, 1)
                report["runs"].append(res)
                status = "OK " if not res["violations"] else "BAD"
                print(
                    f"[{status}] {p['name']:<22} fn={str(res['function']):<20} "
                    f"{res['secs']:>5}s turns={len(res['turns'])} viol={len(res['violations'])}"
                )
                for viol in res["violations"]:
                    print(f"       - {viol}")

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
        print(
            f"tts: unique_phrases={len(phrases)} requests={total_req} hits={hits} "
            f"misses={misses} hit_rate={hits / max(1, total_req) * 100:.0f}%"
        )
        if misses:
            print("uncached phrases (misses):")
            for ph, s in sorted(phrases.items(), key=lambda kv: -kv[1]["misses"]):
                if s["misses"]:
                    print(f"  [{s['misses']} miss/{s['hits']} hit] {ph[:80]}")
        report["tts"] = {
            "unique_phrases": len(phrases), "hits": hits, "misses": misses,
            "phrases": phrases,
        }
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"report: {args.report}")
    return 0 if total_viol == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
