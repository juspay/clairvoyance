#!/usr/bin/env python3
"""Build the tool_based Namma Yatri driver-not-active template (DB format) for qwen.

Base: nammayatri "ny-driver-not-active" (Kannada driver reactivation, 5 nodes).
The multi-node machine collapses into one tool_based node: the fixed TURN 2
pitch (earnings range + secret offer + ready question) is say_pitch; the
issue-node and closing-node lines ride on the outcome tools themselves
(driver_says_no SPEAKS the ask-issue line instead of transitioning; the
closing lines are the end_call says). ready_to_start values that the routing
already encodes are static-injected per function; only the ones that genuinely
vary stay as small enums. Kannada-only say blocks (default_language kn, no
language arg) — the smallest possible arg surface for qwen.

Output: qwen_harness/driver-not-active-toolbased.json
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "driver-not-active-toolbased.json"

TTS = {
    "provider": "elevenlabs",
    "voice_id": "iB2rIwm9cQCRGWoKDRtX",
    "model": "eleven_v3_conversational",
    "language": None,
    "speed": 1,
    "stability": None,
    "similarity_boost": None,
    "volume": None,
    "emotion": None,
    "pitch": None,
    "style_prompt": None,
    "enable_ssml_parsing": None,
    "enable_tts_caching": True,
}

QWEN_LLM = {
    "provider": "openai",
    "sdk": None,
    "model": "qwen3.8-27b-4bit",
    "region": None,
    "endpoint": "http://34.126.104.99:8000/v1",
    "api_key_name": "BREEZE_LLM_API_KEY",
    "temperature": 0.2,
    "max_tokens": 500,
    "thinking": None,
    "function_call_timeout_secs": None,
    "realtime": None,
    "tool_choice": "required",
    "prefill_system_prompt": True,
}

READY_ENUM_FULL = {
    "type": "string",
    "enum": ["YES", "NO", "NOT_SURE", "NO_ANSWER", "NOT_ASKED"],
    "description": "The driver's answer to the ready question "
    "'ಯೇನ್ ಸರ್, Rides ಮಾಡೋಕೆ ready na?'. YES = clearly agreed. NO = clearly "
    "refused after the pitch. NOT_SURE = vague/conditional. NO_ANSWER = "
    "question asked, silence. NOT_ASKED = pitch never delivered. Report "
    "what they actually said — never guess, never YES for politeness.",
}
DRIVER_NUMBER = {
    "type": "string",
    "description": "The driver's 10-digit number. ALWAYS pass "
    "{customer_mobile_number} exactly as it appears in your instructions — "
    "never ask the driver for it, never reformat it.",
}
QUERY = {
    "type": "string",
    "description": "One-line Kanglish summary of what the driver just said, "
    "e.g. 'Driver said houdu, willing to start rides' or 'Driver said "
    "pickup too far, not ready'.",
}
READY_WORDS = {
    "type": "string",
    "description": "A short direct quote of the driver's own reply to the "
    "ready question, in the language they used (e.g. 'ಹೌದು ಮಾಡ್ತೀನಿ'). Empty "
    "string if it was never asked or never answered. Never paraphrase or "
    "invent.",
}
REASON_ENUM = {
    "type": "string",
    "enum": [
        "PICKUP_DISTANCE",
        "FARE_LOW",
        "CUSTOMER_LOCATION",
        "APP_OR_TECHNICAL",
        "NETWORK_ISSUE",
        "REQUESTS_NOT_VISIBLE",
        "VEHICLE_ISSUE",
        "DRIVER_BUSY",
        "PAYMENT_CONCERN",
        "PERSONAL_REASON",
        "OTHER",
    ],
    "description": "Why the driver is not ready: PICKUP_DISTANCE = pickup "
    "too far. FARE_LOW = fare/earnings low. CUSTOMER_LOCATION = pickup/drop "
    "location problem. APP_OR_TECHNICAL = app crash/login/hang. "
    "NETWORK_ISSUE = internet/GPS. REQUESTS_NOT_VISIBLE = ride requests "
    "never arrive. VEHICLE_ISSUE = breakdown/service/fuel/no vehicle. "
    "DRIVER_BUSY = other work/break. PAYMENT_CONCERN = dues/subscription/"
    "settlement. PERSONAL_REASON = health/family. OTHER = anything else — "
    "never guess a specific category.",
}
REASON_DETAILS = {
    "type": "string",
    "description": "Structured detail, exactly this shape, only what the "
    "driver actually said (skip a part they did not mention): "
    "'WHAT: <what is stopping them> | WHEN: <since when> | TRIED: <what "
    "they tried> | DRIVER_WORDS: <short direct quote>'. Never invent.",
}

PITCH = (
    "ನಮ್ಮ partnersu ಈಗ ಒಂದು ದಿನಕ್ಕೆ ನಾಲ್ಕು ಸಾವಿರ ಇಂದ ಐದು ಸಾವಿರ ರೂಪಾಯಿ ವರೆಗೆ "
    "ದುಡಿಮೆ ಮಾಡ್ತಿದ್ದಾರೆ. ನೀವು ಕೂಡ start ಮಾಡಿ ಸರ್. ನಾನು ನಿಮಗೆ ಒಂದು secret "
    "ಹೇಳ್ತೀನಿ, ಇದು ಬೇರೆ ಯಾರಿಗೂ ಹೇಳ್ಬೇಡಿ. ಒಂದು ವಾರ free subscription, "
    "unlimited rides, ನೀವು ಯಾವುದೇ charges pay ಮಾಡಂಗಿಲ್ಲ. ನಿಮಗೆ ಏನಾದ್ರೂ "
    "issue ಇದ್ರೆ ನಮಗೆ call ಮಾಡಿ. But ಈ offer ಮಾತ್ರ ಮಿಸ್ ಮಾಡ್ಕೋಬೇಡಿ. ಯೇನ್ ಸರ್, "
    "Rides ಮಾಡೋಕೆ ready na?"
)
CLOSING_PLAIN = (
    "ಈಗ್ಲೇ online ಹೋಗಿ, rides start ಮಾಡಿ. ಒಳ್ಳೆ ದುಡಿಮೆ ಮಾಡ್ಕೊಳ್ಳಿ ಸರ್, Thank "
    "you, ನಿಮ್ಮ ದಿನ ಶುಭವಾಗಲಿ!"
)
CLOSING_ISSUE = (
    "ಸರಿ ಸರ್, ನಿಮ್ಮ issue ಅರ್ಥ ಆಯ್ತು. Thank you sir, ನಿಮ್ಮ issue note "
    "ಮಾಡ್ತಿದ್ದೀನಿ. But offer ಮಿಸ್ ಮಾಡ್ಬೇಡಿ — ಈಗ್ಲೇ online ಹೋಗಿ, rides start "
    "ಮಾಡಿ. ಒಳ್ಳೆ ದುಡಿಮೆ ಮಾಡ್ಕೊಳ್ಳಿ ಸರ್. Thank you, ನಿಮ್ಮ ದಿನ ಶುಭವಾಗಲಿ!"
)
CLOSING_BUSY = "ಪರವಾಗಿಲ್ಲ ಸರ್, ನಿಮ್ಮ ಸಮಯಕ್ಕೆ ಧನ್ಯವಾದ. ನಿಮ್ಮ ದಿನ ಶುಭವಾಗಲಿ!"
ASK_ISSUE = (
    "ಸರ್, ಯಾಕೆ ready ಇಲ್ಲ, ಏನು issue ಇದೆ ಹೇಳಿ ಸರ್. ನಾವು note ಮಾಡ್ಕೊಂಡು help "
    "ಮಾಡೋಕೆ try ಮಾಡ್ತೀವಿ."
)
STILL_THERE = "ಸರ್, ನೀವು line ನಲ್ಲಿ ಇದ್ದೀರಾ?"


def say(text: str, *, end_call: bool = False) -> dict:
    block: dict = {
        "utterances": {"kn": [text]},
        "phrasing": "first",
        "default_language": "kn",
    }
    if end_call:
        block["end_call"] = True
    return block


def tool(
    name: str,
    description: str,
    text: str,
    props: dict | None = None,
    required: list | None = None,
    hooks: list | None = None,
    end_call: bool = False,
) -> dict:
    return {
        "function_name": name,
        "description": description,
        "properties": props or {},
        "required": required or [],
        "transition_to": None,
        "hooks": hooks or [],
        "say": say(text, end_call=end_call),
    }


def outcome(
    name: str,
    description: str,
    text: str,
    outcome_value: str,
    props: dict | None = None,
    required: list | None = None,
    static: dict | None = None,
    end_call: bool = True,
) -> dict:
    """Outcome tool with update_outcome_in_database hook.

    ``static`` maps field -> fixed value (source static); every other
    required prop is sourced from the llm.
    """
    expected: dict = {"outcome": {"value": outcome_value, "source": "static"}}
    for field, value in (static or {}).items():
        expected[field] = {"value": value, "source": "static"}
    for r in required or []:
        expected[r] = {"source": "llm"}
    return tool(
        name,
        description,
        text,
        props=props,
        required=required,
        hooks=[{"name": "update_outcome_in_database", "expected_fields": expected}],
        end_call=end_call,
    )


ROLE = """# NAMMA YATRI DRIVER REACTIVATION — TOOL-BASED (KANNADA)

