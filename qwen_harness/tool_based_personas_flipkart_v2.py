"""Persona suite for the bilingual flipkart-emi-dropoff-recovery-toolbased
template (qwen_harness/make_qwen_flipkart_v2.py).

Covers, from all angles: the Hindi happy path, English switching and the
language lock, every hold-back card, off-topic + bot questions, mumbles and
fragments, mid-call hello re-call, and all six outcome tools with their
LLM-argument contracts. Payload values are the spoken-word forms (amounts as
words) so speech_contains checks see exactly what a live call would speak
and what gets warmed into DragonTTS.
"""

from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "flipkart-emi-dropoff-recovery-toolbased.json"
)

PAYLOAD = {
    "customer_name": "Rohit Kumar",
    "product_name": "Samsung Galaxy S24, 8GB RAM",
    "lender": "Fibe",
    "approved_loan_amount": "seventy five thousand rupees",
    "credit_limit": "one hundred thousand rupees",
    "applicable_tenures": "3, 6, 9, 12 months",
    "no_cost_emi_tenures": "3, 6 months",
    "no_cost_emi_applicable": "yes",
    "downpayment_amount": "four thousand nine hundred ninety nine rupees",
    "pending_steps": "auto-pay setup and agreement signing",
    "customer_mobile_number": "9999999999",
}

