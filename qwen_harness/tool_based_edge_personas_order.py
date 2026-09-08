"""Edge-case personas for the tool_based order-confirmation template.

Covers: mid-flow interruptions/barge-in detours, out-of-scope and identity
questions, unsupported language then code capture, anger, half-heard
fragments, idle/silence (the runtime injects the template's idle_message as
a user turn — simulated the same way here), repeats, mid-call language
switch, transfer demands, unknown questions, multi-field address updates
and cancel-then-change-mind.
"""

from pathlib import Path

from qwen_harness.tool_based_personas_order import PAYLOAD, TEMPLATE_PATH

IDLE_MSG = (
    "The user has been quiet for a while. Call say_idle_reengage "
    "(or say_resume_confirm if the pending question was the "
    "order-confirmation question)."
)

PERSONAS = [
    {
        "name": "edge_hi_interrupt_read_with_price_q",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "रुकिए रुकिए, total कितने का है?",
                "expect": {
                    "expect_tool": "say_faq_price",
                    "expect_language": "hi",
                    "speech_contains": "rupees",
                },
            },
            {
                "say": "और address क्या है?",
                "expect": {
                    "expect_tool": "say_faq_address",
                    "expect_language": "hi",
                    "speech_contains": "MG Road",
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
        "name": "edge_hi_are_you_robot_pre_read",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "आप robot हो क्या?",
                "expect": {
                    "expect_tool_any": [
                        "say_off_topic",
                        "say_kb_unknown",
                        "say_faq_why_calling",
                    ],
                    "expect_language": "hi",
                },
            },
            {
                "say": "हाँ बताइए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "हाँ, सही है",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_hi_politics_out_of_scope",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "इस सरकार के बारे में क्या सोचते हो?",
                "expect": {
                    "expect_tool_any": ["say_off_topic", "say_kb_unknown"],
                    "expect_language": "hi",
                },
            },
            {
                "say": "अच्छा छोड़ो, ये order ठीक है, confirm कर दो",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_hi_tamil_then_hindi",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "வணக்கம், நான் தமிழ் மட்டும் பேசுவேன்",
                "expect": {
                    "expect_tool_any": [
                        "say_availability_check",
                        "say_faq_why_calling",
                    ],
                    "expect_language": "hi",
                },
            },
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "हाँ ठीक है",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "expect_arg_equals": {"unsupported_language_code": "ta"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_hi_angry_abusive",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "ये बकवास कॉल बंद करो! बार-बार तंग क्यों करते हो",
                "expect": {
                    "expect_tool": "user_busy",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_hi_half_heard_fragment",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "तो मतलब वो...",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "हाँ, अब समझ आया, ठीक है",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_hi_idle_after_read",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": IDLE_MSG,
                "expect": {
                    "expect_tool": "say_resume_confirm",
                    "expect_language": "hi",
                    "speech_contains": "confirm कर दूँ",
                },
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
        "name": "edge_hi_en_switch_mid_call",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "wait, can you speak in English please?",
                "expect": {
                    "expect_tool_any": [
                        "say_order_details",
                        "say_resume_confirm",
                        "say_faq_price",
                        "say_faq_items",
                    ],
                    "expect_language": "en",
                },
            },
            {
                "say": "yes, all correct, go ahead",
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
        "name": "edge_hi_transfer_request",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "किसी इंसान से बात कराओ, आपसे नहीं बात करनी",
                "expect": {
                    "expect_tool_any": ["say_off_topic", "say_kb_unknown"],
                    "expect_language": "hi",
                },
            },
            {
                "say": "हाँ बताइए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "हाँ, confirm कर दो",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_hi_other_order_question",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "मेरा पिछला order आया ही नहीं, उसका क्या करेंगे?",
                "expect": {
                    "expect_tool_any": [
                        "say_kb_unknown",
                        "say_off_topic",
                        "say_faq_track",
                    ],
                    "expect_language": "hi",
                },
            },
            {
                "say": "अच्छा ठीक है, ये वाला confirm कर दो",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_hi_multi_field_address",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "pincode और घर का नंबर दोनों बदलने हैं",
                "expect": {
                    "expect_tool_any": [
                        "say_address_update_ask_pincode",
                        "say_address_update_ask_house",
                        "say_address_update_ask_generic",
                    ],
                    "expect_language": "hi",
                },
            },
            {
                "say": "pincode 560095",
                "expect": {
                    "expect_tool_any": [
                        "say_address_update_ask_house",
                        "say_address_read_back",
                    ],
                    "expect_language": "hi",
                },
            },
            {
                "say": "घर का नंबर 21",
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
                    "expect_arg_contains": {"updated_address": "21"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_hi_cancel_then_change_mind",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "say_order_details", "expect_language": "hi"},
            },
            {
                "say": "नहीं, cancel कर दो",
                "expect": {
                    "expect_tool": "say_cancel_confirm_once",
                    "expect_language": "hi",
                },
            },
            {
                "say": "हम्म... नहीं रहने दो, confirm ही कर दो",
                "expect": {
                    "expect_tool": "confirm_order",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
]