ನೀವು Namma Yatri ಇಂದ ಒಬ್ಬ driver ಗೆ call ಮಾಡ್ತಿರುವ support person. ಈ driver app ನಲ್ಲಿ active ಇಲ್ಲ; ಉದ್ದೇಶ ಅವರು rides ತಗೊಳ್ಳೋಕೆ ಮತ್ತೆ start ಮಾಡೋದು. ಸಾಮಾನ್ಯ driver-support person ರೀತಿ ಮಾತಾಡಿ — robot ಅಲ್ಲ, ನಿಮ್ಮ ಹೆಸರು ಎಂದಿಗೂ ಹೇಳಬೇಡಿ. Day-to-day Kannada + English mix ಮಾತ್ರ; Hindi ಎಂದಿಗೂ ಬೇಡ.

Greeting ಆಗಲೇ ಆಗಿದೆ — ಹೆಸರು, 'ಮರೆತುಬಿಟ್ರಾ', app ಪರಿಚಯ, 'ಈಗ ಮಾತಾಡೋಕೆ ಆಗುತ್ತಾ?'. ಅದರ ಯಾವುದೇ ಸಾಲನ್ನ ಪುನಃ ಎಂದಿಗೂ ಹೇಳಬೇಡಿ.

ನಿಮ್ಮ tools ಗಳೇ ನಿಮ್ಮ ಮಾತು — ನೀವು ಬರೀಯುವುದಿಲ್ಲ. ಒಂದು turn ಗೆ ಒಂದೇ tool.

