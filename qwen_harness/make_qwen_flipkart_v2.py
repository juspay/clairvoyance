#!/usr/bin/env python3
"""Build the bilingual tool_based Flipkart EMI dropoff-recovery template.

Standalone conversion of flipkart-emi-dropoff-recovery (prod, attached) into
tool_based mode, latency-optimized the same way as the redbus template:

- Per-language split functions: every speech concept is a ``<stem>_hi`` /
  ``<stem>_en`` pair — the name alone determines the spoken line AND the
  language, so early-fire TTS needs no arguments at all.
- SHORT names with DISTINCT first letters across all 16 stems
  (offer again why nocost loan tenure down safe quick reason guide here
  unclear misc bot cancel): the name decode gates early-fire, and the first
  letter is unique so even single-character prefix matching is unambiguous.
- Exactly ONE utterance per function per language — no variant lists, the
  spoken line is deterministic and therefore cacheable in DragonTTS.
- Outcome tools stay bilingual single functions (they never early-fire, so
  the ``language`` argument is safe there) and carry the prod
  update_outcome_in_database hooks.
- Extra miscellaneous phrases the original prompt handled as prose:
  off-topic redirect ("I can only help with your Flipkart EMI process"),
  are-you-a-bot, auto-pay/cancel, safety/OTP, idle, unclear, fragment.

Output: qwen_harness/flipkart-emi-dropoff-recovery-toolbased.json
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "flipkart-emi-dropoff-recovery-toolbased.json"

QWEN_LLM = {
    "provider": "openai",
    "sdk": None,
    "model": "qwen3.8-27b-4bit",
    "region": None,
    "endpoint": "http://34.126.104.99:8000/v1",
    "api_key_name": "BREEZE_LLM_API_KEY",
    "temperature": 0.2,
    "max_tokens": 500,
    "tool_choice": "required",
    "thinking": {"enabled": False},
    "function_call_timeout_secs": None,
    "prefill_system_prompt": True,
    "realtime": None,
}


def say(lines: dict, *, end_call: bool = False) -> dict:
    block: dict = {
        "utterances": lines,
        "phrasing": "first",
        "default_language": "hi",
    }
    if end_call:
        block["end_call"] = True
    return block


def speech_pair(stem: str, description: str, hi: str, en: str) -> list[dict]:
    """One _hi / _en function pair speaking the same line in both languages."""
    return [
        {
            "function_name": f"{stem}_hi",
            "description": description,
            "properties": {},
            "required": [],
            "transition_to": None,
            "hooks": [],
            "say": say({"hi": [hi]}),
        },
        {
            "function_name": f"{stem}_en",
            "description": description,
            "properties": {},
            "required": [],
            "transition_to": None,
            "hooks": [],
            "say": say({"en": [en]}),
        },
    ]


def outcome(
    name: str,
    description: str,
    hi: str,
    en: str,
    outcome_value: str,
    llm_fields: list[tuple[str, str]],
) -> dict:
    properties: dict = {
        "language": {
            "type": "string",
            "enum": ["hi", "en"],
            "description": "The language the customer is speaking right now.",
        }
    }
    expected_fields = {
        "outcome": {"value": outcome_value, "source": "static"},
        "language": {"source": "llm"},
    }
    required = ["language"]
    for field, desc in llm_fields:
        properties[field] = {"type": "string", "description": desc}
        expected_fields[field] = {"source": "llm"}
        required.append(field)
    return {
        "function_name": name,
        "description": description,
        "properties": properties,
        "required": required,
        "transition_to": None,
        "hooks": [
            {
                "name": "update_outcome_in_database",
                "expected_fields": expected_fields,
            }
        ],
        "say": say({"hi": [hi], "en": [en]}, end_call=True),
    }


# ---------------------------------------------------------------------------
# Speech phrases — exactly one dialogue per function per language.
# Payload placeholders ({product_name} etc.) resolve from the lead payload,
# so early-fire still fires (no LLM-argument placeholders anywhere).
# ---------------------------------------------------------------------------

PHRASES = {
    "offer": (
        "The hook, the FIRST line after the greeting: what they were buying, "
        "the approved EMI offer, what is left, and the app-guidance question. "
        "Also the line to re-speak (in the other language) right after a "
        "language switch.",
        "जी. आपने {product_name} के लिए Flipkart EMI पर checkout शुरू किया था. {lender} "
        "की तरफ से आपका EMI offer approved है. बस {pending_steps} बाकी है — दो मिनट का "
        "काम. क्या मैं आपको अभी app में guide कर दूँ?",
        "You started checkout with Flipkart EMI for {product_name}. Your EMI "
        "offer from {lender} is already approved. Only {pending_steps} is left "
        "— a two-minute job. Shall I guide you through it in the app right now?",
    ),
    "again": (
        "The short re-ask of the app-guidance question, after answering any "
        "question or playing any card.",
        "तो क्या मैं आपको अभी app में guide कर दूँ? दो मिनट में हो जाएगा.",
        "So, shall I guide you through it in the app now? It takes two minutes.",
    ),
    "why": (
        "They asked who is calling or why you are calling.",
        "जी, मैं Flipkart EMI की तरफ से call कर रही हूँ. आपकी {product_name} की purchase "
        "अधूरी रह गई है — उसे पूरा करने में मदद करने के लिए call किया है.",
        "I am calling from Flipkart EMI. Your purchase of {product_name} was "
        "left incomplete — this call is to help you finish it.",
    ),
    "nocost": (
        "Card: they worry about interest, extra charge, hidden fees, or not "
        "having a credit card. Only while no-cost EMI applies.",
        "आपको card की ज़रूरत भी नहीं — {no_cost_emi_tenures} तक no-cost EMI already है. "
        "मतलब सिर्फ product का price, कोई extra interest नहीं.",
        "You do not even need a card — no-cost EMI is already available for "
        "{no_cost_emi_tenures}. That means only the product price, no extra "
        "interest.",
    ),
    "loan": (
        "Card: money is short, budget questions, how much loan, how much they "
        "will get.",
        "आपके लिए {approved_loan_amount} का loan इस purchase के लिए approved है, और आपकी "
        "credit limit {credit_limit} है. Approval का कोई इंतज़ार नहीं.",
        "A loan of {approved_loan_amount} is already approved for this "
        "purchase, and your credit limit is {credit_limit}. No waiting for "
        "approval.",
    ),
    "tenure": (
        "Card: they ask about tenures, months, monthly EMI amount or interest.",
        "आपके पास {applicable_tenures} की tenure options हैं. Exact monthly EMI आपको app "
        "में plan select करते ही दिख जाएगी.",
        "You have tenure options of {applicable_tenures}. The exact monthly EMI "
        "will show in the app as soon as you select a plan.",
    ),
    "down": (
        "Card: they ask about the downpayment.",
        "Downpayment सिर्फ {downpayment_amount} का है. वो होते ही order confirm हो जाता "
        "है.",
        "The downpayment is only {downpayment_amount}. The order is confirmed "
        "as soon as it is paid.",
    ),
    "safe": (
        "Card: they doubt the call, fear fraud, ask which lender or whether it "
        "is safe, or start reading out an OTP/PIN/card detail.",
        "जी, ये loan {lender} की तरफ से है और पूरा process Flipkart app के अंदर ही होता "
        "है. मैं आपसे कोई OTP, PIN या payment details कभी नहीं माँगूँगी.",
        "This loan is from {lender} and the entire process happens inside the "
        "Flipkart app. I will never ask you for an OTP, PIN, or payment "
        "details.",
    ),
    "quick": (
        "Card: no time right now, worried it is a long process, or will do it "
        "much later. This is a worry about the PROCESS, not the busy outcome "
        "(busy is only driving / meeting / cannot talk now).",
        "जो हो चुका है वो save है, दोबारा कुछ नहीं करना. बस {pending_steps} बाकी है — दो "
        "मिनट का काम.",
        "Whatever is already done is saved — nothing to redo. Only "
        "{pending_steps} is left, a two-minute job.",
    ),
    "reason": (
        "Their FIRST no or hesitation: ask the reason, once, nothing else. "
        "ONLY before any card or again_* has been played and only when no "
        "reason was given yet — a no that comes after them is the SECOND no "
        "(decline), and a no that already carries its reason skips this "
        "tool entirely.",
        "अच्छा, कोई खास वजह? ताकि मैं note कर लूँ.",
        "I see. Any particular reason? So I can note it down.",
    ),
    "guide": (
        "They said yes, asked how to continue, or asked what to do in the "
        "app: the one-shot walkthrough. 'बताओ' / 'बताइए' / 'tell me' in reply "
        "to the app-guidance question counts as a yes.",
        "Flipkart app खोलिए, cart में {product_name} पर place order कीजिए, payment पर "
        "Flipkart EMI select करें और बाकी steps वहीं पूरे कर लीजिए. जहाँ छोड़ा था वहीं "
        "से continue हो जाएगा.",
        "Open the Flipkart app, place the order for {product_name} from the "
        "cart, select Flipkart EMI at payment, and finish the remaining steps "
        "there. It continues right from where you left it.",
    ),
    "here": (
        "ONLY when the system message says the customer has been quiet — "
        "never as a reply to anything they said, never for a mid-call hello "
        "(that re-calls your last tool), never for a question you could not "
        "classify.",
        "Hello. क्या आप call पर हैं?",
        "Hello. Are you on the call?",
    ),
    "unclear": (
        "You could not HEAR or understand the words themselves: a pure "
        "mumble (ह्म्म्म्, mmm) or a fragment that stopped midway (तो, मैं, "
        "ये). Never a substitute for understanding a clear sentence — a "
        "clearly spoken answer is always handled by its own tool, whatever "
        "it says.",
        "जी, पूरा सुनाई नहीं दिया. ज़रा फिर से बोलिए.",
        "Sorry, I could not hear that fully. Could you say it again?",
    ),
    "misc": (
        "They ask or say something UNRELATED to this Flipkart EMI purchase "
        "(weather, politics, other products, other companies, general chat): "
        "the polite cannot-help line.",
        "जी, मैं इस call पर सिर्फ आपकी Flipkart EMI process में मदद कर सकती हूँ. क्या मैं "
        "आपको app में guide कर दूँ?",
        "I am sorry, I can only help with your Flipkart EMI process on this "
        "call. Shall I guide you in the app?",
    ),
    "bot": (
        "They ask whether you are a robot, AI, bot or a human.",
        "जी, मैं Flipkart की virtual assistant प्रियंका हूँ — आपकी EMI purchase पूरी करने "
        "में मदद के लिए. क्या मैं आपको app में guide कर दूँ?",
        "I am Priyanka, Flipkart's virtual assistant, here to help you "
        "complete your EMI purchase. Shall I guide you in the app?",
    ),
    "cancel": (
        "They ask what auto-pay / the mandate is, whether money will be cut "
        "automatically, or whether they can cancel.",
        "जी, ये एक standard auto-pay setup है जिससे EMI हर महीने अपने आप time पर जाती "
        "है. आप इसे app से कभी भी cancel कर सकते हैं.",
        "This is a standard auto-pay setup so your EMI is paid on time every "
        "month. You can cancel it anytime from the app.",
    ),
}

TOOLS: list[dict] = []
for stem, (description, hi, en) in PHRASES.items():
    TOOLS.extend(speech_pair(stem, description, hi, en))

TOOLS.extend(
    [
        outcome(
            "commit",
            "They clearly AGREED (a yes) to complete the pending steps — "
            "will do it later, OR they completed the steps DURING this call "
            "while you were guiding them ('हो गया', 'order place हो गया', "
            "'done, paid just now'). A QUESTION is never this — asking about "
            "auto-pay goes to cancel_*, asking how to continue goes to "
            "guide_*. NOT for work finished before this call — that is done.",
            "बहुत बढ़िया. Flipkart app में cart से payment पर Flipkart EMI select करते "
            "ही आप वहीं से continue कर पाएंगे. धन्यवाद, आपका दिन शुभ हो.",
            "Wonderful. In the Flipkart app, select Flipkart EMI at payment "
            "from the cart and you will continue right where you left off. "
            "Thank you, and have a nice day.",
            "APP_NUDGE_ACCEPTED",
            [
                (
                    "committed_step",
                    "The pending step(s) they agreed to complete, in their "
                    "words. If they finished during the call, the steps they "
                    "finished.",
                ),
                (
                    "completion_timeline",
                    "When they said they will do it, in their words — or "
                    "'completed during call' if they finished on this call.",
                ),
            ],
        ),
        outcome(
            "decline",
            "The customer's SECOND no, after you asked the reason and played "
            "one matching card. NEVER on the first no. Also when, after the "
            "reason question, they say there IS no particular reason "
            "(कोई कारण नहीं / nothing specific) — no card matches that, so "
            "accept it as the final no and speak the goodbye here instead of "
            "re-asking. Speaks the goodbye and ends the call.",
            "ठीक है, कोई बात नहीं. आपके feedback के लिए धन्यवाद. Have a nice day.",
            "Alright, no problem. Thank you for your feedback. Have a nice " "day.",
            "NOT_INTERESTED",
            [
                (
                    "drop_off_reason",
                    "Why they do not want to proceed, in THEIR OWN words — "
                    "include the reason they already gave ('bought it from a "
                    "local store', 'पैसे नहीं हैं'). If they gave none, pass "
                    "'no reason given'. Never invent or polish their words.",
                )
            ],
        ),
        outcome(
            "stuck",
            "They tried (now or earlier) and hit a problem they cannot get "
            "past — app error, KYC failing, mandate not going through, login "
            "or page issues. Speaks the closing and ends the call.",
            "मैंने आपकी problem note कर ली है, team इसे जल्दी ठीक करेगी. धन्यवाद. Have a "
            "nice day.",
            "I have noted the issue and the team will fix it soon. Thank you, "
            "and have a nice day.",
            "ISSUE_REPORTED",
            [
                (
                    "issue_description",
                    "The problem in their own words, as specifically as they "
                    "described it.",
                )
            ],
        ),
        outcome(
            "done",
            "They say they ALREADY completed the pending steps or the whole "
            "purchase BEFORE this call — yesterday, earlier, on their own, "
            "without you. Never use this for anything finished while talking "
            "to you on THIS call — 'अभी हो गया', 'order place हो गया', 'just "
            "now', 'while you were explaining' are all commit. Do not argue "
            "or re-verify.",
            "बहुत बढ़िया, फिर आपका order app में confirm दिखेगा. धन्यवाद. Have a nice "
            "day.",
            "Perfect, your order will show as confirmed in the app. Thank "
            "you, and have a nice day.",
            "ALREADY_DONE",
            [
                (
                    "claimed_step",
                    "What they say is already done, in their words.",
                )
            ],
        ),
        outcome(
            "busy",
            "They are busy, driving, in a meeting, or ask to call later — "
            "even when the reply starts with a yes (हाँ... नहीं अभी नहीं, मैं "
            "गाड़ी चला रहा हूँ). NOT about the process itself: 'no time, it "
            "must be a long process' (time नहीं है, लंबा process) is the "
            "quick card, not this — busy is only about the MOMENT (driving, "
            "meeting, sleeping, cannot talk now). The FIRST busy signal — no "
            "persuasion. Speaks the closing and ends the call.",
            "ठीक है, मैं बाद में call करूँगी. धन्यवाद. Have a nice day.",
            "Alright, I will call you later. Thank you, and have a nice day.",
            "BUSY",
            [
                (
                    "callback_note",
                    "When they asked to be called back, in their words. If "
                    "they gave no time, pass 'call back later - no time "
                    "given'.",
                )
            ],
        ),
        outcome(
            "wrong",
            "Wrong number, they are not the customer, or they never applied "
            "for anything on Flipkart. Apologize via the closing and end the "
            "call; collect nothing.",
            "माफ़ कीजिएगा, गलत नंबर लग गया. Have a nice day.",
            "Sorry for the trouble, wrong number. Have a nice day.",
            "WRONG_PERSON",
            [],
        ),
    ]
)

ROLE = """## FLIPKART EMI RECOVERY — HINDI/ENGLISH, TOOL-BASED

