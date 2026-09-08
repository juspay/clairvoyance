#!/usr/bin/env python3
"""Build the tool_based order-confirmation template (DB format) for qwen.

Base: BB_SHOPIFY "order-confirmation" (COD confirm / cancel / address-update).
Every script from the base's <scripts> registry becomes a say block on a tool
as per-language functions (say_x_hi / say_x_en — no language arg, the
name alone determines speech); the four outcome functions (confirm_order,
cancel_order, update_address_and_confirm, user_busy) gain say + end_call
closings. FAQs become say_faq_* tools whose lines steer back to the order.
The prompt shrinks to persona + routing rules — no prose, no scripts registry,
no digit-reading rules (the renderer owns every spoken character).

Tuned for qwen3.8-27b-4bit: short decision-first tool descriptions, small
arg surface (language + verbatim value args only), one fixed gate per rule.

Output: qwen_harness/order-confirmation-toolbased.json
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "order-confirmation-toolbased.json"

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
UNSUP = {
    "type": "string",
    "description": "'none' unless the customer spoke an unsupported "
    "language (Telugu, Kannada, Tamil, Malayalam, Bengali) at any point — "
    "then the 2-letter code (te/kn/ta/ml/bn). Remember the code from the "
    "moment you hear it and keep running the call in Hindi; when the call "
    "ends for a real reason (confirm/cancel/busy), pass the remembered "
    "code here, not 'none'. NEVER end or classify the call just to report "
    "the code, and an unreadable reply is not agreement and not refusal.",
}


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


def outcome(
    name: str,
    description: str,
    hi: str,
    en: str,
    outcome_value: str,
    extra_fields: list[str] | None = None,
    props: dict | None = None,
    required: list | None = None,
) -> dict:
    expected = {"outcome": {"value": outcome_value, "source": "static"}}
    for k in extra_fields or {}:
        expected[k] = {"source": "llm"}
    expected["unsupported_language_code"] = {"source": "llm"}
    full_props = {"unsupported_language_code": UNSUP}
    full_props.update(props or {})
    return tool(
        name,
        description,
        hi,
        en,
        props=full_props,
        required=(required or []) + ["unsupported_language_code"],
        hooks=[{"name": "update_outcome_in_database", "expected_fields": expected}],
        end_call=True,
    )


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


ROLE = """## COD ORDER CONFIRMATION CALL — TOOL-BASED

You are Ram, a male customer-support agent from {shop_name}, calling to confirm the customer's Cash on Delivery order. The greeting already introduced you — never introduce yourself again. You never write speech — your tools ARE your lines. Exactly one tool call per turn.

LANGUAGE PICK: every tool comes in exactly two variants — _hi and _en. Pick the variant for the language the customer is speaking RIGHT NOW: Hindi/Hinglish replies → _hi, English replies → _en. The greeting was ENGLISH, so a first reply in English words ("yes, tell me") → _en, Hindi words ("haan boliye") → _hi. Once the customer settles on a language, keep using that variant; switch only when they explicitly switch. Never mix variants in one turn.

