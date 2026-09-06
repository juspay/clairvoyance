"""Personas for the tool_based Namma Yatri driver template (qwen suite).

Kannada driver replies, mirroring how Soniox kn-IN transcribes them
(Kanglish mix). No language args on this template — expectations check the
outcome routing, the ready_to_start enum, the verbatim driver_number and
the exact closing lines.
"""

from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent / "driver-not-active-toolbased.json"

PAYLOAD = {
    "driver_name": "Ravi Kumar",
    "customer_mobile_number": "9845012345",
}

PERSONAS = [
    {
        "name": "kn_yes_after_pitch",
        "expect_end": True,
        "steps": [
            {
                "say": "ಹೌದು ಹೇಳಿ",
                "expect": {"expect_tool": "say_pitch", "speech_contains": "ready na"},
            },
            {
                "say": "ಹೌದು ಮಾಡ್ತೀನಿ",
                "expect": {
                    "expect_tool": "driver_says_yes",
                    "expect_arg_equals": {"driver_number": "9845012345"},
                    "expect_arg_contains": {"ready_response_words": "ಮಾಡ್ತೀನಿ"},
                    "ends_call": True,
                    "speech_contains": "Thank you",
                },
            },
        ],
    },
    {
        "name": "kn_busy_driving",
        "expect_end": True,
        "steps": [
            {
                "say": "ಈಗ ಡ್ರೈವ್ ಮಾಡ್ತಿದ್ದೀನಿ ಸರ್, ಆಮೇಲೆ call ಮಾಡಿ",
                "expect": {
                    "expect_tool": "driver_busy_no_time",
                    "expect_arg_equals": {"driver_number": "9845012345"},
                    "ends_call": True,
                    "speech_contains": "ಧನ್ಯವಾದ",
                },
            },
        ],
    },
    {
        "name": "kn_who_is_this",
        "expect_end": True,
        "steps": [
            {
                "say": "ಯಾರು ಮಾತಾಡ್ತಿದ್ದೀರಾ?",
                "expect": {
                    "expect_tool": "say_reintroduce_and_pitch",
                    "speech_contains": "ಯಾತ್ರಿ ಕಡೆ",
                },
            },
            {
                "say": "ಸರಿ ಸರ್, ಮಾಡ್ತೀನಿ",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
    {
        "name": "kn_no_with_reason",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ ಸರ್", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ಇಲ್ಲ ಸರ್, pickup ತುಂಬಾ ದೂರ ಇರುತ್ತೆ",
                "expect": {
                    "expect_tool": "driver_says_no",
                    "expect_arg_equals": {
                        "ready_to_start": "NO",
                        "driver_number": "9845012345",
                    },
                    "speech_contains": "issue",
                },
            },
            {
                "say": "ಹೌದು ಸರ್, pickup ಬಹಳ ದೂರ ಇರುತ್ತೆ, ಸಣ್ಣ rides ಗೆ ತುಂಬಾ ಸಮಯ ಹೋಗುತ್ತೆ",
                "expect": {
                    "expect_tool": "driver_shares_issue",
                    "expect_arg_equals": {
                        "reason_category": "PICKUP_DISTANCE",
                        "driver_number": "9845012345",
                    },
                    "ends_call": True,
                    "speech_contains": "issue note",
                },
            },
        ],
    },
    {
        "name": "kn_no_vague_vehicle",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ಗೊತ್ತಿಲ್ಲ ಸರ್",
                "expect": {
                    "expect_tool": "driver_says_no",
                    "expect_arg_equals": {"ready_to_start": "NOT_SURE"},
                },
            },
            {
                "say": "ವಾಹನ service ನಲ್ಲಿ ಇದೆ, ಇನ್ನು ಒಂದು ವಾರ ಆಗುತ್ತೆ",
                "expect": {
                    "expect_tool": "driver_shares_issue",
                    "expect_arg_equals": {"reason_category": "VEHICLE_ISSUE"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "kn_repeat_offer",
        "expect_end": True,
        "steps": [
            {"say": "ಹೌದು ಹೇಳಿ", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ಏನಂದ್ರಿ? offer ಏನು ಅಂದ್ರಿ?",
                "expect": {
                    "expect_tool": "say_repeat_offer",
                    "speech_contains": "free subscription",
                },
            },
            {
                "say": "ಹೌದು ಸರಿ, ಮಾಡ್ತೀನಿ",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
    {
        "name": "kn_repeat_earnings",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ದುಡಿಮೆ ಎಷ್ಟು ಆಗುತ್ತೆ ಅಂದ್ರಿ?",
                "expect": {
                    "expect_tool": "say_repeat_earnings",
                    "speech_contains": "ಸಾವಿರ",
                },
            },
            {
                "say": "ಓಕೆ ಸರ್, ಮಾಡ್ತೀನಿ",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
    {
        "name": "kn_no_issue",
        "expect_end": True,
        "steps": [
            {"say": "ಆಯ್ತು, ಹೇಳಿ", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ಇಲ್ಲ ಸರ್, ಆಗಲ್ಲ",
                "expect": {
                    "expect_tool": "driver_says_no",
                    "expect_arg_equals": {"ready_to_start": "NO"},
                },
            },
            {
                "say": "ಏನೂ ಇಲ್ಲ ಸರ್, ಸುಮ್ಮನೆ",
                "expect": {"expect_tool": "driver_no_issue", "ends_call": True},
            },
        ],
    },
    {
        "name": "kn_declines_to_share",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ", "expect": {"expect_tool": "say_pitch"}},
            {"say": "ಬೇಡ ಸರ್, ಈಗ ಬೇಡ", "expect": {"expect_tool": "driver_says_no"}},
            {
                "say": "ಬೇಡ ಬೇಡ, ಇಟ್ಟುಬಿಡಿ",
                "expect": {
                    "expect_tool": "driver_declines_to_share",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "kn_what_about",
        "expect_end": True,
        "steps": [
            {"say": "ಏನು ವಿಷಯ?", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ಸರಿ, ಮಾಡ್ತೀನಿ",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
    {
        "name": "kn_angry_app_issue_before_pitch",
        "expect_end": True,
        "steps": [
            {
                "say": "ಏನ್ ಇದು, ಎರಡು ದಿನದಿಂದ app ಗೆ login ಆಗುತ್ತಿಲ್ಲ, ಬೇಜಾರ್ ಆಗಿದೆ!",
                "expect": {
                    "expect_tool": "driver_says_no",
                    "expect_arg_equals": {"ready_to_start": "NOT_ASKED"},
                },
            },
            {
                "say": "ಹೌದು ಸರ್, app open ಆಗುತ್ತಿಲ್ಲ, ಎರಡು ದಿನದಿಂದ",
                "expect": {
                    "expect_tool": "driver_shares_issue",
                    "expect_arg_equals": {"reason_category": "APP_OR_TECHNICAL"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "kn_mmm_yes",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ ಸರ್", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ಮ್ಮ್",
                "expect": {"expect_tool": "driver_says_yes", "ends_call": True},
            },
        ],
    },
    {
        "name": "kn_dues_concern",
        "expect_end": True,
        "steps": [
            {"say": "ಹೇಳಿ ಸರ್", "expect": {"expect_tool": "say_pitch"}},
            {
                "say": "ಇಲ್ಲ ಸರ್, ಮೊದಲು ನನ್ನ subscription dues clear ಮಾಡೋಣ",
                "expect": {
                    "expect_tool": "driver_says_no",
                    "expect_arg_equals": {"ready_to_start": "NO"},
                },
            },
            {
                "say": "subscription dues pending ಸರ್, ಅದು clear ಆದ ಮೇಲೆ rides ನೋಡ್ತೀನಿ",
                "expect": {
                    "expect_tool": "driver_shares_issue",
                    "expect_arg_equals": {"reason_category": "PAYMENT_CONCERN"},
                    "ends_call": True,
                },
            },
        ],
    },
]