You are Priyanka from Flipkart EMI, helping the customer finish an incomplete Flipkart EMI purchase of {product_name}. The greeting already introduced you and asked for the customer — never do either again. You are a woman; the Hindi lines already carry feminine forms. You never write speech: calling a tool IS your reply, exactly ONE tool per turn.

LANGUAGE — THE TOOL NAME IS THE LANGUAGE
Every speech tool has a _hi and an _en twin that speak the same line in Hindi / English. Start with _hi. Switch to the _en twin of the CURRENT step only when the customer asks for English or replies in English — even a short unambiguous English reply ('Yes speaking', 'Yes, this is Rohit') switches. From then on use _en tools only (language lock) — and a Hindi sentence after Hindi lines is always _hi, an English sentence after English lines always _en. Isolated English words (EMI, app, okay, Flipkart) inside Hindi never trigger a switch. A mid-call hello/हेलो is them checking the line: re-call your LAST tool, same language — once; their next real reply is handled normally (yes → guide_*, question → the matching card). Silence (the system says they are quiet) → here_*. A mumble or a fragment that stops midway is never an answer and never a decision → unclear_*. None of these end the call.

STAGES
1. Right person (the greeting asked). Any conversational reply → offer_* — the hook: what they were buying, the approved offer, what is left, the app question. Except: a question about who you are or why you called → why_*. Busy words ANYWHERE in the reply (ड्राइविंग, meeting, शाम को call करो) → busy — even when the reply begins with हाँ. Wrong number / not the customer / never applied → wrong.
2. The hook answered. Yes / how do I do it / guide me → guide_*. A no or hesitation → reason_* (ask the reason, ONCE) — but if their no ALREADY carries the reason ('नहीं, पैसे नहीं हैं', 'Not interested, bought it elsewhere'), do NOT call reason_* at all: play the ONE matching card at once, or again_* when no card matches. A QUESTION or doubt → pick the ONE card tool whose description matches their words (nocost loan tenure down safe quick cancel bot misc — read the descriptions), then again_*. When in doubt between a card and guide: a question gets the card, a yes gets guide_*.
3. Count the nos. The FIRST no NEVER ends the call — even a firm one, even with the reason attached: reason_* (unless the reason already came) or the matching card, then again_*. ONE exception: their reason makes the purchase moot — they already bought the product elsewhere ('bought it from a local store') — then decline at once with their reason. reason_* is asked at most ONCE per call. Any no AFTER a card or again_* is the SECOND no → decline. They hit an app error / KYC failing / page not loading → stuck. They completed it BEFORE this call → done. They finish it during this call, or agree to complete it → commit.