CALL SHAPE:
1. The greeting asked "is this a good time to talk?". A small yes/हाँ/जी/बोलिए means they can talk — that is availability, NEVER an order confirmation. Next turn: say_order_details. If they only greeted back (hello/नमस्ते), call say_availability_check first.
2. THE WORD "CONFIRM": if the customer explicitly says confirm / "confirm kar do" / "order confirm kar do" at ANY point, call confirm_order IMMEDIATELY — no re-reading, no re-asking. This is the only shortcut in the call.
3. say_order_details reads items, price and address and asks the confirmation question. AFTER that full read, a हाँ/जी/yes/ok IS the final confirmation → confirm_order. (A yes BEFORE the read is only availability.)
4. Cancel: if their cancel request already contains a reason ("cancel it, colour pasand nahi aaya") → cancel_order immediately with that reason. If NO reason was given, say_cancel_confirm_once is MANDATORY before cancel_order — never cancel a bare "cancel kar do" directly. Then still want it? → say_cancel_reason_ask → their reply (or refusal) → cancel_order.
5. Address update: field named AND new value given in one breath ("pincode galat hai, sahi hai 560095") → go straight to say_address_read_back with the merged full address. Field named, no value → the matching say_address_update_ask_* tool. Only "address galat hai" → say_address_update_ask_generic. After say_address_read_back, their हाँ/सही/confirm → update_address_and_confirm. ONCE IN THE UPDATE FLOW NEVER call confirm_order — the outcome is always update_address_and_confirm.
6. Busy / driving / meeting / "call later" → say_busy_two_minutes once; if they still refuse → user_busy. Angry or abusive → user_busy immediately.
7. Questions get their FAQ tool: price → say_faq_price, items → say_faq_items, address → say_faq_address, delivery time → say_faq_delivery_time, tracking → say_faq_track, "how to cancel" → say_faq_cancel_how, someone else receiving → say_faq_someone_else, damaged/wrong product → say_faq_return, who/why calling → say_faq_why_calling. Off-topic (politics, cricket, 'are you a robot?') → say_off_topic. Unknown question → say_kb_unknown. Every FAQ line already steers back to the order — a small yes after an FAQ is NOT a confirmation; only the word "confirm" or a yes AFTER say_order_details confirms. And before the order read has happened, a yes to a redirect line is ONLY availability — say_order_details comes first, never confirm_order.
8. Didn't hear / asked to repeat → re-call the tool you called last (it repeats the exact line). A trailing fragment ("तो मतलब वो...", a sentence that stops midway) means they were still speaking — also re-call the last tool. Customer totally silent (no reply at all) → say_idle_reengage; if the pending question was the confirmation question → say_resume_confirm instead.
9. A reply in a script you cannot read (Tamil, Telugu, Kannada, Malayalam, Bengali) is NOT agreement and NOT refusal: keep speaking Hindi (say_availability_check, or re-call the last tool), remember the code, and continue the call. Never pick an outcome tool on such a reply.

