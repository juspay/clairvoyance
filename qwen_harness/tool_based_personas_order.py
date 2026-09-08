"""Personas for the tool_based order-confirmation template (qwen suite).

PAYLOAD mirrors the loader's post-transform values (total_price arrives as
indian_number_to_speech text, the mobile as digits_to_speech words) — exactly
what a live call's template_vars would hold.
"""

from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent / "order-confirmation-toolbased.json"

PAYLOAD = {
    "shop_name": "stylehub",
    "customer_name": "Rahul",
    "items": "2 Cotton T-Shirt, 1 Denim Jacket",
    "total_price": "two thousand eight hundred and seventy-four rupees",
    "customer_address": "12, MG Road, Bengaluru, 560001",
    "customer_mobile_number": "nine eight seven six five four three two one zero",
}

PERSONAS = [
    {
        "name": "hi_confirm_after_read",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {
                    "expect_tool": "say_order_details",
                    "expect_language": "hi",
                    "speech_contains": "Cotton",
                },
            },
            {
                "say": "हाँ, सब सही है, confirm कर दो",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                    "speech_contains": "confirm हो गया",
                },
            },
        ],
    },
    {
        "name": "hi_explicit_confirm_first_turn",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "haan haan, order confirm kar do",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_cancel_with_reason",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बताइए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "नहीं, cancel कर दीजिए, colour पसंद नहीं आया",
                "expect": {
                    "expect_tool": "cancel_order",
                    "expect_language": "hi",
                    "expect_arg_contains": {"cancellation_reason": "colour"},
                    "ends_call": True,
                    "speech_contains": "cancel हो गया",
                },
            },
        ],
    },
    {
        "name": "hi_cancel_no_reason",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "नहीं चाहिए, cancel कर दो",
                "expect": {
                    "expect_tool": "say_cancel_confirm_once",
                    "expect_language": "hi",
                },
            },
            {
                "say": "हाँ पक्का cancel कर दीजिए",
                "expect": {
                    "expect_tool": "say_cancel_reason_ask",
                    "expect_language": "hi",
                },
            },
            {
                "say": "कोई खास वजह नहीं है",
                "expect": {
                    "expect_tool": "cancel_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_address_update_pincode",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ जी बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "address में pincode गलत है",
                "expect": {
                    "expect_tool": "say_address_update_ask_pincode",
                    "expect_language": "hi",
                },
            },
            {
                "say": "नया pincode 560095 है",
                "expect": {
                    "expect_tool": "say_address_read_back",
                    "expect_language": "hi",
                    "expect_arg_contains": {"updated_address": "560095"},
                },
            },
            {
                "say": "हाँ, यही सही है",
                "expect": {
                    "expect_tool": "update_address_and_confirm",
                    "expect_language": "hi",
                    "expect_arg_contains": {"updated_address": "560095"},
                    "ends_call": True,
                    "speech_contains": "update हो गया",
                },
            },
        ],
    },
    {
        "name": "hi_address_wrong_value_given",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बताइए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "घर का नंबर गलत है, सही नंबर 21 है",
                "expect": {
                    "expect_tool": "say_address_read_back",
                    "expect_language": "hi",
                    "expect_arg_contains": {"updated_address": "21"},
                },
            },
            {
                "say": "हाँ सही है",
                "expect": {
                    "expect_tool": "update_address_and_confirm",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_busy_refuses",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "अभी ड्राइविंग कर रहा हूँ, बाद में कॉल करें",
                "expect": {
                    "expect_tool": "say_busy_two_minutes",
                    "expect_language": "hi",
                },
            },
            {
                "say": "नहीं नहीं, बाद में ही बात करेंगे",
                "expect": {
                    "expect_tool": "user_busy",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_busy_accepts_two_minutes",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "अभी थोड़ा busy हूँ",
                "expect": {
                    "expect_tool": "say_busy_two_minutes",
                    "expect_language": "hi",
                },
            },
            {
                "say": "अच्छा ठीक है, बताइए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "हाँ सही है, confirm कर दो",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_greeting_back_only",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हेलो",
                "expect": {
                    "expect_tool": "say_availability_check",
                    "expect_language": "hi",
                },
            },
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "हाँ, ठीक है",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_asks_price",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "कुल कितने का order है?",
                "expect": {
                    "expect_tool": "say_faq_price",
                    "expect_language": "hi",
                    "speech_contains": "rupees",
                },
            },
            {
                "say": "हाँ ठीक है, confirm कर दो",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_asks_delivery_time",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "order कब तक पहुँचेगा?",
                "expect": {
                    "expect_tool": "say_faq_delivery_time",
                    "expect_language": "hi",
                },
            },
            {
                "say": "ओके, ठीक है",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_why_calling",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "कौन बोल रहा है? क्यों कॉल किया है?",
                "expect": {
                    "expect_tool": "say_faq_why_calling",
                    "expect_language": "hi",
                },
            },
            {
                "say": "अच्छा ठीक है, बताइए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "हाँ सही है",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "en_happy_path",
        "language": "en",
        "expect_end": True,
        "steps": [
            {
                "say": "yes, tell me",
                "expect": {
                    "expect_tool": "say_order_details",
                    "expect_language": "en",
                    "speech_contains": "Denim",
                },
            },
            {
                "say": "yes, everything is correct, go ahead",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "en",
                    "ends_call": True,
                    "speech_contains": "confirmed",
                },
            },
        ],
    },
    {
        "name": "hi_repeat_details",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "क्या? ज़रा दोबारा बताइए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "हाँ ठीक है",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_wrong_person",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "नहीं, मैं Rahul नहीं हूँ, गलत नंबर है",
                "expect": {
                    "expect_tool": "user_busy",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
]