# THE MOST IMPORTANT GATE — PERMISSION vs READY
Greeting ನ ಕೊನೆಯ ಪ್ರಶ್ನೆ 'ಈಗ ಮಾತಾಡೋಕೆ ಆಗುತ್ತಾ?' ಗೆ ಬಂದ ಒಪ್ಪಿಗೆ ('ಹೌದು','ಸರಿ','ಆಯ್ತು','ಹೇಳಿ','ok','ಮ್ಮ್','ಹಾಂ') ಎಂದರೆ 'ಮಾತಾಡೋಕೆ ಆಗುತ್ತಾ' ಅಷ್ಟೇ — ಅದು ready ಉತ್ತರ ಅಲ್ಲ. say_pitch ಆಗುವವರೆಗೂ driver_says_yes ಮತ್ತು driver_says_no ಎಂದಿಗೂ call ಮಾಡಬೇಡಿ.

# CALL SHAPE
1. Driver ಒಪ್ಪಿದರೆ → say_pitch (ದುಡಿಮೆ range + secret offer + ready ಪ್ರಶ್ನೆ — ಒಂದೇ fixed ಸಾಲು). 'ಯಾರು ಮಾತಾಡ್ತಿದ್ದೀರಾ?' → say_reintroduce_and_pitch. 'ಏನು ವಿಷಯ?' → say_pitch (ಒಪ್ಪಿಗೆ ಎಂದು ತೆಗೆದುಕೊಳ್ಳಿ).
2. say_pitch ನ ನಂತರ: ಒಪ್ಪಿಗೆ ('ಹೌದು','ಮಾಡ್ತೀನಿ','ಸರಿ','ok','ಮ್ಮ್','ಹಾಂ') → driver_says_yes — ಮೊದಲು ಏನೂ ಹೇಳಬೇಡಿ, ತಕ್ಷಣ call. Repeat ಕೇಳಿದರೆ ('ಏನಂದ್ರಿ?','offer ಏನು?','ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ') → say_repeat_offer; ದುಡಿಮೆ ಕೇಳಿದರೆ ('ಎಷ್ಟು ಸಂಬಳ?','ಎಷ್ಟು ಆಗುತ್ತೆ?') → say_repeat_earnings. ಅವರ ಮುಂದಿನ ಉತ್ತರದಿಂದ ಮಾತ್ರ route ಮಾಡಿ.
3. ಇಲ್ಲ / ಆಗಲ್ಲ / ಗೊತ್ತಿಲ್ಲ / ನೋಡೋಣ → driver_says_no — ಅದೇ turn ನಲ್ಲಿ ಕಾರಣ ಕೇಳೋ ಸಾಲು ಹೊರಡುತ್ತದೆ. ಸಿಟ್ಟಾದ driver pitch ಗೆ ಮೊದಲೇ ಸಮಸ್ಯೆ ವಿವರಿಸಿದರೆ → driver_says_no, ready_to_start=NOT_ASKED.
4. driver_says_no ನ ನಂತರ: ಕಾರಣ ಸಿಕ್ಕರೆ → driver_shares_issue (reason_category + reason_details). 'ಏನೂ ಇಲ್ಲ / ಸುಮ್ಮನೆ' → driver_no_issue. ಹೇಳಲು ನಿರಾಕರಿಸಿದರೆ ('ಬೇಡ','ಇಟ್ಟುಬಿಡಿ') → driver_declines_to_share.
5. Busy / driving / 'ಆಮೇಲೆ call ಮಾಡಿ' → driver_busy_no_time — pitch ಹೇಳದೆ, ಒತ್ತಾಯ ಇಲ್ಲದೆ, ತಕ್ಷಣ.
6. Driver ಬೇರೆ ವಿಷಯ ಎತ್ತಿದರೆ (dues, plan, insurance, account): argue ಮಾಡಬೇಡಿ, ಭರವಸೆ ಕೊಡಬೇಡಿ — ಅದು ready ಇಲ್ಲ ಎಂಬ ಕಾರಣವಾಗಿ driver_says_no → driver_shares_issue ದಾರಿಯಲ್ಲಿ note ಆಗುತ್ತದೆ.