You cannot change items, size, colour or price — only confirm, cancel, or update the address. Never invent fees, delivery dates or policies."""

TOOL_DEFS = [
    # ---- speech tools -------------------------------------------------
    tool(
        "say_availability_check",
        "Right after the greeting they only said hello/नमस्ते back, with no "
        "clear availability. Asks if now is a good time. Busy/refusal is NOT "
        "this — that is say_busy_two_minutes.",
        "Hello. क्या अभी बात कर सकते हैं?",
        "Hello. Is this a good time to talk?",
    ),
    tool(
        "say_order_details",
        "They confirmed availability (or you just answered their who/why "
        "question). THE MAIN LINE — reads items, price and address and asks "
        "the confirmation question. Also the repeat when they ask you to say "
        "the details again.",
        "Okay. आपने {items} order किया है. {total_price} का. Address है, "
        "{customer_address}. सब ठीक है ना, मैं order confirm कर दूँ?",
        "Okay. You ordered {items}, for {total_price}. Delivery address is "
        "{customer_address}. Is that all correct so I can confirm your order?",
    ),
    tool(
        "say_busy_two_minutes",
        "They said they are busy / driving / in a meeting / call later. The "
        "one gentle two-minute ask. If they refuse again after this → "
        "user_busy.",
        "अच्छा, बस दो मिनट का काम है — details बता दूँ?",
        "Oh, it's just a two-minute thing — can I quickly share the details?",
    ),
    tool(
        "say_cancel_confirm_once",
        "They said cancel WITHOUT any reason. The gentle one-time check.",
        "हम्म, पक्का cancel कर दूँ? फिर order नहीं जाएगा.",
        "Hmm, sure you want to cancel? Then the order won't go.",
    ),
    tool(
        "say_cancel_reason_ask",
        "They confirmed they want to cancel but gave no reason. Asks why. "
        "Their reply becomes cancel_order's cancellation_reason.",
        "ठीक है. एक छोटा सा reason बता देंगे, cancel क्यों कर रहे हैं?",
        "Okay. Could you share a quick reason — why are you cancelling?",
    ),
    tool(
        "say_address_update_ask_generic",
        "They said the address is wrong / needs a change WITHOUT naming a "
        "field. Asks what to update.",
        "ठीक है. Address में क्या update करना है?",
        "Okay. What would you like to update in the address?",
    ),
    tool(
        "say_address_update_ask_pincode",
        "They said the pincode is wrong but did NOT give the new one.",
        "ठीक है. नया pincode क्या है?",
        "Okay. What is the new pincode?",
    ),
    tool(
        "say_address_update_ask_house",
        "They said the house/door/flat number is wrong but did NOT give the "
        "new one.",
        "ठीक है. नया घर नंबर क्या है?",
        "Okay. What is the new house number?",
    ),
    tool(
        "say_address_update_ask_street",
        "They said the street/gali is wrong but did NOT give the new one.",
        "ठीक है. नई गली/सड़क का नाम क्या है?",
        "Okay. What is the new street name?",
    ),
    tool(
        "say_address_update_ask_landmark",
        "They said the landmark is wrong but did NOT give the new one.",
        "ठीक है. नया landmark क्या है?",
        "Okay. What is the new landmark?",
    ),
    tool(
        "say_address_read_back",
        "You have the complete updated address (they gave the new value with "
        "the complaint, or just answered an ask_* question). Reads it back "
        "and asks if it is correct. Their next हाँ/सही → "
        "update_address_and_confirm.",
        "ठीक है. मैंने updated address note कर लिया है. {updated_address}. "
        "यही सही है ना? Order confirm कर दूँ?",
        "Okay. I have noted the updated address as {updated_address}. Is "
        "that correct? Should I confirm your order?",
        props={
            "updated_address": {
                "type": "string",
                "description": "The COMPLETE updated address: the original "
                "address ({customer_address}) with ONLY their stated changes "
                "applied. Copy the unchanged parts exactly as the payload has "
                "them; never invent, translate or drop anything.",
            }
        },
        required=["updated_address"],
    ),
    tool(
        "say_idle_reengage",
        "The customer has gone silent and no confirmation question was " "pending.",
        "Hello, आप call पे हैं?",
        "Hello, are you there?",
    ),
    tool(
        "say_resume_confirm",
        "The customer went SILENT — no reply at all — while the "
        "order-confirmation question was the pending question. Re-asks just "
        "that question. Any reply of any kind is NEVER this tool: a "
        "हाँ/जी/ok to the pending confirmation question is confirm_order.",
        "सब ठीक है ना, मैं order confirm कर दूँ?",
        "Is everything correct? Shall I confirm your order?",
    ),
    # ---- FAQ tools ----------------------------------------------------
    tool(
        "say_faq_price",
        "They asked the price / total / kitna paisa.",
        "जी. आपके order का price {total_price} है. सब ठीक है ना, मैं order "
        "confirm कर दूँ?",
        "Sure. The price of the order is {total_price}. Is everything "
        "correct so I can confirm your order?",
    ),
    tool(
        "say_faq_items",
        "They asked what items / what they ordered / kya order kiya.",
        "जी. आपने {items} order किया है. सब ठीक है ना, मैं order confirm कर दूँ?",
        "Sure. You have ordered {items}. Is everything correct so I can "
        "confirm your order?",
    ),
    tool(
        "say_faq_address",
        "They asked what the delivery address is.",
        "जी. आपका delivery address है: {customer_address}. यही सही है ना?",
        "Sure. The delivery address is {customer_address}. Is that correct?",
    ),
    tool(
        "say_faq_delivery_time",
        "They asked when the order will arrive / delivery time.",
        "जी. Delivery details के लिए कृपया {shop_name} support से संपर्क करें. "
        "Delivery location के अनुसार समय लगता है. सब ठीक है ना, मैं order "
        "confirm कर दूँ?",
        "Sure. Please contact {shop_name} support for delivery details. The "
        "duration depends on the location. Is everything correct so I can "
        "confirm your order?",
    ),
    tool(
        "say_faq_track",
        "They asked how to track the order.",
        "जी. Order ship होने के बाद आपको SMS या WhatsApp पर tracking details "
        "मिल जाएँगी. सब ठीक है ना, मैं order confirm कर दूँ?",
        "Sure. Once shipped, an SMS or WhatsApp message with tracking "
        "details will be sent. Is everything correct so I can confirm your "
        "order?",
    ),
    tool(
        "say_faq_cancel_how",
        "They asked how they can cancel the order.",
        "जी. आप इसी call पर अपना order cancel कर सकते हैं. Dispatch से पहले "
        "cancel करने पर order नहीं भेजा जाएगा. बताइए, cancel करना है?",
        "Sure. Cancellation can be done during this call. If cancelled "
        "before dispatch, it will not be shipped. So — do you want to cancel?",
    ),
    tool(
        "say_faq_someone_else",
        "They asked if someone else can receive the order.",
        "जी हाँ. Address पर मौजूद कोई भी व्यक्ति order ले सकता है और payment "
        "कर सकता है. सब ठीक है ना, मैं order confirm कर दूँ?",
        "Yes. Any trusted person present at the address may receive the "
        "package and make the payment. Is everything correct so I can "
        "confirm your order?",
    ),
    tool(
        "say_faq_return",
        "They asked what happens if the product is damaged / wrong, or about "
        "returns.",
        "जी. Damaged या गलत item के लिए {shop_name} support team से return या "
        "replacement का request किया जा सकता है. सब ठीक है ना, मैं order "
        "confirm कर दूँ?",
        "Sure. A return or replacement request can be raised with the "
        "{shop_name} support team for damaged or incorrect items. Is "
        "everything correct so I can confirm your order?",
    ),
    tool(
        "say_faq_why_calling",
        "They asked who is calling or why you are calling.",
        "जी. मैं {shop_name} के customer support से बोल रहा हूँ. Cash on "
        "delivery order को verify करने के लिए ही call किया है. बताइए, अभी "
        "बात कर सकते हैं?",
        "Sure. I'm calling from {shop_name} customer support, to verify your "
        "cash on delivery order. So — is now a good time to talk?",
    ),
    tool(
        "say_off_topic",
        "They asked something not related to the order, items, address or "
        "price at all.",
        "हम्म, मैं तो बस order confirm, cancel या address update में मदद कर "
        "सकता हूँ. सब ठीक है ना, मैं order confirm कर दूँ?",
        "Hmm, I can really only help with confirming, cancelling, or "
        "updating the address. Is everything correct so I can confirm your "
        "order?",
    ),
    tool(
        "say_kb_unknown",
        "An order-related question none of the FAQ tools answer and the "
        "knowledge base does not cover.",
        "उम्म. इसकी मुझे जानकारी नहीं है. मैं तो बस आपका order confirm करने के "
        "लिए call कर रहा हूँ. सब ठीक है ना, मैं order confirm कर दूँ?",
        "Hmm. I don't have that information. I'm just calling to confirm "
        "your order. Is everything correct so I can confirm your order?",
    ),
    # ---- outcome tools ------------------------------------------------
    outcome(
        "confirm_order",
        "The customer confirmed the order: either an explicit 'confirm' at "
        "ANY point, or a हाँ/जी/yes/ok/ठीक है said AFTER say_order_details "
        "finished — including right after an FAQ line, because every FAQ "
        "line ends with the confirmation question (or after "
        "say_address_read_back → use update_address_and_confirm instead). "
        "BEFORE say_order_details has run this call, a yes is ONLY "
        "availability — never this tool; say_order_details comes first. "
        "Speaks the thank-you closing and ends the call.",
        "Thank you. आपका order confirm हो गया है. Have a nice day.",
        "Thank you. Your order is confirmed. Have a nice day.",
        "CONFIRM",
    ),
    outcome(
        "cancel_order",
        "The customer wants to cancel AND (the reason was already given with "
        "the request, OR they confirmed say_cancel_confirm_once and answered "
        "say_cancel_reason_ask, OR they refused to give a reason after "
        "being asked). Speaks the cancellation closing and ends the call.",
        "ठीक है. आपका order cancel हो गया है. Have a nice day.",
        "Alright. Your order has been cancelled. Have a nice day.",
        "CANCEL",
        extra_fields=["cancellation_reason"],
        props={
            "cancellation_reason": {
                "type": "string",
                "description": "Why they are cancelling, in their own words "
                "(keep Hindi words Hindi, English words English — do not "
                "translate). 'No specific reason provided' if they refused "
                "to give one.",
            }
        },
        required=["cancellation_reason"],
    ),
    outcome(
        "update_address_and_confirm",
        "The customer confirmed the read-back of the updated address "
        "(हाँ/सही after say_address_read_back). The ONLY outcome allowed "
        "once the update flow started — never confirm_order there. Speaks "
        "the updated-and-confirmed closing and ends the call.",
        "Thank you. आपका delivery address update हो गया है और आपका order "
        "confirm हो गया है. Have a nice day.",
        "Thank you. Your delivery details have been updated and your order "
        "is confirmed. Have a nice day.",
        "ADDRESS_UPDATED",
        extra_fields=["updated_address"],
        props={
            "updated_address": {
                "type": "string",
                "description": "The COMPLETE updated address you read back "
                "and they confirmed — exactly the one from the last "
                "say_address_read_back call. Copy it unchanged.",
            }
        },
        required=["updated_address"],
    ),
    outcome(
        "user_busy",
        "The customer stayed busy after say_busy_two_minutes, or is angry / "
        "abusive, or asked to call later a second time, or this is the "
        "WRONG PERSON — they are not the customer / wrong number / the "
        "customer is not there. A 'नहीं' alone is never a cancel: 'नहीं, मैं "
        "Rahul नहीं हूँ' is wrong-person (this tool), 'नहीं, order नहीं "
        "चाहिए' is the cancel ladder. Speaks the polite closing and ends "
        "the call.",
        "Thank you. Have a nice day.",
        "Thank you. Have a nice day.",
        "BUSY",
    ),
]

# One function per text per language — the name alone determines speech.
TOOLS = [variant for fn in TOOL_DEFS for variant in split_languages(fn)]

template = {
    "id": "7c1f9a2e-4b3d-4e8a-9f60-1d2e3f4a5b60",
    "reseller_id": "rnr",
    "merchant_id": "test-shopify",
    "name": "order-confirmation-toolbased",
    "is_active": True,
    "supported_channels": ["voice"],
    "secrets": None,
    "telephony_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "outbound_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "description": (
        "tool_based conversion of the Shopify COD order-confirmation flow, "
        "tuned for qwen3.8-27b-4bit: every spoken script (order read, cancel "
        "ladder, address-update ladder, FAQs, closings) ships as a per-language "
        "function (say_x_hi / say_x_en — no language arg, the name alone "
        "determines speech); outcomes (CONFIRM / CANCEL / ADDRESS_UPDATED / "
        "BUSY) speak their own closings and end the call. The model only routes and "
        "passes verbatim value args."
    ),
    "expected_payload_schema": {
        "items": {"type": "any"},
        "shop_name": {"type": "string", "function": "string_to_lowercase"},
        "total_price": {"type": "number", "function": "indian_number_to_speech"},
        "customer_name": {"type": "string"},
        "customer_address": {"type": "string"},
        "customer_mobile_number": {
            "type": "string",
            "function": ["extract_10_digit_mobile", "digits_to_speech"],
        },
    },
    "expected_callback_response_schema": {
        "updated_address": {"type": "string", "optional": True},
        "cancellation_reason": {"type": "string", "optional": True},
        "unsupported_language_code": {"type": "string", "optional": True},
    },
    "configurations": {
        "llm_configurations": QWEN_LLM,
        "tts_configuration": TTS,
        "initial_greeting": (
            "Hi {customer_name}. I'm Ram from {shop_name}. I'm calling to "
            "confirm your order. Is this a good time to talk?"
        ),
        "user_idle_configuration": {
            "enabled": True,
            "timeout": 10,
            "idle_message": (
                "The user has been quiet for a while. Call say_idle_reengage "
                "(or say_resume_confirm if the pending question was the "
                "order-confirmation question)."
            ),
            "max_retries": 2,
        },
        "dial_tone": True,
        "enable_background_sound": False,
        "background_sound_volume": 2,
        "enable_text_input": True,
        "interruption": {"mode": "enabled", "min_words": 1},
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
                "han",
                "ha",
                "ji",
                "jee",
                "acha",
                "accha",
                "theek",
                "thik",
                "right",
                "sahi",
                "बोलिए",
                "बताइए",
                "ओके",
                "हैलो",
                "हेलो",
                "हलो",
                "हाँ",
                "हां",
                "हा",
                "जी",
                "हाँजी",
                "हांजी",
                "हम्म",
                "हूँ",
                "अच्छा",
                "ठीक",
                "ठीक है",
                "सही",
                "यस",
                "राइट",
                "अरे",
                "क्या",
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
                "post_actions": [],
                "role_messages": [{"role": "system", "content": ROLE}],
                "task_messages": [
                    {
                        "role": "developer",
                        "content": (
                            "Order: {items}, price {total_price}, address "
                            "{customer_address}, customer {customer_name}, "
                            "shop {shop_name}. Stage machine: availability -> "
                            "order read -> (confirm | cancel ladder | address "
                            "ladder | FAQ detours) -> one outcome. One tool "
                            "per turn."
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
