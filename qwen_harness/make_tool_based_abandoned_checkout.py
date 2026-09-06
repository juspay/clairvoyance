#!/usr/bin/env python3
"""Build the tool_based abandoned-checkout template (DB format) for qwen.

Base: BB_SHOPIFY "abandoned-checkout" (purchase recovery). The base's probe /
pitch scripts (opening, after_consent, issue_help, intent_probing,
deflection_probe, price_probe, price_value_pitch, trust_concern_pitch,
random_browsing_pitch, general_product_pitch_1, final_product_pitch + the
objection-specific handling) become per-language functions (say_x_hi /
say_x_en — no language arg, the name alone determines speech); the four
classification outcomes (HIGH_INTENT / LOW_INTENT / ALREADY_PURCHASED / BUSY)
speak their own closings and end the call. <cart_reference> becomes a verbatim
tool arg the model fills from {cart_items_summary} (count-if->3 rule in the
description). The giant probe-ladder prompt shrinks to the ladder rules +
enum mapping.

Output: qwen_harness/abandoned-checkout-toolbased.json
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "abandoned-checkout-toolbased.json"

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

LANG = {
    "type": "string",
    "enum": ["hi", "en"],
    "description": "Language the customer is speaking right now "
    "(hi = Hindi/Hinglish, en = English). Lock once they switch.",
}
CART_REF = {
    "type": "string",
    "description": "The cart reference for the line, built from the real "
    "{cart_items_summary}: drop quantity tags like (x1)/(x2), drop duplicate "
    "names, count the distinct products. 3 or fewer → pass those product "
    "names exactly as the payload spells them. 4 or more → pass ONLY the "
    "count phrase like '4 products'. Never invent a product name.",
}
HIGH_ENUM = [
    "PAYMENT_FAILED",
    "PAYMENT_METHOD_UNAVAILABLE",
    "TECHNICAL_ISSUE",
    "DELIVERY_PINCODE_BLOCKER",
    "WANTS_DISCOUNT",
    "SIZE_FIT_CONCERN",
    "DESIGN_COLOR_CONCERN",
    "MATERIAL_QUALITY_CONCERN",
    "SPECS_FEATURE_QUERY",
    "WARRANTY_RETURN_CONCERN",
    "COMPARING_ALTERNATIVES",
    "PRODUCT_DETAIL_GAP",
]
LOW_ENUM = [
    "JUST_BROWSING",
    "PRICE_TOO_HIGH",
    "PRODUCT_NOT_AVAILABLE",
    "ALREADY_PURCHASED_ELSEWHERE",
    "STILL_DECIDING",
    "CHANGED_MIND",
]


def say(hi: str, en: str, *, end_call: bool = False) -> dict:
    block: dict = {
        "utterances": {"hi": [hi], "en": [en]},
        "phrasing": "first",
        "default_language": "hi",
    }
    if end_call:
        block["end_call"] = True
    return block


def tool(
    name: str,
    description: str,
    hi: str,
    en: str,
    props: dict | None = None,
    required: list | None = None,
    hooks: list | None = None,
    end_call: bool = False,
) -> dict:
    properties: dict = {"language": LANG}
    if props:
        properties.update(props)
    return {
        "function_name": name,
        "description": description,
        "properties": properties,
        "required": ["language"] + (required or []),
        "transition_to": None,
        "hooks": hooks or [],
        "say": say(hi, en, end_call=end_call),
    }


def split_languages(fn: dict) -> list[dict]:
    """Emit one function per language: say_x_hi / say_x_en.

    The function NAME alone determines the spoken line (no language arg),
    so a streaming parser can fire TTS the moment the name finishes
    decoding. Definitions stay authored once with both utterances; this
    splitter stamps out the per-language variants.
    """
    variants = []
    for lang in ("hi", "en"):
        # the _hi/_en suffix in the name IS the variant differentiator —
        # keep descriptions decision-first, identical across variants
        variant = {
            "function_name": f"{fn['function_name']}_{lang}",
            "description": fn["description"],
            "properties": {
                k: v for k, v in fn["properties"].items() if k != "language"
            },
            "required": [r for r in fn["required"] if r != "language"],
            "transition_to": None,
            "hooks": fn["hooks"],
            "say": {
                "utterances": {lang: fn["say"]["utterances"][lang]},
                "phrasing": fn["say"]["phrasing"],
                "default_language": lang,
                **({"end_call": True} if fn["say"].get("end_call") else {}),
            },
        }
        variants.append(variant)
    return variants


ROLE = """## ABANDONED PURCHASE RECOVERY — TOOL-BASED

