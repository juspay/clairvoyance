"""Personas for the tool_based abandoned-checkout template (qwen suite).

PAYLOAD mirrors the loader's post-transform values (cart_total as
indian_number_to_speech text). Cart has 2 distinct products so the
cart_reference arg should carry both names.
"""

from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent / "abandoned-checkout-toolbased.json"

PAYLOAD = {
    "shop_name": "urbantrends",
    "customer_name": "priya",
    "cart_total": "three thousand two hundred and ninety-nine rupees",
    "cart_items_summary": "Running Shoes (x1), Cotton Kurta (x2)",
    "customer_mobile_number": "nine eight one two three four five six seven eight",
}

PERSONAS = [
    {
        "name": "hi_payment_failed_recovered",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ, payment fail हो गया था, दो बार try किया",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": "हाँ, फिर से try कर लूँगी",
                "expect": {
                    "expect_tool": "high_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "PAYMENT_FAILED"},
                    "ends_call": True,
                    "speech_contains": "purchase complete",
                },
            },
        ],
    },
    {
        "name": "hi_after_consent_then_payment",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {
                    "expect_tool": "say_after_consent",
                    "expect_language": "hi",
                    "expect_arg_contains": {"cart_reference": "Running Shoes"},
                },
            },
            {
                "say": "payment नहीं हुआ था, UPI में problem आई थी",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": "हाँ, अब try करूँगी",
                "expect": {
                    "expect_tool": "high_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "PAYMENT_FAILED"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_price_high_firm",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "price ज़्यादा लग रही थी",
                "expect": {"expect_tool": "say_price_probe", "expect_language": "hi"},
            },
            {
                "say": "product अच्छा है पर price ही ज़्यादा है",
                "expect": {
                    "expect_tool": "say_price_value_pitch",
                    "expect_language": "hi",
                },
            },
            {
                "say": "नहीं, अभी भी बहुत ज़्यादा लग रहा है",
                "expect": {
                    "expect_tool": "say_final_product_pitch",
                    "expect_language": "hi",
                },
            },
            {
                "say": "नहीं, really नहीं चाहिए अभी",
                "expect": {
                    "expect_tool": "low_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "PRICE_TOO_HIGH"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_price_high_converted",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "price ज़्यादा है",
                "expect": {"expect_tool": "say_price_probe", "expect_language": "hi"},
            },
            {
                "say": "price ही high लग रहा है, budget की दिक्कत नहीं है",
                "expect": {
                    "expect_tool": "say_price_value_pitch",
                    "expect_language": "hi",
                },
            },
            {
                "say": "हम्म, ठीक है, try कर लेती हूँ",
                "expect": {
                    "expect_tool": "high_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "PRODUCT_DETAIL_GAP"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_trust_concern_converted",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "store थोड़ा नया लग रहा है, fake तो नहीं है?",
                "expect": {
                    "expect_tool": "say_trust_concern_pitch",
                    "expect_language": "hi",
                },
            },
            {
                "say": "अच्छा, तो ठीक है, try कर सकती हूँ",
                "expect": {
                    "expect_tool": "high_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "PRODUCT_DETAIL_GAP"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_not_interested_size_doubt",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "इंटरेस्ट नहीं है अभी",
                "expect": {
                    "expect_tool": "say_deflection_probe",
                    "expect_language": "hi",
                    "expect_arg_contains": {"cart_reference": "Running Shoes"},
                },
            },
            {
                "say": "size का doubt है, कौन सा लूँ पता नहीं चल रहा",
                "expect": {"expect_tool": "say_size_fit_help", "expect_language": "hi"},
            },
            {
                "say": "हाँ ठीक है, एक बार try कर लेती हूँ",
                "expect": {
                    "expect_tool": "high_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "SIZE_FIT_CONCERN"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_return_concern",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "return होता है ना इसमें? बदलने में दिक्कत होती है ना",
                "expect": {
                    "expect_tool": "say_return_exchange_help",
                    "expect_language": "hi",
                },
            },
            {
                "say": "ओके, तो order कर देती हूँ",
                "expect": {
                    "expect_tool": "high_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {
                        "abandonment_reason": "WARRANTY_RETURN_CONCERN"
                    },
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_just_browsing_ladder",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "कुछ खास नहीं, ऐसे ही देख रही थी",
                "expect": {
                    "expect_tool": "say_random_browsing_pitch",
                    "expect_language": "hi",
                    "expect_arg_contains": {"cart_reference": "Running Shoes"},
                },
            },
            {
                "say": "नहीं, अभी इनकी ज़रूरत नहीं है",
                "expect": {
                    "expect_tool_any": [
                        "say_deflection_probe",
                        "say_general_product_pitch",
                    ],
                    "expect_language": "hi",
                    "expect_arg_contains": {"cart_reference": "Running Shoes"},
                },
            },
            {
                "say": "नहीं, try नहीं करना अभी",
                "expect": {
                    "expect_tool": "say_final_product_pitch",
                    "expect_language": "hi",
                },
            },
            {
                "say": "नहीं, बस ऐसे ही देख रही थी, अभी नहीं चाहिए",
                "expect": {
                    "expect_tool": "low_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "JUST_BROWSING"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_vague_probing_technical",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "कुछ समझ नहीं आया, ऐसे ही छोड़ दिया",
                "expect": {
                    "expect_tool": "say_intent_probing",
                    "expect_language": "hi",
                },
            },
            {
                "say": "website धीमी चल रही थी, payment page खुल ही नहीं रहा था",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": "हाँ, अब फिर try करूँगी",
                "expect": {
                    "expect_tool": "high_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "TECHNICAL_ISSUE"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_discount_ask",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "अगर कोई discount मिल जाए तो कर लूँ",
                "expect": {
                    "expect_tool": "say_discount_response",
                    "expect_language": "hi",
                },
            },
            {
                "say": "हम्म ठीक है, बिना discount के भी कर लेती हूँ",
                "expect": {
                    "expect_tool": "high_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "WANTS_DISCOUNT"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_already_purchased",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ वो order हो गया था, बाद में complete कर दिया",
                "expect": {
                    "expect_tool": "already_purchased",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_busy",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "अभी meeting में हूँ, बाद में कॉल करें",
                "expect": {
                    "expect_tool": "user_busy",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "en_payment_failed",
        "language": "en",
        "expect_end": True,
        "steps": [
            {
                "say": "yes, the payment failed while I was trying",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "en"},
            },
            {
                "say": "sure, I'll try again today",
                "expect": {
                    "expect_tool": "high_intent_recovery",
                    "expect_language": "en",
                    "expect_arg_equals": {"abandonment_reason": "PAYMENT_FAILED"},
                    "ends_call": True,
                    "speech_contains": "complete the purchase",
                },
            },
        ],
    },
    {
        "name": "hi_bought_elsewhere",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "इंटरेस्ट नहीं है, मैंने दूसरी जगह से order कर दिया",
                "expect": {
                    "expect_tool": "say_deflection_probe",
                    "expect_language": "hi",
                },
            },
            {
                "say": "हाँ, Amazon से कर लिया, सस्ता मिल गया था",
                "expect": {
                    "expect_tool": "low_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {
                        "abandonment_reason": "ALREADY_PURCHASED_ELSEWHERE"
                    },
                    "ends_call": True,
                },
            },
        ],
    },
]