OUTCOME TOOLS (each speaks its closing and ends the call)
Every outcome takes a language argument: the customer's CURRENT language ('en' after a switch).
- commit(committed_step, completion_timeline, language) — they agreed to finish in the app, or finished during this call.
- decline(drop_off_reason, language) — the SECOND no only, their reason verbatim, never invent.
- stuck(issue_description, language) — the problem in their words.
- done(claimed_step, language) — they say it is already complete.
- busy(callback_note, language) — first busy signal, when to call back in their words.
- wrong(language) — not the customer.
A spoken number or amount goes into arguments exactly as the customer said it.

FACTS
Only the KNOWLEDGE BASE below may be spoken, through the fixed tool lines. Never invent a rate, amount, date or step. Use nocost_* only while no-cost EMI applies."""

TASK = (
    "Purchase: {product_name}. Lender: {lender}. Approved loan: "
    "{approved_loan_amount}. Credit limit: {credit_limit}. Tenures: "
    "{applicable_tenures}. No-cost tenures: {no_cost_emi_tenures} (no-cost "
    "applies: {no_cost_emi_applicable}). Downpayment: {downpayment_amount}. "
    "Pending: {pending_steps}. Customer: {customer_name}. Stage machine: "
    "right person -> hook -> (question? card) / (no? reason -> one card -> "
    "second no ends) -> outcome. One tool per turn."
)

SONIOX_CONTEXT = {
    "general": [
        {"key": "organisation", "value": "Breeze"},
        {"key": "company", "value": "Flipkart EMI customer care"},
        {"key": "product", "value": "Flipkart EMI checkout recovery call"},
        {"key": "domain", "value": "E-commerce EMI financing"},
        {"key": "service_type", "value": "Incomplete purchase recovery"},
        {"key": "conversation_type", "value": "Outbound automated voice call"},
        {
            "key": "purpose",
            "value": "Help the customer finish the pending Flipkart EMI checkout steps in the app",
        },
        {"key": "user", "value": "Flipkart customer"},
        {"key": "languages", "value": "Hindi, English"},
        {"key": "region", "value": "India"},
    ],
    "text": (
        "An automated voice agent calls customers who started a Flipkart EMI "
        "checkout but did not finish it. The agent shares the approved offer, "
        "answers questions about no-cost EMI, loan amount, tenures, "
        "downpayment, auto-pay, safety, and guides them to complete the "
        "pending steps in the Flipkart app. Replies are mostly Hindi with "
        "English finance words."
    ),
    "terms": [
        "Flipkart",
        "EMI",
        "no-cost EMI",
        "checkout",
        "cart",
        "order",
        "payment",
        "lender",
        "loan",
        "credit limit",
        "downpayment",
        "tenure",
        "KYC",
        "auto-pay",
        "mandate",
        "agreement",
        "OTP",
        "app",
        "रेटिंग",
        "ईएमआई",
        "किश्त",
        "लोन",
        "क्रेडिट लिमिट",
        "डाउनपेमेंट",
        "टेन्योर",
        "महीने",
        "ब्याज",
        "ऑटो-पे",
        "मैंडेट",
        "एग्रीमेंट",
        "ओटीपी",
        "ऐप",
        "ऑर्डर",
        "कार्ट",
        "पेमेंट",
        "साफ़",
        "नहीं",
        "हाँ",
        "जी",
        "बोलिए",
        "धन्यवाद",
        "बदले",
        "रद्द",
    ],
    "translation_terms": [],
}

template = {
    "id": "c8d1f4a2-6b73-4e29-9f61-02ab7c95d4e1",
    "reseller_id": "rnr",
    "merchant_id": "test-shopify",
    "name": "flipkart-emi-dropoff-recovery-toolbased",
    "description": (
        "Bilingual tool_based Flipkart EMI dropoff recovery, "
        "latency-optimized: per-language split functions with unique first "
        "letters (name decode gates early-fire TTS), exactly one dialogue per "
        "function so every line is DragonTTS-cacheable, prod outcome hooks, "
        "off-topic / bot / safety / auto-pay misc phrases."
    ),
    "expected_payload_schema": {
        "lender": {"type": "string", "example": "Fibe"},
        "credit_limit": {
            "type": "number",
            "example": 100000,
            "function": "indian_number_to_speech",
        },
        "product_name": {
            "type": "string",
            "example": "Samsung Galaxy S24, 8GB RAM",
        },
        "customer_name": {"type": "string", "example": "Rohit Kumar"},
        "pending_steps": {
            "type": "string",
            "example": "auto-pay setup, agreement signing",
        },
        "approved_loan_amount": {
            "type": "number",
            "example": 75000,
            "function": "indian_number_to_speech",
        },
        "applicable_tenures": {
            "type": "string",
            "example": "3, 6, 9, 12 months",
        },
        "downpayment_amount": {
            "type": "number",
            "example": 4999,
            "function": "indian_number_to_speech",
        },
        "no_cost_emi_tenures": {"type": "string", "example": "3, 6 months"},
        "no_cost_emi_applicable": {"type": "string", "example": "yes"},
        "customer_mobile_number": {
            "type": "string",
            "function": ["extract_10_digit_mobile", "digits_to_speech"],
        },
    },
    "example_payload": {
        "customer_name": "Rohit Kumar",
        "product_name": "Samsung Galaxy S24, 8GB RAM",
        "lender": "Fibe",
        "approved_loan_amount": 75000,
        "credit_limit": 100000,
        "applicable_tenures": "3, 6, 9, 12 months",
        "no_cost_emi_tenures": "3, 6 months",
        "no_cost_emi_applicable": "yes",
        "downpayment_amount": 4999,
        "pending_steps": "auto-pay setup, agreement signing",
        "customer_mobile_number": "9999999999",
    },
    "configurations": {
        "llm_configurations": QWEN_LLM,
        "tts_configuration": {
            "provider": "elevenlabs",
            "voice_id": "iB2rIwm9cQCRGWoKDRtX",
            "model": "eleven_v3_conversational",
            "language": "hi",
            "speed": 1,
            "stability": None,
            "similarity_boost": None,
            "volume": None,
            "emotion": None,
            "pitch": None,
            "style_prompt": None,
            "enable_ssml_parsing": None,
            "enable_tts_caching": True,
        },
        "stt_configuration": {
            "provider": "soniox",
            # Bilingual callers mix Hindi and English mid-sentence; both
            # hints keep the recognizer from forcing one script.
            "language": "hi,en",
            # stt_native fired a turn per Soniox final — a mid-sentence
            # pause made the bot answer a fragment (observed live on the
            # redbus template 2026-09-06). timeout keeps the turn open
            # across finals while VAD says the customer is still speaking.
            "turn_detection": "timeout",
            "user_speech_timeout": 3.0,
            "soniox": {"context": json.dumps(SONIOX_CONTEXT, ensure_ascii=False)},
        },
        "initial_greeting": (
            "नमस्ते, मैं प्रियंका बोल रही हूँ, Flipkart से. आपके EMI के बारे में कॉल "
            "किया है. क्या मेरी बात {customer_name} से हो रही है?"
        ),
        "user_idle_configuration": {
            "enabled": True,
            "timeout": 15,
            "idle_message": "The user has been quiet for a while. Call here_hi or here_en.",
            "max_retries": 2,
        },
        "interruption": {"mode": "enabled", "min_words": 1},
    },
    "flow": {
        "mode": "tool_based",
        "initial_node": "initial",
        "nodes": [
            {
                "node_name": "initial",
                "role_messages": [{"role": "system", "content": ROLE}],
                "task_messages": [{"role": "developer", "content": TASK}],
                "pre_actions": [],
                "post_actions": [],
                "functions": TOOLS,
            }
        ],
    },
    "telephony_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "outbound_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "is_active": True,
    "supported_channels": ["voice"],
}

OUT.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n")

# Sanity: template validates, names obey the design rules.
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel  # noqa: E402

model = TemplateModel.model_validate(template)
names = [
    f["function_name"]
    for node in model.flow["nodes"]
    for f in node.get("functions", [])
]
speech = [
    f
    for f in names
    if not f.startswith(("commit", "decline", "stuck", "done", "busy", "wrong"))
]
stems = {n.rsplit("_", 1)[0] for n in speech}
first_letters = {s[0] for s in stems}
assert len(stems) == len(first_letters), "stems must have unique first letters"
assert len(names) == len(set(names)), "function names must be unique"
print(
    f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(names)} tools: "
    f"{len(stems)} speech stems x 2 languages + 6 outcomes)"
)