You are Ram, a male support agent from {shop_name}. The customer selected products and reached the payment stage, but the purchase was not completed. Your job: find the REAL reason, help with it, try to recover the purchase, then classify exactly once. The greeting already opened the call — never reintroduce yourself, use their name no more than once more.

You never write speech — your tools ARE your lines. Exactly one tool call per turn.

LANGUAGE PICK: every tool comes in exactly two variants — _hi and _en. Pick the variant for the language of the customer's LATEST reply: a full English sentence ('the payment failed while I was trying') or an English request → _en, even as the first reply after the Hindi greeting; Hindi/Hinglish → _hi. Once locked, only an explicit switch changes the variant again. Never mix variants in one turn.

THE LADDER (soft objections: not interested / just browsing / price high / changed mind / maybe later / no need / store trust):
- NEVER classify on the first soft brush-off. Make at least 2 (at most 3) probe/pitch attempts, and WAIT for the customer's reply after each — never call a classification tool in the same turn where a question was asked.
- THE LADDER COUNT: attempt 1 = say_deflection_probe, or the pitch matching their opening reason (say_random_browsing_pitch / say_trust_concern_pitch / say_issue_help). Attempt 2 = the pitch matching the reason they gave when refusing attempt 1 — and if that refusal gives NO new reason ('नहीं', 'ज़रूरत नहीं', 'try नहीं करना'), attempt 2 is say_deflection_probe or say_general_product_pitch. NEVER skip to attempt 3 on a reason-less refusal. Attempt 3 = say_final_product_pitch — ONLY on the refusal that comes after attempt 2, never earlier. Each attempt needs the customer's refusing reply in between; never repeat an attempt-2 pitch, and never classify before a refusal to say_final_product_pitch (a firm explicit final reason is the only exception).
- Their FIRST answer with a concrete recoverable issue skips the ladder: payment failed / OTP / card declined → say_issue_help; no UPI / no card / wanted COD → say_issue_help too; site slow / crash / button stuck → say_issue_help; pincode or delivery concern → say_issue_help; vague 'kuch nahi' → say_intent_probing.
- Price high → say_price_probe FIRST (price itself or budget timing), then say_price_value_pitch. Store trust / genuine / reviews → say_trust_concern_pitch. Just browsing → say_random_browsing_pitch. Size or fit doubt → say_size_fit_help. Return / warranty worry → say_return_exchange_help. Asks for a discount → say_discount_response (never promise one).
- After any pitch: convinced / sounds good / will try / asks how to complete → high_intent_recovery. Still refusing after attempt 3 → low_intent_recovery. Firm anger or 'stop calling' → user_busy.

CONVERTED vs FIRM: a small okay/hmm after a pitch is interest only if they agree to try or complete — otherwise one more matching attempt. Their hesitation is not a no.

OUTCOMES (each speaks its closing and ends the call — exactly one per call):
- high_intent_recovery(abandonment_reason) — recoverable blocker or convinced. Map: payment failed/OTP/declined → PAYMENT_FAILED; no UPI/card/COD missing → PAYMENT_METHOD_UNAVAILABLE; site slow/crash/stuck → TECHNICAL_ISSUE; pincode not serviceable / delivery slow → DELIVERY_PINCODE_BLOCKER; asked for discount → WANTS_DISCOUNT (if they asked for a discount at ANY point — even one they then converted without — it is WANTS_DISCOUNT, never PRODUCT_DETAIL_GAP); size/fit → SIZE_FIT_CONCERN; design/colour → DESIGN_COLOR_CONCERN; fabric/build quality → MATERIAL_QUALITY_CONCERN; specs/features/compatibility → SPECS_FEATURE_QUERY; warranty/return/exchange → WARRANTY_RETURN_CONCERN; comparing with another model/store → COMPARING_ALTERNATIVES; everything else (trust converted, price-value clarity, product clarity, random browsing converted) → PRODUCT_DETAIL_GAP.
- low_intent_recovery(abandonment_reason) — firm no after the ladder. JUST_BROWSING / PRICE_TOO_HIGH / PRODUCT_NOT_AVAILABLE / ALREADY_PURCHASED_ELSEWHERE / STILL_DECIDING / CHANGED_MIND. PRICE_TOO_HIGH only when they NEVER asked for a discount — a refusal that mentions discount/coupon is WANTS_DISCOUNT.
- already_purchased — they say THIS order is already placed/done. (Bought from ANOTHER store instead → low_intent_recovery with ALREADY_PURCHASED_ELSEWHERE, after one probe.)
- user_busy — busy / driving / meeting / call later, or angry and refusing to continue. Immediately, no convincing.

