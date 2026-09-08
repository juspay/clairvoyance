"""Persona matrix for the tool_based redbus template (eleven_v3/redbus-toolbased.json).

Mirrors the prod scenarios the v4 template was hardened against: rating in
digits/words/doubled/english-looking transcription, invalid ratings, why-
calling / trip-details / scale detours, low-rating reason loop, busy and
wrong-person exits, poor-audio turns.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "qwen_harness"
    / "redbus-customer-trip-feedback-toolbased.json"
)

PAYLOAD = {
    "customer_name": "Rhea",
    "bus_operator_name": "ABC travels",
    "source": "Mumbai",
    "destination": "Punjab",
    "customer_mobile_number": "9999999999",
}

E = "expect_tool"

PERSONAS = [
    {
        "name": "happy_5_digits",
        "expect_end": True,
        "steps": [
            {"say": "हाँ बोलिए", "expect": {E: "rating_hi"}},
            {
                "say": "5",
                "expect": {
                    E: "positive_feedback",
                    "expect_arg_equals": {"rating": "5"},
                    "ends_call": True,
                    "speech_contains": "धन्यवाद",
                },
            },
        ],
    },
    {
        "name": "happy_4_hindi_word",
        "expect_end": True,
        "steps": [
            {"say": "जी बोलो", "expect": {E: "rating_hi"}},
            {
                "say": "चार",
                "expect": {
                    E: "positive_feedback",
                    "expect_arg_equals": {"rating": "4"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "doubled_panch",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {E: "rating_hi"}},
            {
                "say": "पाँच पाँच",
                "expect": {
                    E: "positive_feedback",
                    "expect_arg_equals": {"rating": "5"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "english_looking_transcription",
        "expect_end": True,
        "steps": [
            {"say": "बोलिए", "expect": {E: "rating_hi"}},
            {
                "say": "panch",
                "expect": {
                    E: "positive_feedback",
                    "expect_arg_equals": {"rating": "5"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "low_rating_reason_late",
        "expect_end": True,
        "steps": [
            {"say": "हाँ बोलो", "expect": {E: "rating_hi"}},
            {"say": "तीन", "expect": {E: "reason_hi", "speech_contains": "कारण"}},
            {
                "say": "बस एक घंटा लेट थी",
                "expect": {
                    E: "negative_feedback",
                    "expect_arg_equals": {"rating": "3"},
                    "expect_arg_contains": {"feedback": "लेट"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "low_rating_dirty_bus_english_words",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {E: "rating_hi"}},
            {"say": "do", "expect": {E: "reason_hi"}},
            {
                "say": "bus was dirty and seats were broken",
                "expect": {
                    E: "negative_feedback",
                    "expect_arg_equals": {"rating": "2"},
                    "expect_arg_contains": {"feedback": "dirty"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "invalid_then_valid",
        "expect_end": True,
        "steps": [
            {"say": "हाँ बोलिए", "expect": {E: "rating_hi"}},
            {
                "say": "सात",
                "expect": {E: "bad_rating_hi", "speech_contains": "एक से पाँच"},
            },
            {
                "say": "चार",
                "expect": {
                    E: "positive_feedback",
                    "expect_arg_equals": {"rating": "4"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "who_is_calling_detour",
        "expect_end": True,
        "steps": [
            {
                "say": "कौन बोल रहा है?",
                "expect": {E: "why_hi", "speech_contains": "ABC travels"},
            },
            {"say": "हाँ बोलिए", "expect": {E: "rating_hi"}},
            {
                "say": "4",
                "expect": {
                    E: "positive_feedback",
                    "expect_arg_equals": {"rating": "4"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "trip_details_then_reask",
        "expect_end": True,
        "steps": [
            {
                "say": "कौन सी ट्रिप?",
                "expect": {E: "trip_hi", "speech_contains": "Mumbai"},
            },
            {
                "say": "अच्छा",
                "expect": {
                    E: "rating_hi",
                    "expect_tool_any": ["rating_hi", "reask_hi"],
                },
            },
            {
                "say": "चार",
                "expect": {
                    E: "positive_feedback",
                    "expect_arg_equals": {"rating": "4"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "scale_explained_detour",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {E: "rating_hi"}},
            {
                "say": "रेटिंग कैसे देनी है, कौन सा सबसे ज़्यादा?",
                "expect": {E: "scale_hi"},
            },
            {"say": "तीन", "expect": {E: "reason_hi"}},
            {
                "say": "driver behaviour ठीक नहीं था",
                "expect": {
                    E: "negative_feedback",
                    "expect_arg_equals": {"rating": "3"},
                    "expect_arg_contains": {"feedback": "driver"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "busy_first_reply",
        "expect_end": True,
        "steps": [
            {
                "say": "अभी व्यस्त हूँ, बाद में कॉल करो",
                "expect": {
                    E: "user_busy",
                    "ends_call": True,
                    "speech_contains": "आपके समय के लिए",
                },
            },
        ],
    },
    {
        "name": "wrong_number",
        "expect_end": True,
        "steps": [
            {"say": "ये गलत नंबर है", "expect": {E: "user_busy", "ends_call": True}},
        ],
    },
    {
        "name": "half_heard_then_rating",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {E: "rating_hi"}},
            {"say": "तो", "expect": {E: "partial_hi"}},
            {
                "say": "पाँच, बढ़िया थी ट्रिप",
                "expect": {
                    E: "positive_feedback",
                    "expect_arg_equals": {"rating": "5"},
                    "expect_arg_contains": {"feedback": "बढ़िया"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "midcall_hello_repeats_line",
        "expect_end": True,
        "steps": [
            {"say": "हाँ बोलिए", "expect": {E: "rating_hi"}},
            {
                "say": "हेलो?",
                "expect": {
                    E: "rating_hi",
                    "expect_tool_any": ["rating_hi", "reask_hi"],
                },
            },
            {
                "say": "चार",
                "expect": {
                    E: "positive_feedback",
                    "expect_arg_equals": {"rating": "4"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "unclear_then_repeat_then_reask",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {E: "rating_hi"}},
            {
                "say": "mmm hmmm...",
                "expect": {
                    E: "repeat_hi",
                    "expect_tool_any": [
                        "repeat_hi",
                        "unclear_hi",
                        "partial_hi",
                    ],
                },
            },
            {"say": "दो", "expect": {E: "reason_hi"}},
            {
                "say": "AC काम नहीं कर रहा था",
                "expect": {
                    E: "negative_feedback",
                    "expect_arg_equals": {"rating": "2"},
                    "expect_arg_contains": {"feedback": "AC"},
                    "ends_call": True,
                },
            },
        ],
    },
]