# FIELD RULES
- driver_number ಗೆ ಯಾವಾಗಲೂ {customer_mobile_number} ಅನ್ನೇ ಕಳಿಸಿ — driver ಹತ್ರ ಕೇಳಬೇಡಿ.
- ಇತರ ವಿಷಯ / 'ನೀವು robot ಆ?' / 'ನನ್ನ number ಯಾರು ಕೊಟ್ಟರು?' → say_off_topic — ಉತ್ತರಿಸಬೇಡಿ, ready ಪ್ರಶ್ನೆಗೆ ಹಿಂತಿರುಗಿಸಿ.
- ready_response_words: ready ಪ್ರಶ್ನೆಗೆ driver ಹೇಳಿದ ಮಾತಿನ ಚಿಕ್ಕ quote, ಇದ್ದ ಹಾಗೆ; ಕೇಳಿಲ್ಲ / ಉತ್ತರ ಬರಲಿಲ್ಲ ಎಂದರೆ ಖಾಲಿ ಹಾಕಿ. ತಿದ್ದಬೇಡಿ, invent ಮಾಡಬೇಡಿ.
- Fare / incentive / timeline ಭರವಸೆ ಎಂದಿಗೂ ಕೊಡಬೇಡಿ. ಒತ್ತಾಯ ಬೇಡ, lecture ಬೇಡ. Driver 'ಇಲ್ಲ' ಅಂದ ಮೇಲೆ ಒಂದೇ ಸಲ ಕಾರಣ ಕೇಳಿ — ಎರಡನೇ ಸಲ ಇಲ್ಲ."""

TOOLS = [
    # ---- speech tools -------------------------------------------------
    tool(
        "say_pitch",
        "The driver agreed to talk (their answer to the greeting's "
        "permission question), or asked 'ಏನು ವಿಷಯ?'. THE fixed pitch — "
        "earnings range, secret offer, and the ready question, in one "
        "line. Before this, driver_says_yes / driver_says_no are "
        "FORBIDDEN.",
        PITCH,
    ),
    tool(
        "say_reintroduce_and_pitch",
        "The driver asked 'ಯಾರು ಮಾತಾಡ್ತಿದ್ದೀರಾ?' / 'ಎಲ್ಲಿಂದ call?' — they did not "
        "hear the greeting clearly. One-line reintroduction, then the full "
        "pitch in the same breath.",
        "ನಾನು ನಮ್ಮ ಯಾತ್ರಿ ಕಡೆ ಇಂದ call ಮಾಡ್ತಿದ್ದೀನಿ ಸರ್. " + PITCH,
    ),
    tool(
        "say_repeat_offer",
        "After the pitch the driver asked to repeat the offer/secret "
        "('ಏನಂದ್ರಿ?', 'offer ಏನು?', 'ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ', 'ಎಷ್ಟು ದಿನ free?'). Repeats "
        "the offer and re-asks the ready question once. Never say 'ಆಗಲೇ "
        "ಹೇಳಿದೆ'.",
        "ಒಂದು ವಾರ free subscription ಸರ್, unlimited rides. ನೀವು ಯಾವುದೇ charges "
        "pay ಮಾಡಂಗಿಲ್ಲ. ಯೇನ್ ಸರ್, Rides ಮಾಡೋಕೆ ready na?",
    ),
    tool(
        "say_repeat_earnings",
        "After the pitch the driver asked about earnings ('ದುಡಿಮೆ ಎಷ್ಟು?', "
        "'ಎಷ್ಟು ಆಗುತ್ತೆ ಅಂದ್ರಿ?'). Repeats the earnings range honestly and "
        "re-asks the ready question once.",
        "ನಮ್ಮ partnersu ಒಂದು ದಿನಕ್ಕೆ ನಾಲ್ಕು ಸಾವಿರ ಇಂದ ಐದು ಸಾವಿರ ರೂಪಾಯಿ ವರೆಗೆ "
        "ದುಡಿಮೆ ಮಾಡ್ತಿದ್ದಾರೆ ಸರ್. ಇದು ride ಮತ್ತು area ಮೇಲೆ ಬದಲಾಗುತ್ತೆ. ಯೇನ್ ಸರ್, "
        "Rides ಮಾಡೋಕೆ ready na?",
    ),
    tool(
        "say_off_topic",
        "The driver went to something with NO relation to this call — "
        "politics, cricket, 'ನೀವು robot ಆ?', 'ನನ್ನ number ಯಾರು ಕೊಟ್ಟರು?'. One "
        "polite redirect back to the ready question. Never play along with "
        "the new topic and never share any data.",
        "ಸರ್, ಅದು ನನಗೆ ಗೊತ್ತಿಲ್ಲ ಸರ್. ನಾನು ಬಸ್ಸ್ rides start ಮಾಡೋದ್ರಲ್ಲಿ help "
        "ಮಾಡೋಕೆ call ಮಾಡ್ತಿದ್ದೀನಿ. ಯೇನ್ ಸರ್, Rides ಮಾಡೋಕೆ ready na?",
    ),
    # ---- outcome tools ------------------------------------------------
    outcome(
        "driver_says_yes",
        "HARD PRECONDITION: say_pitch (or say_reintroduce_and_pitch) was "
        "ALREADY called in this call. The driver answered the ready "
        "question with any agreement — 'ಹೌದು', 'ಮಾಡ್ತೀನಿ', 'ready ಇದ್ದೀನಿ', "
        "'ok', or just an agreeing 'ಮ್ಮ್'/'ಹಾಂ'. Speak NOTHING extra — this "
        "speaks the closing and ends the call.",
        CLOSING_PLAIN,
        "DRIVER_READY_TO_START",
        props={
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["query", "ready_response_words", "driver_number"],
        static={"ready_to_start": "YES", "reason_category": "NO_REASON"},
    ),
    outcome(
        "driver_says_no",
        "HARD PRECONDITION: the pitch was already spoken — the ONLY "
        "exception is an angry driver describing a problem BEFORE the "
        "pitch (then ready_to_start = NOT_ASKED). Any non-agreement to the "
        "ready question: 'ಇಲ್ಲ', 'ಆಗಲ್ಲ', 'time ಇಲ್ಲ', 'ಗೊತ್ತಿಲ್ಲ', 'ನೋಡೋಣ'. "
        "If the driver did not hear the why question and asks you to repeat "
        "('ಏನು? ಇನ್ನೊಮ್ಮೆ ಕೇಳಿ'), call this AGAIN to re-speak it. Speaks the "
        "why question; their next reply routes to "
        "driver_shares_issue / driver_no_issue / driver_declines_to_share.",
        ASK_ISSUE,
        "DRIVER_NOT_READY",
        props={
            "ready_to_start": {
                "type": "string",
                "enum": ["NO", "NOT_SURE", "NOT_ASKED"],
                "description": "NO = clear refusal after the pitch. "
                "NOT_SURE = vague/conditional ('ಗೊತ್ತಿಲ್ಲ', 'ನೋಡೋಣ'). "
                "NOT_ASKED = ONLY for the angry-driver-before-pitch "
                "exception.",
            },
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["ready_to_start", "query", "ready_response_words", "driver_number"],
        end_call=False,
    ),
    outcome(
        "driver_busy_no_time",
        "The driver said this is not a good time — busy, driving, on a "
        "ride, 'ಆಮೇಲೆ call ಮಾಡಿ' — answering the greeting's permission "
        "question. The pitch is NEVER spoken on this path. Speaks the "
        "polite no-pressure closing and ends the call.",
        CLOSING_BUSY,
        "DRIVER_BUSY_NO_TIME",
        props={
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["query", "ready_response_words", "driver_number"],
        static={"ready_to_start": "NOT_ASKED", "reason_category": "NO_REASON"},
    ),
    outcome(
        "greeting_no_response",
        "The driver stayed silent (about 3s) after the greeting's "
        "permission question, before any pitch. Speaks the line-check "
        "question. If they then reply → driver_responded; silence again → "
        "still_no_response.",
        STILL_THERE,
        "GREETING_NO_RESPONSE",
        props={
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["query", "ready_response_words", "driver_number"],
        static={"ready_to_start": "NOT_ASKED", "reason_category": "NO_REASON"},
        end_call=False,
    ),
    outcome(
        "pitch_no_response",
        "The driver stayed silent (about 3s) after the ready question in "
        "say_pitch. Speaks the line-check question. If they then reply → "
        "driver_responded; silence again → still_no_response.",
        STILL_THERE,
        "PITCH_NO_RESPONSE",
        props={
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["query", "ready_response_words", "driver_number"],
        static={"ready_to_start": "NO_ANSWER", "reason_category": "NO_REASON"},
        end_call=False,
    ),
    outcome(
        "driver_responded",
        "After the line-check question the driver responded in any way, "
        "however small. If they agreed to rides → driver_says_yes is the "
        "better tool; use this for anything else. Speaks the plain closing "
        "and ends the call.",
        CLOSING_PLAIN,
        "DRIVER_RESPONDED_AFTER_CHECK",
        props={
            "ready_to_start": {
                "type": "string",
                "enum": ["NOT_ASKED", "NO_ANSWER"],
                "description": "Carry forward: NOT_ASKED if the silence "
                "came before the pitch, NO_ANSWER if it came after the "
                "ready question.",
            },
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["ready_to_start", "query", "ready_response_words", "driver_number"],
        static={"reason_category": "NO_REASON"},
    ),
    outcome(
        "still_no_response",
        "The driver stayed silent AGAIN after the line-check question. Do "
        "not ask a third time. Speaks the plain closing and ends the call.",
        CLOSING_PLAIN,
        "DRIVER_UNRESPONSIVE",
        props={
            "ready_to_start": {
                "type": "string",
                "enum": ["NOT_ASKED", "NO_ANSWER"],
                "description": "Carry forward: NOT_ASKED if silence started "
                "before the pitch, NO_ANSWER if after the ready question.",
            },
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["ready_to_start", "query", "ready_response_words", "driver_number"],
        static={"reason_category": "NO_REASON"},
    ),
    outcome(
        "driver_shares_issue",
        "After driver_says_no's why question the driver gave ANY reason — "
        "pickup distance, fare, app, network, vehicle, dues, health, "
        "anything. Acknowledge is built into the closing. Categorise "
        "honestly; OTHER when unsure.",
        CLOSING_ISSUE,
        "REASON_GIVEN",
        props={
            "reason_category": REASON_ENUM,
            "reason_details": REASON_DETAILS,
            "ready_to_start": {
                "type": "string",
                "enum": ["NO", "NOT_SURE", "NOT_ASKED"],
                "description": "Same value you passed on driver_says_no — "
                "do not recalculate.",
            },
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=[
            "reason_category",
            "reason_details",
            "ready_to_start",
            "query",
            "ready_response_words",
            "driver_number",
        ],
    ),
    outcome(
        "driver_no_issue",
        "After the why question the driver said there is no particular "
        "issue ('ಏನೂ ಇಲ್ಲ', 'ಸುಮ್ಮನೆ', 'ಗೊತ್ತಿಲ್ಲ'). No pushing. Speaks the "
        "plain closing and ends the call.",
        CLOSING_PLAIN,
        "NO_ISSUE_REPORTED",
        props={
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["query", "ready_response_words", "driver_number"],
        static={"ready_to_start": "NO", "reason_category": "NO_REASON"},
    ),
    outcome(
        "driver_declines_to_share",
        "After the why question the driver CLEARLY refused to explain or "
        "wants to hang up ('ಬೇಡ', 'ಈಗ ಬೇಡ', 'ಇಟ್ಟುಬಿಡಿ', 'ಆಮೇಲೆ ಮಾತಾಡ್ತೀನಿ'). "
        "A repeat request — 'ಏನು? ಇನ್ನೊಮ್ಮೆ ಕೇಳಿ', 'ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ' — is NOT "
        "refusal: call driver_says_no again to re-speak the question. "
        "Speaks the plain closing and ends the call.",
        CLOSING_PLAIN,
        "NO_DETAILS_SHARED",
        props={
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["query", "ready_response_words", "driver_number"],
        static={"ready_to_start": "NO", "reason_category": "OTHER"},
    ),
    outcome(
        "issue_no_response",
        "After the why question the driver stayed silent (about 3s). Do "
        "not ask again. Speaks the plain closing and ends the call.",
        CLOSING_PLAIN,
        "ISSUE_NO_RESPONSE",
        props={
            "query": QUERY,
            "ready_response_words": READY_WORDS,
            "driver_number": DRIVER_NUMBER,
        },
        required=["query", "ready_response_words", "driver_number"],
        static={"ready_to_start": "NO", "reason_category": "NO_REASON"},
    ),
]

template = {
    "id": "9e3f1c4a-6d5b-4a0c-9b82-3f4a5b6c7d82",
    "reseller_id": "rnr",
    "merchant_id": "test-shopify",
    "name": "ny-driver-not-active-toolbased",
    "is_active": True,
    "supported_channels": ["voice"],
    "secrets": None,
    "telephony_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "outbound_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "description": (
        "tool_based conversion of the Namma Yatri driver-not-active "
        "reactivation flow (Kannada), tuned for qwen3.8-27b-4bit: the 5-node "
        "machine collapses into one node — the fixed pitch, repeat lines and "
        "closings are say blocks, driver_says_no SPEAKS the why question "
        "instead of transitioning, and ready_to_start values that the "
        "routing already encodes are static-injected per function. Kannada-"
        "only utterances, no language arg at all."
    ),
    "expected_payload_schema": {
        "driver_name": {"type": "string", "optional": False},
        "customer_mobile_number": {
            "type": "string",
            "function": "extract_10_digit_mobile",
            "optional": False,
        },
    },
    "expected_callback_response_schema": {
        "query": {"type": "string", "optional": True},
        "outcome": {"type": "string"},
        "driver_number": {"type": "string", "optional": True},
        "ready_to_start": {"type": "string", "optional": True},
        "reason_details": {"type": "string", "optional": True},
        "reason_category": {"type": "string", "optional": True},
        "ready_response_words": {"type": "string", "optional": True},
    },
    "configurations": {
        "llm_configurations": QWEN_LLM,
        "tts_configuration": TTS,
        "stt_configuration": {
            "provider": "soniox",
            "language": "kn-IN",
            "payload_based_language_selection": False,
            "turn_detection": "stt_native",
            "user_speech_timeout": 0,
        },
        "initial_greeting": (
            "Namsakar {driver_name} ಸರ್, yen ಸರ್ ನಮ್ಮನ್ನ ಮರೆತುಬಿಟ್ರಾ? Naan ಸರ್, "
            "ನಮ್ಮ ಯಾತ್ರಿ App ನಿಮಗೋಸ್ಕರ ಮಾಡಿರೋದು App. ನಿಮ್ಮ ಹತ್ರ ಎರಡು ನಿಮಿಷ "
            "ಮಾತಾಡ್ಬೇಕಿತ್ತು. ಈಗ ಮಾತಾಡೋಕೆ ಆಗುತ್ತಾ?"
        ),
        "user_idle_configuration": {
            "enabled": True,
            "timeout": 10,
            "idle_message": (
                "Driver has gone quiet. Call greeting_no_response if the "
                "pitch has not been spoken yet, pitch_no_response if it "
                "has."
            ),
            "max_retries": 3,
        },
        "dial_tone": True,
        "enable_background_sound": False,
        "background_sound_volume": 2,
        "enable_text_input": True,
        "enable_inbound": False,
        "noise_filter": {
            "enable": True,
            "provider": "aic",
            "model": "noise_cancellation",
        },
        "keyword_filter": {
            "enabled": True,
            "keywords": [
                "hello",
                "hi",
                "yes",
                "ok",
                "okay",
                "hmm",
                "hm",
                "yeah",
                "haan",
                "ha",
                "hu",
                "hoo",
                "heli",
                "helri",
                "matadi",
                "enu",
                "yen",
                "yenu",
                "acha",
                "accha",
                "oho",
                "oh",
                "howdu",
                "haudu",
                "houdu",
                "howda",
                "hauda",
                "sari",
                "aythu",
                "aytu",
                "aaythu",
                "ayth",
                "ಹೌದು",
                "ಹೌದಾ",
                "ಹೌದ್",
                "ಸರಿ",
                "ಆಯ್ತು",
                "ಆಯ್ತ್",
                "ಆಯಿತು",
                "ಓಕೆ",
                "ಒಕೆ",
                "ಹ್ಮ್",
                "ಹ್ಮ್ಮ್",
                "ಹೂಂ",
                "ಹಾಂ",
                "ಹಾ",
                "ಹೇಳಿ",
                "ಹೇಳ್ರಿ",
                "ಮಾತಾಡಿ",
                "ಏನು",
                "ಏನ್",
                "ಅಚ್ಚಾ",
                "ಓಹ್",
                "ಓ",
                "ಅದೇ",
                "ಅದಾ",
            ],
            "match_type": "exact",
        },
        "observers": None,
    },
    "flow": {
        "mode": "tool_based",
        "initial_node": "initial",
        "nodes": [
            {
                "node_name": "initial",
                "pre_actions": [
                    {"type": "function", "handler": "mute_stt", "args": {"duration": 4}}
                ],
                "post_actions": [{"type": "function", "handler": "unmute_stt"}],
                "role_messages": [{"role": "system", "content": ROLE}],
                "task_messages": [
                    {
                        "role": "developer",
                        "content": (
                            "Driver: {driver_name}, number "
                            "{customer_mobile_number} (use exactly this for "
                            "driver_number). Stage machine: permission -> "
                            "pitch -> ready answer -> (yes | no -> reason) "
                            "-> closing. One tool per turn."
                        ),
                    }
                ],
                "functions": TOOLS,
            }
        ],
    },
}

OUT.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n")
print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(TOOLS)} tools)")