OTHER RULES:
- A small yes/haan/ji after the greeting, or 'why did you call' → say_after_consent. Silent → say_idle_reengage.
- Completely unrelated question (politics, cricket, 'are you a robot?', 'how did you get my number?', 'connect me to a human') → say_off_topic — answer nothing, redirect back.
- Never say 'checkout', 'cart' or 'abandoned' — 'selected items' / 'products'. Never offer or promise a discount, never offer to send anything. Never mention their phone number.
- Keep every turn to the tool's line — no additions."""


def outcome(
    name: str,
    description: str,
    hi: str,
    en: str,
    outcome_value: str,
    props: dict | None = None,
    required: list | None = None,
) -> dict:
    expected = {
        "outcome": {"value": outcome_value, "source": "static"},
    }
    for r in required or []:
        if r != "language":
            expected[r] = {"source": "llm"}
    return tool(
        name,
        description,
        hi,
        en,
        props=props,
        required=required,
        hooks=[{"name": "update_outcome_in_database", "expected_fields": expected}],
        end_call=True,
    )


TOOL_DEFS = [
    # ---- speech tools -------------------------------------------------
    tool(
        "say_after_consent",
        "Their first reply was ONLY a bare yes/okay/haan/ji with nothing "
        "else, or a question ('why did you call?', 'which products?'). NOT "
        "for replies that already carry a reason or feeling — 'payment fail "
        "हो गया', 'ऐसे ही देख रही थी', 'price ज़्यादा है', 'कुछ समझ नहीं आया' "
        "all route directly to their matching probe/pitch/issue tool.",
        "Okay. Thank you. ये {cart_reference} के बारे में था, total around "
        "{cart_total} था। मैं बस समझना चाहता था कि payment complete होने से "
        "पहले क्या हुआ, ताकि मैं आपकी help कर पाऊँ।",
        "Okay. Thank you. This was about {cart_reference}, around "
        "{cart_total} in total. I just wanted to understand what happened "
        "before the payment was completed, so I can help you.",
        props={"cart_reference": CART_REF},
        required=["cart_reference"],
    ),
    tool(
        "say_intent_probing",
        "Their answer was a pure non-answer — NO reason and NO signal at "
        "all: 'पता नहीं', 'कोई reason नहीं', 'समझ नहीं आया, छोड़ दिया', "
        "'nothing specific, just left it'. If they mentioned browsing, "
        "price, trust, a product doubt or any issue, that is NOT vague — "
        "route it to its matching tool.",
        "Okay. No problem. बस आपकी help करने के लिए समझना चाहता हूँ, issue "
        "website या payment side पर था, delivery से related था, price की "
        "वजह से था, store trust को लेकर था, या product को लेकर कोई doubt था?",
        "Okay. No problem. Just so I can help you better — was it more of a "
        "website or payment issue, a delivery concern, the price, trust in "
        "the store, or something about the product itself?",
    ),
    tool(
        "say_deflection_probe",
        "Soft brush-off attempt 1: not interested / changed mind / just "
        "looking / no need now. Asks what changed since they checked the "
        "items. NEVER classify right after this.",
        "Okay. समझ गया। कोई pressure नहीं है। आपने {cart_reference} check किए "
        "थे, इसलिए बस जानना चाहता हूँ कि बाद में क्या change हुआ। Product, "
        "price, delivery या store trust में कुछ doubt था क्या?",
        "Okay. I understand. No pressure at all. Since you had checked "
        "{cart_reference}, I just wanted to know what changed after that. "
        "Was it the product, the price, delivery, or trust in the store?",
        props={"cart_reference": CART_REF},
        required=["cart_reference"],
    ),
    tool(
        "say_price_probe",
        "They said price is high / costly / expensive. First understand: the "
        "price itself, or budget timing? No discount offer here.",
        "Right. Fair है। Honestly बताने के लिए thank you। Product का price ही "
        "high लग रहा है, या अभी budget timing ठीक नहीं है? दोनों okay हैं, बस "
        "मुझे reason समझने में help मिलेगी।",
        "Right. That's fair, thanks for being honest. Is the product price "
        "itself feeling high, or is it more about your budget timing right "
        "now? Both are completely okay; it just helps me understand better.",
    ),
    tool(
        "say_price_value_pitch",
        "Price-vs-value pitch (attempt 2 for price). Quality and long-term "
        "durability. NEVER offer a discount here, and NEVER repeat this "
        "exact pitch once it has already been spoken this call — if the "
        "customer still refuses on price after it, the next attempt is "
        "say_final_product_pitch (attempt 3), then classify.",
        "Right. समझता हूँ, price important होता है। ये products quality और "
        "long-term durability को ध्यान में रखकर रखे गए हैं, इसलिए value सिर्फ "
        "आज के लिए नहीं, long time तक use करने के हिसाब से है। अगर quality और "
        "durability आपके लिए important है, तो ये किसी cheap option से better "
        "choice हो सकता है। क्या इस हिसाब से आपको ये थोड़ा worth trying लग रहा "
        "है?",
        "Right. I understand, price matters. These products are priced for "
        "good quality and long-term durability, so the value is not just for "
        "today but for longer use. If quality and durability are important "
        "for you, this can be a better choice than buying something cheaper "
        "that may not last. Does that make it feel a bit more worth trying?",
    ),
    tool(
        "say_trust_concern_pitch",
        "They doubt the store: genuine? reviews? delivery worry? The "
        "trust-building pitch (attempt 2 for trust).",
        "Right. मैं आपका concern समझता हूँ। {shop_name} on-time delivery और "
        "high-quality products के लिए जाना जाता है, और आप चाहें तो store के "
        "reviews भी check कर सकते हैं। Purchase के बाद भी support रहेगा। क्या "
        "इससे आपको store से purchase complete करने में थोड़ा comfortable लग रहा "
        "है?",
        "Right. I understand your concern. {shop_name} is known for on-time "
        "delivery and high-quality products, and you can also search and "
        "check customer reviews about the store. The store also has support "
        "if you need help after purchase. Does that make you feel more "
        "comfortable completing the purchase from the store?",
    ),
    tool(
        "say_random_browsing_pitch",
        "They said they were randomly checking / just browsing / casually "
        "looking / 'ऐसे ही देख रही थी' / 'कुछ खास नहीं, देख रही थी'. Browsing "
        "IS the reason — go straight to the quality pitch with a soft "
        "try-question.",
        "Okay. बिल्कुल ठीक है। आप {cart_reference} check कर रहे थे, तो एक बार "
        "इन्हें try कर सकते हैं। Store product quality पर focus करता है, और "
        "मुझे लगता है product receive करने के बाद quality आपको अच्छी लगेगी। Try "
        "करने लायक लग रहा है क्या?",
        "Okay. That's completely fine. Since you were checking "
        "{cart_reference}, you can try these items once. The store focuses "
        "on product quality, and I'm confident the quality can impress you "
        "when you receive it. Does it sound worth trying?",
        props={"cart_reference": CART_REF},
        required=["cart_reference"],
    ),
    tool(
        "say_size_fit_help",
        "Their doubt is size or fit (which size, will it fit). Reassures "
        "with try + return/exchange.",
        "Right. Size का doubt बहुत normal है। आप इन items को एक बार order करके "
        "try कर सकते हैं — अगर fit ना हो तो return या exchange भी हो जाता है, "
        "उसमें कोई problem नहीं है। तो एक बार try करने का mood है?",
        "Right. Size doubts are very normal. You can order these items once "
        "and try them — if the fit isn't right, a return or exchange works "
        "too, that's no problem at all. So, worth giving it a try?",
    ),
    tool(
        "say_return_exchange_help",
        "Their worry is returns / refund / warranty / after-sales. Reassures "
        "with the support story.",
        "Right. उसमें कोई tension नहीं है। Damaged या गलत item आने पर return या "
        "replacement का request store team से हो जाता है, और purchase के बाद "
        "भी support रहता है। तो purchase complete कर दें?",
        "Right. No tension there. If a damaged or wrong item arrives, a "
        "return or replacement request can be raised with the store team, "
        "and support continues after purchase too. So — shall we complete "
        "the purchase?",
    ),
    tool(
        "say_discount_response",
        "They asked for a discount / coupon / any offer. NEVER promise one "
        "and never ask if they want a discount — team-review line, then "
        "steer to value. If they refuse AFTER this line ('बिना discount के "
        "नहीं चाहिए'), the blocker is still the discount: next attempt is "
        "say_final_product_pitch, and if they still refuse, "
        "low_intent_recovery with WANTS_DISCOUNT — never PRICE_TOO_HIGH.",
        "जी. Discount की अभी मैं guarantee नहीं बता सकता, ये team review कर "
        "सकती है। लेकिन products की quality और value इस price पर अच्छी है। "
        "Purchase complete करना चाहेंगे?",
        "Sure. I can't guarantee a discount right now — the team can review "
        "that. But the product quality and value at this price is good. "
        "Would you like to complete the purchase?",
    ),
    tool(
        "say_issue_help",
        "They hit a concrete issue: payment failed, OTP, card declined, "
        "website slow/crash/button stuck, or a delivery/pincode concern. "
        "Apologize + guide the retry.",
        "Sorry. मैं issue समझ गया। अगर payment या website side पर problem था, "
        "तो आप store से purchase फिर से complete कर सकते हैं, और मैं ये issue "
        "team के लिए note कर लेता हूँ। क्या आप store से फिर से purchase complete "
        "try कर पाएंगे?",
        "Sorry. I understand the issue. If it was a payment or website "
        "problem, you can try completing the purchase again from the store, "
        "and I'll note this issue for the team. Would you be able to try "
        "completing it again from the store?",
    ),
    tool(
        "say_general_product_pitch",
        "Attempt 2 when they are still unsure with no specific concern. "
        "Quality + help offer, asks which part bothers them.",
        "Right. समझ गया। मैं इसलिए पूछ रहा हूँ क्योंकि आपने {cart_reference} "
        "already select किए थे, मतलब कुछ तो आपको पसंद आया होगा। ये "
        "quality-focused products हैं, और अगर doubt fit, quality, delivery या "
        "trust को लेकर है, तो मैं उसे clear करने में help कर सकता हूँ। अभी कौन "
        "सा part आपको सबसे ज़्यादा doubt दे रहा है?",
        "Right. I understand. The reason I'm checking is that you had "
        "already selected {cart_reference}, so there must have been "
        "something you liked. These are quality-focused products, and if "
        "your doubt is about fit, quality, delivery or trust, I can help "
        "clear it for you. Which part is bothering you the most right now?",
        props={"cart_reference": CART_REF},
        required=["cart_reference"],
    ),
    tool(
        "say_final_product_pitch",
        "Attempt 3 — the FINAL pitch. Count your NON-FINAL probe/pitch "
        "attempts this call (anything except this tool): this tool is "
        "FORBIDDEN until TWO have already been spoken and refused (e.g. "
        "browsing pitch + matching pitch, or deflection probe + "
        "price-value pitch). A price refusal after say_price_value_pitch "
        "included — never repeat the attempt-2 pitch. If they refuse THIS "
        "one too, classify. Respectful last suggestion.",
        "Fine. मैं आपकी choice पूरी तरह respect करता हूँ। मेरी तरफ से बस last "
        "suggestion है, अगर ये items आपको पहले पसंद आए थे तो एक बार try कर "
        "सकते हैं, क्योंकि {shop_name} product quality, smooth delivery और "
        "customer support पर focus करता है। अगर अब आपको ठीक लग रहा है, तो आप "
        "store से purchase complete कर सकते हैं। क्या आप इसे आगे continue करना "
        "चाहेंगे?",
        "Fine. I completely respect your choice. My last suggestion is to "
        "try the items once if you liked them earlier, because {shop_name} "
        "focuses on product quality, smooth delivery, and customer support "
        "if you need help. If it feels okay to you now, you can complete "
        "the purchase from the store. Would you like to go ahead with it?",
    ),
    tool(
        "say_idle_reengage",
        "The customer has gone silent.",
        "Hello. क्या आप अभी line पर हैं?",
        "Hello. Are you still on the line?",
    ),
    tool(
        "say_off_topic",
        "They asked something with NO relation to the purchase — politics, "
        "cricket, weather, 'are you a robot?', 'how did you get my "
        "number?', 'connect me to a human'. One polite redirect back to "
        "the pending question. Never play along with the new topic.",
        "हम्म, वो तो मैं नहीं बता सकता। मैं बस आपकी उस incomplete purchase "
        "में help करने के लिए call कर रहा हूँ — बताइए, payment से पहले क्या "
        "issue आया था?",
        "Hmm, I can't really answer that. I'm just calling to help with "
        "that incomplete purchase — so, what issue came up before the "
        "payment?",
    ),
    # ---- outcome tools ------------------------------------------------
    outcome(
        "high_intent_recovery",
        "The customer is convinced / recoverable / willing to complete the "
        "purchase (agreed to retry after say_issue_help, converted by a "
        "pitch, or asked how to complete). Speak nothing extra — the closing "
        "line here is the only completion guidance.",
        "Thanks. Please purchase complete कर दीजिए। Have a great day.",
        "Thanks. Please complete the purchase. Have a great day.",
        "HIGH_INTENT",
        props={
            "abandonment_reason": {
                "type": "string",
                "enum": HIGH_ENUM,
                "description": "The recoverable blocker or conversion "
                "reason. PAYMENT_FAILED / PAYMENT_METHOD_UNAVAILABLE / "
                "TECHNICAL_ISSUE / DELIVERY_PINCODE_BLOCKER for concrete "
                "issues; WANTS_DISCOUNT if they asked for one; SIZE_FIT_"
                "CONCERN / DESIGN_COLOR_CONCERN / MATERIAL_QUALITY_CONCERN "
                "/ SPECS_FEATURE_QUERY / WARRANTY_RETURN_CONCERN / "
                "COMPARING_ALTERNATIVES for that exact product doubt; "
                "PRODUCT_DETAIL_GAP when trust, price-value clarity or "
                "product clarity was the thing you resolved.",
            }
        },
        required=["abandonment_reason"],
    ),
    outcome(
        "low_intent_recovery",
        "Count your NON-FINAL probe/pitch attempts this call: FORBIDDEN "
        "while that count is under 2, and say_final_product_pitch must "
        "already have been spoken and refused too. A first soft brush-off "
        "('not interested', 'bought from another store', 'price high') "
        "gets a probe/pitch first; 'bought elsewhere' gets "
        "say_deflection_probe first — and once that probe confirms they "
        "bought elsewhere ('Amazon से कर लिया', 'सस्ता मिल गया'), classify "
        "right there with ALREADY_PURCHASED_ELSEWHERE — no price path. "
        "Only then: firm no, bought elsewhere, price the firm blocker "
        "after the value pitch, or genuinely changed mind.",
        "Thanks. Have a great day.",
        "Thanks. Have a great day.",
        "LOW_INTENT",
        props={
            "abandonment_reason": {
                "type": "string",
                "enum": LOW_ENUM,
                "description": "The real low-intent reason after probing: "
                "JUST_BROWSING / PRICE_TOO_HIGH / PRODUCT_NOT_AVAILABLE / "
                "ALREADY_PURCHASED_ELSEWHERE / STILL_DECIDING / CHANGED_"
                "MIND.",
            }
        },
        required=["abandonment_reason"],
    ),
    outcome(
        "already_purchased",
        "They say this order is already placed / completed (the purchase "
        "went through after all). Not for 'bought from another store' — "
        "that is low_intent_recovery with ALREADY_PURCHASED_ELSEWHERE "
        "after one probe.",
        "Great. फिर disturb करने के लिए sorry। हमारे साथ shopping करने के लिए "
        "thank you। Have a wonderful day.",
        "Great. Apologies for the disturbance then. Thanks so much for "
        "shopping with us. Have a wonderful day.",
        "ALREADY_PURCHASED",
    ),
    outcome(
        "user_busy",
        "They are busy / driving / in a meeting / ask to call later, or are "
        "angry and refusing to continue. Immediate — no convincing, no "
        "pitch.",
        "Okay. कोई problem नहीं। Call pick करने के लिए thank you। Have a great " "day.",
        "Okay. No problem at all, thank you for picking up. Have a great " "day.",
        "BUSY",
    ),
]