PERSONAS = [
    {
        "name": "hi_commit_evening",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ, मैं रोहित बोल रहा हूँ",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "कैसे पूरा करना है?",
                "expect": {"expect_tool": "guide", "expect_language": "hi"},
            },
            {
                "say": "ठीक है, मैं आज शाम कर दूँगा",
                "expect": {
                    "expect_tool": "commit",
                    "expect_language": "hi",
                    "expect_arg_contains": {"completion_timeline": "शाम"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "en_switch_commit",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "Yes, this is Rohit. Can we talk in English?",
                "expect": {"expect_tool": "offer", "expect_language": "en"},
            },
            {
                "say": "Yes, tell me how to complete it",
                "expect": {"expect_tool": "guide", "expect_language": "en"},
            },
            {
                "say": "I will finish it tonight",
                "expect": {
                    "expect_tool": "commit",
                    "expect_language": "en",
                    "expect_arg_contains": {"completion_timeline": "tonight"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_decline_after_reason_and_card",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलो",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "नहीं नहीं, मत करना",
                "expect": {"expect_tool": "reason", "expect_language": "hi"},
            },
            {
                "say": "पैसे की तंगी है अभी",
                "expect": {"expect_tool": "loan", "expect_language": "hi"},
            },
            {
                "say": "ना भी, रहने दो",
                "expect": {
                    "expect_tool": "decline",
                    "expect_language": "hi",
                    "expect_arg_contains": {"drop_off_reason": "तंगी"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "en_decline_bought_elsewhere",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "Yes, this is Rohit speaking",
                "expect": {"expect_tool": "offer", "expect_language": "en"},
            },
            {
                "say": "Not interested, I already bought it from a local store",
                "expect": {
                    "expect_tool": "decline",
                    "expect_language": "en",
                    "expect_arg_contains": {"drop_off_reason": "store"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "stuck_kyc_error",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बताइए",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "पर जब मैं KYC करता हूँ तो हर बार error आ जाता है",
                "expect": {
                    "expect_tool": "stuck",
                    "expect_language": "hi",
                    "expect_arg_contains": {"issue_description": "error"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "already_done_yesterday",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "अरे मैंने तो कल ही सब पूरा कर दिया था",
                "expect": {
                    "expect_tool": "done",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "busy_driving_evening_callback",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ... नहीं अभी नहीं, मैं गाड़ी चला रहा हूँ, शाम को call करो",
                "expect": {
                    "expect_tool": "busy",
                    "expect_language": "hi",
                    "expect_arg_contains": {"callback_note": "शाम"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "wrong_number",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "ये गलत नंबर है, रोहित यहाँ नहीं रहते",
                "expect": {
                    "expect_tool": "wrong",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "offtopic_then_bot_question",
        "language": "hi",
        "expect_end": False,
        "steps": [
            {
                "say": "हाँ बोलो",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "अच्छा सुनो, आज का क्रिकेट मैच किसने जीता?",
                "expect": {"expect_tool": "misc", "expect_language": "hi"},
            },
            {
                "say": "अरे बताओ ना... खैर, तुम रोबोट हो क्या?",
                "expect": {"expect_tool": "bot", "expect_language": "hi"},
            },
            {
                "say": "अच्छा ठीक है, कैसे करना है बताओ",
                "expect": {"expect_tool": "guide", "expect_language": "hi"},
            },
        ],
    },
    {
        "name": "tenure_down_guide_commit",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "EMI कितनी बनेगी और कितने महीने के लिए?",
                "expect": {"expect_tool": "tenure", "expect_language": "hi"},
            },
            {
                "say": "और downpayment कितना देना होगा?",
                "expect": {"expect_tool": "down", "expect_language": "hi"},
            },
            {
                "say": "चलो ठीक है, बताओ कैसे करना है",
                "expect": {"expect_tool": "guide", "expect_language": "hi"},
            },
            {
                "say": "हो गया, order place हो गया",
                "expect": {
                    "expect_tool": "commit",
                    "expect_language": "hi",
                    "expect_arg_contains": {"completion_timeline": "during call"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "safety_fraud_otp",
        "language": "hi",
        "expect_end": False,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "ये fraud तो नहीं है? OTP माँगोगे क्या?",
                "expect": {"expect_tool": "safe", "expect_language": "hi"},
            },
            {
                "say": "हम्म ठीक है... बताओ कैसे करना है",
                "expect": {"expect_tool": "guide", "expect_language": "hi"},
            },
        ],
    },
    {
        "name": "nocost_no_card",
        "language": "hi",
        "expect_end": False,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "अरे EMI पर ब्याज तो बहुत लगेगा ना? Hidden charges?",
                "expect": {"expect_tool": "nocost", "expect_language": "hi"},
            },
            {
                "say": "और मेरे पास card भी नहीं है",
                "expect": {"expect_tool": "nocost", "expect_language": "hi"},
            },
            {
                "say": "चलो ठीक है, बता दो कैसे करना है",
                "expect": {"expect_tool": "guide", "expect_language": "hi"},
            },
        ],
    },
    {
        "name": "no_time_then_decline",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "अभी time नहीं है मेरे पास, बहुत लंबा process होगा",
                "expect": {"expect_tool": "quick", "expect_language": "hi"},
            },
            {
                "say": "नहीं, बाद में देखेंगे",
                "expect": {
                    "expect_tool": "decline",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "autopay_and_cancel",
        "language": "hi",
        "expect_end": False,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "ये auto-pay क्या होता है? पैसे अपने आप कट जाएंगे?",
                "expect": {"expect_tool": "cancel", "expect_language": "hi"},
            },
            {
                "say": "क्या मैं इसे बाद में रद्द कर सकता हूँ?",
                "expect": {"expect_tool": "cancel", "expect_language": "hi"},
            },
            {
                "say": "ठीक है, बताओ कैसे करना है",
                "expect": {"expect_tool": "guide", "expect_language": "hi"},
            },
        ],
    },
    {
        "name": "mumble_and_fragment",
        "language": "hi",
        "expect_end": False,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "ह्म्म्म्",
                "expect": {"expect_tool": "unclear", "expect_language": "hi"},
            },
            {
                "say": "तो मैं",
                "expect": {"expect_tool": "unclear", "expect_language": "hi"},
            },
            {
                "say": "अरे मैं कह रहा था कि बताओ कैसे करना है",
                "expect": {"expect_tool": "guide", "expect_language": "hi"},
            },
        ],
    },
    {
        "name": "midcall_hello_recalls_last_tool",
        "language": "hi",
        "expect_end": False,
        "steps": [
            {
                "say": "हाँ बोलो",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "हेलो?",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "हाँ सुन रहा हूँ, बताओ",
                "expect": {"expect_tool": "guide", "expect_language": "hi"},
            },
        ],
    },
    {
        "name": "en_full_flow_with_cards",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "Yeah, Rohit here. Please continue in English.",
                "expect": {"expect_tool": "offer", "expect_language": "en"},
            },
            {
                "say": "How much is the downpayment?",
                "expect": {"expect_tool": "down", "expect_language": "en"},
            },
            {
                "say": "Okay, guide me through the app",
                "expect": {"expect_tool": "guide", "expect_language": "en"},
            },
            {
                "say": "Done, I completed the payment just now",
                "expect": {
                    "expect_tool": "commit",
                    "expect_language": "en",
                    "expect_arg_contains": {"completion_timeline": "during call"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "wrong_number_english",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "Wrong number, no Rohit lives here",
                "expect": {
                    "expect_tool": "wrong",
                    "expect_language": "en",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "busy_english_meeting",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "Yes, but I am in a meeting. Call me after 7 pm",
                "expect": {
                    "expect_tool": "busy",
                    "expect_language": "en",
                    "expect_arg_contains": {"callback_note": "7"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "mixed_words_stay_hindi",
        "language": "hi",
        "expect_end": False,
        "steps": [
            {
                "say": "हाँ",
                "expect": {"expect_tool": "offer", "expect_language": "hi"},
            },
            {
                "say": "Okay okay, तो बताइए कैसे करना है",
                "expect": {"expect_tool": "guide", "expect_language": "hi"},
            },
        ],
    },
    {
        "name": "why_are_you_calling",
        "language": "hi",
        "expect_end": False,
        "steps": [
            {
                "say": "कौन हो तुम? क्यों call किया है?",
                "expect": {"expect_tool": "why", "expect_language": "hi"},
            },
            {
                "say": "अच्छा ठीक है, बताओ आगे",
                "expect": {
                    "expect_tool_any": ["offer", "guide", "again"],
                    "expect_language": "hi",
                },
            },
        ],
    },
]
