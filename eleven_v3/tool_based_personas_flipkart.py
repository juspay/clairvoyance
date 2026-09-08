"""Persona matrix for the tool_based Flipkart template
(eleven_v3/flipkart-recovery-toolbased.json).

Prod-shaped scenarios distilled from the v5 23-persona matrix: all five
customer states through the full guidance ladder, hold-back cards, soft-yes,
second-no, busy, wrong person, stuck-after-retry, already-completed, bare
hello, safety suspicion, no-cost=standard selector, EMI plan question.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "qwen_harness"
    / "flipkart-recovery-toolbased.json"
)

PAYLOAD = {
    "customer_name": "राहुल",
    "product_name": "Samsung Galaxy S24",
    "customer_current_state": "OFFERED",
    "lender": "Kotak",
    "no_cost_emi_applicable": "yes",
    "no_cost_emi_tenures": "3 and 6 months",
    "credit_limit": "₹80,000",
    "approved_loan_amount": "₹65,000",
    "emi_plan_summary": "3 months: ₹21,667/month at 0% interest; 6 months: ₹10,834/month at 0% interest",
    "applicable_tenures": "3, 6, 9 and 12 months",
    "downpayment_amount": "₹4,333",
    "pending_steps": "KYC",
}

E = "expect_tool"


def _override(**kw) -> dict:
    payload = dict(PAYLOAD)
    payload.update(kw)
    return payload


GUIDE_STEPS = [
    {"say": "हाँ, बताइए कैसे करना है", "expect": {E: "say_step_open_app"}},
    {"say": "खुल गया", "expect": {E: "say_step_cart_order"}},
    {"say": "payment page आ गया", "expect": {E: "say_step_select_emi"}},
    {"say": "select हो गया", "expect": {E: "say_step_select_plan"}},
]

PERSONAS = [
    {
        "name": "offered_full_journey",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोल रहा हूँ",
                "expect": {
                    E: "say_hook_offered",
                    "expect_language": "hi",
                    "speech_contains": "KYC",
                },
            },
            *GUIDE_STEPS,
            {"say": "हो गया", "expect": {E: "say_step_redirect_lender"}},
            {
                "say": "lender page खुल गया",
                "expect": {E: "say_step_kyc", "speech_contains": "selfie"},
            },
            {
                "say": "KYC done दिख रहा है, order place हो गया",
                "expect": {
                    E: "app_return_committed",
                    "expect_arg_contains": {"completion_timeline": "call"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "eligibility_full_journey",
        "payload": _override(
            customer_current_state="ELIGIBILITY_CHECKED",
            pending_steps="Plan selection and agreement",
        ),
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {
                    E: "say_hook_eligibility_checked",
                    "speech_contains": "credit line",
                },
            },
            *GUIDE_STEPS,
            {
                "say": "plan select हो गया",
                "expect": {
                    E: "say_step_agreement",
                    "speech_contains": "Agree and Sign",
                },
            },
            {
                "say": "sign हो गया, OTP डाल दिया, order placed",
                "expect": {E: "app_return_committed", "ends_call": True},
            },
        ],
    },
    {
        "name": "kyc_completed_full_journey",
        "payload": _override(
            customer_current_state="KYC_COMPLETED", pending_steps="Autopay"
        ),
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ",
                "expect": {E: "say_hook_kyc_completed", "speech_contains": "auto-pay"},
            },
            *GUIDE_STEPS,
            {"say": "हो गया", "expect": {E: "say_step_redirect_lender"}},
            {
                "say": "page आ गया",
                "expect": {E: "say_step_autopay", "speech_contains": "auto-pay"},
            },
            {
                "say": "authorize हो गया, order placed दिख रहा है",
                "expect": {E: "app_return_committed", "ends_call": True},
            },
        ],
    },
    {
        "name": "mandate_completed_full_journey",
        "payload": _override(
            customer_current_state="MANDATE_COMPLETED", pending_steps="Agreement"
        ),
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बताइए",
                "expect": {
                    E: "say_hook_mandate_completed",
                    "speech_contains": "Agree and Sign",
                },
            },
            *GUIDE_STEPS,
            {"say": "हो गया", "expect": {E: "say_step_redirect_lender"}},
            {"say": "agreement दिख रहा है", "expect": {E: "say_step_agreement"}},
            {
                "say": "OTP verify हो गया, order placed",
                "expect": {E: "app_return_committed", "ends_call": True},
            },
        ],
    },
    {
        "name": "agreement_signed_full_journey",
        "payload": _override(
            customer_current_state="AGREEMENT_SIGNED", pending_steps="Downpayment"
        ),
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ",
                "expect": {
                    E: "say_hook_agreement_signed",
                    "speech_contains": "downpayment",
                },
            },
            *GUIDE_STEPS,
            {"say": "हो गया", "expect": {E: "say_step_redirect_lender"}},
            {
                "say": "downpayment वाला page आ गया है, क्या करूँ?",
                "expect": {E: "say_step_downpayment", "speech_contains": "4,333"},
            },
            {
                "say": "payment हो गया, order placed",
                "expect": {E: "app_return_committed", "ends_call": True},
            },
        ],
    },
    {
        "name": "first_no_card_then_yes",
        "expect_end": True,
        "steps": [
            {"say": "हाँ बोल रहा हूँ", "expect": {E: "say_hook_offered"}},
            {
                "say": "नहीं नहीं, सोचता हूँ अभी",
                "expect": {E: "say_card_no_cost", "speech_contains": "no-cost"},
            },
            {
                "say": "अच्छा है क्या... चलो करते हैं",
                "expect": {
                    E: "say_step_open_app",
                    "expect_tool_any": ["say_step_open_app", "say_return_to_app"],
                },
            },
            {
                "say": "मैंने app में सब कर दिया, order place हो गया",
                "expect": {E: "app_return_committed", "ends_call": True},
            },
        ],
    },
    {
        "name": "second_no_not_interested",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {E: "say_hook_offered"}},
            {
                "say": "नहीं, नहीं करना मुझे",
                "expect": {
                    E: "say_card_no_cost",
                    "expect_tool_any": [
                        "say_card_no_cost",
                        "say_card_effort",
                        "say_return_to_app",
                    ],
                },
            },
            {
                "say": "ज़रूरत नहीं है मुझे EMI की",
                "expect": {
                    E: "not_interested",
                    "expect_arg_contains": {"drop_off_reason": "ज़रूरत"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "monthly_emi_card_then_commit_later",
        "expect_end": True,
        "steps": [
            {"say": "हाँ बोलिए", "expect": {E: "say_hook_offered"}},
            {
                "say": "monthly EMI कितनी बनेगी?",
                "expect": {
                    E: "say_card_monthly_emi",
                    "expect_language": "hi",
                    "speech_contains": "21,667",
                },
            },
            {
                "say": "ओके... बाद में कर लूँगा शाम को",
                "expect": {
                    E: "app_return_committed",
                    "expect_arg_contains": {"completion_timeline": "शाम"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "budget_card_soft_exit",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {E: "say_hook_offered"}},
            {
                "say": "अभी पैसे नहीं हैं, budget tight है",
                "expect": {E: "say_card_amount", "speech_contains": "65,000"},
            },
            {
                "say": "हम्म, अभी नहीं कर सकता",
                "expect": {
                    E: "say_return_to_app",
                    "expect_tool_any": [
                        "say_return_to_app",
                        "say_card_effort",
                        "not_interested",
                    ],
                },
            },
            {
                "say": "नहीं, बाद में",
                "expect": {
                    E: "app_return_committed",
                    "expect_tool_any": ["app_return_committed", "not_interested"],
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "busy_meeting",
        "expect_end": True,
        "steps": [
            {"say": "हाँ बोलो", "expect": {E: "say_hook_offered"}},
            {
                "say": "अभी meeting में हूँ, बाद में कॉल करो",
                "expect": {
                    E: "user_busy",
                    "expect_arg_contains": {"callback_note": "कॉल"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "wrong_number_first_turn",
        "expect_end": True,
        "steps": [
            {
                "say": "ये नंबर गलत है, मैं राहुल नहीं हूँ",
                "expect": {E: "wrong_person", "ends_call": True},
            },
        ],
    },
    {
        "name": "stuck_after_retry",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {E: "say_hook_offered"}},
            {
                "say": "अभी बताइए, करता हूँ",
                "expect": {
                    E: "say_step_open_app",
                    "expect_tool_any": ["say_step_open_app", "say_return_to_app"],
                },
            },
            {
                "say": "app खुल ही नहीं रहा, बार-बार error आ रहा है",
                "expect": {E: "say_suggest_retry"},
            },
            {
                "say": "फिर भी नहीं खुल रहा",
                "expect": {
                    E: "customer_stuck",
                    "expect_arg_contains": {"issue_description": "app"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "already_completed_final_step",
        "payload": _override(
            customer_current_state="AGREEMENT_SIGNED", pending_steps="Downpayment"
        ),
        "expect_end": True,
        "steps": [
            {"say": "हाँ बोल रहा हूँ", "expect": {E: "say_hook_agreement_signed"}},
            {
                "say": "downpayment कर दिया था मैंने, order place हो गया है",
                "expect": {
                    E: "already_completed",
                    "expect_arg_contains": {"claimed_step": "downpayment"},
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "bare_hello_then_hook",
        "expect_end": "any",
        "steps": [
            {
                "say": "हेलो",
                "expect": {E: "say_repeat_greeting", "speech_contains": "प्रियंका"},
            },
            {"say": "हाँ बोलिए", "expect": {E: "say_hook_offered"}},
            {
                "say": "पता नहीं, बाद में",
                "expect": {
                    E: "say_card_effort",
                    "expect_tool_any": [
                        "say_card_effort",
                        "say_card_no_cost",
                        "say_return_to_app",
                        "user_busy",
                        "app_return_committed",
                    ],
                },
            },
        ],
    },
    {
        "name": "safety_suspicion_card",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {E: "say_hook_offered"}},
            {
                "say": "आप कौन हो? ये तो fraud लग रहा है",
                "expect": {E: "say_card_safety", "speech_contains": "OTP"},
            },
            {"say": "हम्म ठीक है", "expect": {E: "say_return_to_app"}},
            {
                "say": "नहीं मैं नहीं करूँगा",
                "expect": {
                    E: "say_card_no_cost",
                    "expect_tool_any": [
                        "say_card_no_cost",
                        "say_card_effort",
                        "say_return_to_app",
                    ],
                },
            },
            {
                "say": "मैंने कहा नहीं, बंद करो",
                "expect": {E: "not_interested", "ends_call": True},
            },
        ],
    },
    {
        "name": "standard_no_cost_selector",
        "payload": _override(no_cost_emi_applicable="no"),
        "expect_end": True,
        "steps": [
            {
                "say": "हाँ बोलिए",
                "expect": {E: "say_hook_offered", "expect_language": "hi_std"},
            },
            {
                "say": "इतना लंबा process है? मेरे पास इतना time नहीं है",
                "expect": {E: "say_card_effort"},
            },
            {
                "say": "ठीक है, शाम को कर दूँगा ज़रूर",
                "expect": {
                    E: "app_return_committed",
                    "expect_arg_contains": {"completion_timeline": "शाम"},
                    "ends_call": True,
                },
            },
        ],
    },
]