# One function per text per language — the name alone determines speech.
TOOLS = [variant for fn in TOOL_DEFS for variant in split_languages(fn)]

template = {
    "id": "8d2e0b3f-5c4a-4f9b-8a71-2e3f4a5b6c71",
    "reseller_id": "rnr",
    "merchant_id": "test-shopify",
    "name": "abandoned-checkout-toolbased",
    "is_active": True,
    "supported_channels": ["voice"],
    "secrets": None,
    "telephony_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "outbound_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "description": (
        "tool_based conversion of the Shopify abandoned-checkout recovery "
        "flow, tuned for qwen3.8-27b-4bit: the probe/pitch ladder is say "
        "blocks (hi/en), <cart_reference> is a verbatim arg built from "
        "{cart_items_summary}, and the four classification outcomes speak "
        "their own closings and end the call. The model routes the ladder "
        "and maps the dug-out reason to the enum."
    ),
    "expected_payload_schema": {
        "shop_name": {"type": "string", "function": "string_to_lowercase"},
        "cart_total": {
            "type": "number",
            "function": "indian_number_to_speech",
            "description": "Cart total as English Indian-number speech — "
            "already includes 'rupees'; never add another.",
        },
        "customer_name": {"type": "string", "function": "string_to_lowercase"},
        "cart_items_summary": {
            "type": "string",
            "description": "Real cart items for this call. Build cart "
            "reference args from this only.",
        },
        "customer_mobile_number": {
            "type": "string",
            "function": "extract_10_digit_mobile",
            "description": "Context only — never mention it to the " "customer.",
        },
    },
    "expected_callback_response_schema": {
        "abandonment_reason": {"type": "string", "optional": True},
    },
    "configurations": {
        "llm_configurations": QWEN_LLM,
        "tts_configuration": TTS,
        "initial_greeting": (
            "हेलो. {customer_name}, मैं Ram, {shop_name} से बोल रहा हूँ। आपने "
            "हमारे store पर कुछ products purchase करने की try की थी, लेकिन "
            "payment complete नहीं हुआ। क्या मैं help करने के लिए आपका एक minute "
            "ले सकता हूँ?"
        ),
        "user_idle_configuration": {
            "enabled": True,
            "timeout": 15,
            "idle_message": (
                "The user has been quiet for a while. Call say_idle_reengage."
            ),
            "max_retries": 3,
        },
        "dial_tone": True,
        "enable_background_sound": False,
        "background_sound_volume": 2,
        "enable_text_input": True,
        "interruption": {"mode": "enabled", "min_words": 1},
        "keyword_filter": None,
        "observers": None,
    },
    "flow": {
        "mode": "tool_based",
        "initial_node": "initial",
        "nodes": [
            {
                "node_name": "initial",
                "pre_actions": [],
                "post_actions": [],
                "role_messages": [{"role": "system", "content": ROLE}],
                "task_messages": [
                    {
                        "role": "developer",
                        "content": (
                            "Shop {shop_name}, customer {customer_name}, "
                            "items {cart_items_summary}, total {cart_total}. "
                            "Stage machine: first answer -> (issue_help | "
                            "ladder) -> one classification. One tool per "
                            "turn."
                        ),
                    }
                ],
                "functions": TOOLS,
            }
        ],
        "end_conversation_callbacks": ["service_callback"],
    },
}

OUT.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n")
print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(TOOLS)} tools)")
