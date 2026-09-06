"""Edge-case personas for the tool_based abandoned-checkout template.

Covers: interruption with a which-products question, robot/identity and
out-of-scope questions, anger, silence/idle, mid-ladder concern switch,
mid-call English switch, repeat requests, number-privacy question,
browsing→price combo ladder and discount-then-refuse.
"""

from pathlib import Path

from qwen_harness.tool_based_personas_abandoned import PAYLOAD, TEMPLATE_PATH

IDLE_MSG = "The user has been quiet for a while. Call say_idle_reengage."

PERSONAS = [
    {
        "name": "edge_hi_interrupt_which_products",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ, पर पहले बताओ कौन से products की बात कर रहे हो?",
                "expect": {
                    "expect_tool": "say_after_consent",
                    "expect_language": "hi",
                    "expect_arg_contains": {"cart_reference": "Running Shoes"},
                },
            },
            {
                "say": "अच्छा, payment fail हो गया था",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": "हाँ, try करूँगी",
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
        "name": "edge_hi_are_you_robot",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "आप robot हो क्या? आवाज़ तो इंसान जैसी लग रही है",
                "expect": {
                    "expect_tool_any": ["say_off_topic", "say_intent_probing"],
                    "expect_language": "hi",
                },
            },
            {
                "say": "payment नहीं हुआ था",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": "हाँ, try करूँगी",
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
        "name": "edge_hi_angry_stop_calling",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "क्या बकवास है! बार-बार call क्यों करते हो, number हटाओ मेरा",
                "expect": {
                    "expect_tool": "user_busy",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_hi_cricket_out_of_scope",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "कल का match देखा? पहले बताओ परिणाम क्या हुआ",
                "expect": {
                    "expect_tool_any": ["say_off_topic", "say_intent_probing"],
                    "expect_language": "hi",
                },
            },
            {
                "say": "अच्छा छोड़ो, असल में payment issue था",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": "हाँ, फिर try करूँगी",
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
        "name": "edge_hi_idle_mid_call",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "payment fail हो गया था",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": IDLE_MSG,
                "expect": {
                    "expect_tool": "say_idle_reengage",
                    "expect_language": "hi",
                    "speech_contains": "line",
                },
            },
            {
                "say": "हाँ हूँ, try करूँगी",
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
        "name": "edge_hi_concern_switch_mid_ladder",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "price ज़्यादा थी",
                "expect": {"expect_tool": "say_price_probe", "expect_language": "hi"},
            },
            {
                "say": "असल में price ठीक है पर store पर trust नहीं है",
                "expect": {
                    "expect_tool": "say_trust_concern_pitch",
                    "expect_language": "hi",
                },
            },
            {
                "say": "अच्छा, तो ठीक है, try कर लेती हूँ",
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
        "name": "edge_hi_en_switch_mid_call",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ, but can we talk in English?",
                "expect": {
                    "expect_tool": "say_after_consent",
                    "expect_language": "en",
                    "expect_arg_contains": {"cart_reference": "Running Shoes"},
                },
            },
            {
                "say": "the payment failed during checkout",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "en"},
            },
            {
                "say": "sure, I'll retry now",
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
        "name": "edge_hi_repeat_request",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "payment fail हो गया था",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": "ज़रा दोबारा समझाइए, समझ नहीं आया",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": "हाँ ठीक है, try करूँगी",
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
        "name": "edge_hi_number_privacy",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "आपको मेरा number कहाँ से मिला?",
                "expect": {
                    "expect_tool_any": ["say_off_topic", "say_trust_concern_pitch"],
                    "expect_language": "hi",
                },
            },
            {
                "say": "ठीक है, असल में payment issue था",
                "expect": {"expect_tool": "say_issue_help", "expect_language": "hi"},
            },
            {
                "say": "हाँ, try करूँगी",
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
        "name": "edge_hi_browsing_then_price",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "ऐसे ही देख रही थी",
                "expect": {
                    "expect_tool": "say_random_browsing_pitch",
                    "expect_language": "hi",
                    "expect_arg_contains": {"cart_reference": "Running Shoes"},
                },
            },
            {
                "say": "नहीं, price ज़्यादा है इसकी",
                "expect": {
                    "expect_tool_any": ["say_price_value_pitch", "say_price_probe"],
                    "expect_language": "hi",
                },
            },
            {
                "say": "नहीं, अभी नहीं चाहिए इतने में",
                "expect": {
                    "expect_tool_any": [
                        "say_price_value_pitch",
                        "say_general_product_pitch",
                        "say_final_product_pitch",
                    ],
                    "expect_language": "hi",
                },
            },
            {
                "say": "नहीं, बहुत महंगा है, नहीं लूँगी",
                "expect": {
                    "expect_tool_any": [
                        "say_final_product_pitch",
                        "low_intent_recovery",
                    ],
                    "expect_language": "hi",
                },
            },
            {
                "say": "नहीं, बस इतने पैसे नहीं दूँगी, thank you",
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
        "name": "edge_hi_discount_then_refuse",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "discount मिलेगा तो ही लूँगी",
                "expect": {
                    "expect_tool": "say_discount_response",
                    "expect_language": "hi",
                },
            },
            {
                "say": "नहीं, बिना discount के नहीं चाहिए",
                "expect": {
                    "expect_tool_any": [
                        "say_final_product_pitch",
                        "say_general_product_pitch",
                    ],
                    "expect_language": "hi",
                },
            },
            {
                "say": "नहीं, discount नहीं तो purchase नहीं",
                "expect": {
                    "expect_tool": "low_intent_recovery",
                    "expect_language": "hi",
                    "expect_arg_equals": {"abandonment_reason": "WANTS_DISCOUNT"},
                    "ends_call": True,
                },
            },
        ],
    },
]
