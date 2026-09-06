"""Edge-case personas for the tool_based driver-not-active template (Kannada).

Covers: double question in the first reply, anger with no content, politics,
repeat of the why question, silence after the pitch (idle_message injection
→ pitch_no_response → driver_responded carry-forward), earnings doubt,
call-back request, number-privacy and why-calling variants.
"""

from pathlib import Path

from qwen_harness.tool_based_personas_driver import PAYLOAD, TEMPLATE_PATH

IDLE_MSG = (
    "Driver has gone quiet. Call greeting_no_response if the "
    "pitch has not been spoken yet, pitch_no_response if it has."
)

PERSONAS = [
    {
        "name": "edge_kn_first_reply_two_questions",
        "expect_end": True,
        "steps": [
            {
                "say": "ಯಾರಿದ್ದಾರಾ? ಏನಾದರೂ ವಿಷಯ ಇದೆಯಾ?",
                "expect": {
                    "expect_tool_any": ["say_reintroduce_and_pitch", "say_pitch"]
                },
            },
            {
                "say": "ಸರಿ ಸರ್, ಮಾಡ್ತೀನಿ",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
    {
        "name": "edge_kn_angry_no_content",
        "expect_end": "any",
        "steps": [
            {
                "say": "ಬೇಡ ಬೇಡ ಸರ್, ಈ call ಬೇಡ, ತೊಂದರೆ ಕೊಡಬೇಡಿ!",
                "expect": {
                    "expect_tool_any": ["driver_busy_no_time", "driver_says_no"]
                },
            },
        ],
    },
    {
        "name": "edge_kn_politics_after_pitch",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ ಸರ್", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ಈ ಸರ್ಕಾರದ ಬಗ್ಗೆ ಏನು ಅನ್ನುತ್ತೀರಿ?",
                "expect": {
                    "expect_tool_any": [
                        "say_off_topic",
                        "say_repeat_offer",
                        "say_repeat_earnings",
                    ]
                },
            },
            {
                "say": "ಸರಿ ಸರ್, ಮಾಡ್ತೀನಿ",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
    {
        "name": "edge_kn_repeat_why_question",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ", "expect": {"expect_tool": "say_pitch"}},
            {"say": "ಇಲ್ಲ ಸರ್", "expect": {"expect_tool": "driver_says_no"}},
            {
                "say": "ಏನು? ಇನ್ನೊಮ್ಮೆ ಕೇಳಿ ಸರ್",
                "expect": {"expect_tool": "driver_says_no"},
            },
            {
                "say": "ವಾಹನ service ನಲ್ಲಿ ಇದೆ ಸರ್",
                "expect": {
                    "expect_tool": "driver_shares_issue",
                    "expect_arg_equals": {"reason_category": "VEHICLE_ISSUE"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_kn_silence_after_pitch",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ ಸರ್", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": IDLE_MSG,
                "expect": {
                    "expect_tool": "pitch_no_response",
                    "speech_contains": "line",
                },
            },
            {
                "say": "ಇದೀನಿ ಸರ್, ಇದ್ದೆ",
                "expect": {
                    "expect_tool_any": ["driver_responded", "driver_says_yes"],
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "edge_kn_earnings_doubt",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ನಿಜವಾಗ್ಲೂ ಅಷ್ಟು ದುಡಿಮೆ ಆಗುತ್ತಾ?",
                "expect": {
                    "expect_tool_any": ["say_repeat_earnings", "say_repeat_offer"]
                },
            },
            {
                "say": "ಓಕೆ ಸರ್, ಮಾಡ್ತೀನಿ",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
    {
        "name": "edge_kn_call_tomorrow",
        "expect_end": True,
        "steps": [
            {
                "say": "ಈಗ ಕೆಲಸದಲ್ಲಿದ್ದೀನಿ ಸರ್, ನಾಳೆ call ಮಾಡಿ",
                "expect": {"expect_tool": "driver_busy_no_time", "ends_call": True},
            },
        ],
    },
    {
        "name": "edge_kn_number_privacy",
        "expect_end": True,
        "steps": [
            {
                "say": "ನನ್ನ number ಯಾರು ಕೊಟ್ಟರು ನಿಮಗೆ?",
                "expect": {
                    "expect_tool_any": [
                        "say_off_topic",
                        "say_reintroduce_and_pitch",
                        "say_pitch",
                    ]
                },
            },
            {"say": "ಸರಿ, ಹೇಳಿ", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ಮಾಡ್ತೀನಿ ಸರ್",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
    {
        "name": "edge_kn_why_calling_variant",
        "expect_end": True,
        "steps": [
            {
                "say": "ಯಾಕೆ call ಮಾಡಿದ್ರಿ ಸರ್?",
                "expect": {
                    "expect_tool_any": ["say_pitch", "say_reintroduce_and_pitch"]
                },
            },
            {
                "say": "ಆಗುತ್ತೆ, ಮಾಡ್ತೀನಿ",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
]
