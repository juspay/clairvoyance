#!/usr/bin/env python3
"""Build the tool_based Flipkart EMI recovery template from flipkart-recovery-v5.json.

The v5 prompt's speech inventory (per-state hooks, 6 hold-back cards, app
guidance steps, first-turn greeting rules, per-outcome closings) moves into
say blocks on tools. The model keeps only the decision logic: which tool
matches the customer's latest message, which selector variant fits the
payload (no-cost vs standard), and verbatim arg extraction.

Selector convention for variant lines: the `language` argument picks the
utterance set. "hi" = Hindi with the no-cost offer line, "hi_std" = Hindi
with the standard (no no-cost mention) offer line, "en" = English.

Output: eleven_v3/flipkart-recovery-toolbased.json
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent
SRC = REPO / "flipkart-recovery-v5.json"
OUT = REPO / "flipkart-recovery-toolbased.json"

src = json.loads(SRC.read_text())
src_funcs = {f["function_name"]: f for f in src["flow"]["nodes"][0]["functions"]}


def lang_prop(extra_selectors: bool = False) -> dict:
    enums = ["hi", "hi_std", "en"] if extra_selectors else ["hi", "en"]
    return {
        "type": "string",
        "enum": enums,
        "description": "Language the customer is speaking right now"
        + (
            ". Use hi_std (not hi) when {no_cost_emi_applicable} is not yes."
            if extra_selectors
            else ""
        ),
    }


def say(utterances: dict, *, end_call: bool = False, first: bool = True) -> dict:
    block: dict = {
        "utterances": utterances,
        "phrasing": "first" if first else "random",
        "default_language": "hi",
    }
    if end_call:
        block["end_call"] = True
    return block


def tool(
    name,
    description,
    hi,
    *,
    props=None,
    required=None,
    end_call=False,
    selector_variants=None,
    first=True,
):
    """selector_variants: (hi_line, hi_std_line, en_line) when the line
    branches on no_cost_emi_applicable."""
    if selector_variants:
        hi_line, hi_std_line, en_line = selector_variants
        utterances = {"hi": [hi_line], "hi_std": [hi_std_line], "en": [en_line]}
    else:
        utterances = {"hi": hi}
    properties = dict(props or {})
    extra = selector_variants is not None
    properties.setdefault("language", lang_prop(extra_selectors=extra))
    return {
        "function_name": name,
        "description": description,
        "properties": properties,
        "required": (required or []) + ["language"],
        "transition_to": None,
        "hooks": [],
        "say": say(utterances, end_call=end_call, first=first),
    }


OFFER_NOCOST = (
    "{lender} की तरफ से आपका EMI offer already approved है, और इस पर "
    "({no_cost_emi_tenures} in English words) तक no-cost EMI है — मतलब कोई "
    "extra interest नहीं."
)
OFFER_STD = "{lender} की तरफ से आपका EMI offer already approved है — approval का कोई इंतज़ार नहीं."


def hook(hi_body: str, hi_std_body: str, en_body: str, desc_state: str) -> dict:
    return tool(
        f"say_hook_{desc_state.lower()}",
        f"THE OPENING HOOK for customer_current_state={desc_state}. Call once, "
        "as your first substantive turn after identity is settled (or after "
        "the greeting repeat). Never again.",
        None,
        selector_variants=(hi_body, hi_std_body, en_body),
    )


TOOLS = [
    # ---- first-turn tools ----------------------------------------------
    tool(
        "say_repeat_greeting",
        "FIRST TURN only, when their first words are a bare hello / हाँ? / कौन? "
        "with nothing else: repeat the greeting once. Also after a call "
        "screener, when a human first comes on the line. Say nothing else.",
        [
            "नमस्ते. मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में. क्या मेरी बात {customer_name} से हो रही है?"
        ],
    ),
    tool(
        "say_screening_intro",
        "FIRST TURN only, when the first voice is an automated call screener "
        "(iPhone/assistant asking you to state name and reason). English by "
        "design — pass language=en. After it, WAIT; when a human comes on, "
        "call say_repeat_greeting.",
        ["नमस्ते. मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से."],
    ),
    # ---- hooks per state -------------------------------------------------
    hook(
        f"जी. आपने {{product_name}} के लिए Flipkart EMI पर checkout शुरू किया था. {OFFER_NOCOST} बस lender page पर selfie और Aadhaar KYC बाकी है — दो मिनट का काम, और order place हो जाएगा. क्या मैं आपको अभी app में guide कर दूँ?",
        f"जी. आपने {{product_name}} के लिए Flipkart EMI पर checkout शुरू किया था. {OFFER_STD} बस lender page पर selfie और Aadhaar KYC बाकी है — दो मिनट का काम, और order place हो जाएगा. क्या मैं आपको अभी app में guide कर दूँ?",
        "Yes — you started checkout on Flipkart EMI for your {product_name}. Your EMI offer from {lender} is already approved. Only the selfie and Aadhaar KYC are left on the lender page — two minutes, and the order places. Shall I guide you in the app right now?",
        "OFFERED",
    ),
    hook(
        f"जी. आपके पास Flipkart EMI की {{credit_limit}} की credit line already active है, और आपने {{product_name}} के लिए check भी किया था. {OFFER_NOCOST} बस cart से payment में Flipkart EMI select करके plan चुनना और sign करना बाकी है — KYC कुछ नहीं. क्या मैं आपको अभी app में guide कर दूँ?",
        f"जी. आपके पास Flipkart EMI की {{credit_limit}} की credit line already active है, और आपने {{product_name}} के लिए check भी किया था. {OFFER_STD} बस cart से payment में Flipkart EMI select करके plan चुनना और sign करना बाकी है — KYC कुछ नहीं. क्या मैं आपको अभी app में guide कर दूँ?",
        "Yes — you already have an active {credit_limit} Flipkart EMI credit line, and you checked eligibility for your {product_name}. Only choosing the plan and signing are left — no KYC at all. Shall I guide you in the app right now?",
        "ELIGIBILITY_CHECKED",
    ),
    hook(
        f"जी. आपने {{product_name}} के लिए Flipkart EMI पर checkout शुरू किया था. {OFFER_NOCOST} आपका KYC भी complete हो चुका है — सबसे बड़ा step हो गया. बस auto-pay set up करना बाकी है, एक-दो मिनट का काम. क्या मैं आपको अभी app में guide कर दूँ?",
        f"जी. आपने {{product_name}} के लिए Flipkart EMI पर checkout शुरू किया था. {OFFER_STD} आपका KYC भी complete हो चुका है — सबसे बड़ा step हो गया. बस auto-pay set up करना बाकी है, एक-दो मिनट का काम. क्या मैं आपको अभी app में guide कर दूँ?",
        "Yes — you started checkout on Flipkart EMI for your {product_name}, and your KYC is already complete — the biggest step is done. Only the auto-pay setup is left, one or two minutes. Shall I guide you in the app right now?",
        "KYC_COMPLETED",
    ),
    hook(
        f"जी. आपने {{product_name}} के लिए Flipkart EMI पर checkout शुरू किया था. {OFFER_NOCOST} KYC और auto-pay दोनों हो चुके हैं. बस agreement पर Agree and Sign करके OTP डालना बाकी है — उसके बाद order place. क्या मैं आपको अभी app में guide कर दूँ?",
        f"जी. आपने {{product_name}} के लिए Flipkart EMI पर checkout शुरू किया था. {OFFER_STD} KYC और auto-pay दोनों हो चुके हैं. बस agreement पर Agree and Sign करके OTP डालना बाकी है — उसके बाद order place. क्या मैं आपको अभी app में guide कर दूँ?",
        "Yes — your KYC and auto-pay are both done for the {product_name} checkout. Only Agree and Sign plus the OTP are left, and the order places. Shall I guide you in the app right now?",
        "MANDATE_COMPLETED",
    ),
    hook(
        f"जी. आपने {{product_name}} के लिए Flipkart EMI पर checkout शुरू किया था. {OFFER_NOCOST} आपकी पूरी application complete है — KYC, auto-pay, agreement सब. बस downpayment बाकी है, जिसके बाद order confirm हो जाता है. क्या मैं आपको अभी app में guide कर दूँ?",
        f"जी. आपने {{product_name}} के लिए Flipkart EMI पर checkout शुरू किया था. {OFFER_STD} आपकी पूरी application complete है — KYC, auto-pay, agreement सब. बस downpayment बाकी है, जिसके बाद order confirm हो जाता है. क्या मैं आपको अभी app में guide कर दूँ?",
        "Yes — your whole application is complete — KYC, auto-pay, agreement. Only the downpayment is left, and the order confirms. Shall I guide you in the app right now?",
        "AGREEMENT_SIGNED",
    ),
    # ---- hold-back cards -------------------------------------------------
    tool(
        "say_card_no_cost",
        "CARD 1 — no-cost reminder. Trigger: hesitation, 'sochta hoon', "
        "'mehnga', 'interest lagega', 'kya offer hai'. Only when "
        "{no_cost_emi_applicable} is yes (else use hi_std/en which stay "
        "neutral).",
        None,
        selector_variants=(
            f"आपको card की ज़रूरत भी नहीं — Flipkart EMI पर ({{no_cost_emi_tenures}} in English words) तक no-cost EMI already है. मतलब सिर्फ product का price, कोई extra interest नहीं.",
            "Flipkart EMI पर ये already approved है — सिर्फ product का price, कोई extra charge नहीं.",
            "You don't even need a card — Flipkart EMI already gives you no-cost EMI on this purchase. Only the product price, no extra interest.",
        ),
    ),
    tool(
        "say_card_amount",
        "CARD 2 — approved amount. Trigger: 'paise nahi hain', 'budget', "
        "'kitna milega', 'loan kitna', affordability worries.",
        [
            "आपके लिए {approved_loan_amount} का loan already approve है इस purchase के लिए — और आपकी total credit limit {credit_limit} है. Approval का कोई इंतज़ार नहीं, बस complete करना है."
        ],
    ),
    tool(
        "say_card_monthly_emi",
        "CARD 3 — monthly EMI / tenures. Trigger: 'kitne mahine', 'EMI kitni "
        "banegi', 'monthly kitna' when they mean the instalment. hi = speak "
        "the plan summary; hi_std = when {emi_plan_summary} is empty.",
        None,
        selector_variants=(
            "{emi_plan_summary}. Exact figure app में plan select करते ही confirm हो जाएगी.",
            "आपके पास ({applicable_tenures} in English words) की tenure के options हैं. Exact monthly EMI आपको app में plan select करते ही दिख जाएगी.",
            "You have tenure options of ({applicable_tenures} in English words). The exact monthly EMI shows in the app the moment you select a plan.",
        ),
    ),
    tool(
        "say_clarify_emi_question",
        "When it is unclear whether they mean the loan amount or the monthly "
        "EMI, clarify first — one line, then wait.",
        ["आपका मतलब loan amount से है या monthly EMI से?"],
    ),
    tool(
        "say_card_downpayment",
        "CARD 4 — downpayment. Trigger: asked about downpayment, 'pehle kitna "
        "dena hoga'.",
        [
            "Downpayment सिर्फ {downpayment_amount} का है. वो होते ही order confirm हो जाता है."
        ],
    ),
    tool(
        "say_card_safety",
        "CARD 5 — safety / lender. Trigger: suspicion, 'fraud', 'kaun ho aap', "
        "'kaunsa bank', 'safe hai kya'.",
        [
            "ये loan {lender} की तरफ से है, और पूरा process Flipkart app के अंदर ही होता है. मैं आपसे कोई OTP, PIN या payment details कभी नहीं माँगूँगी — जो भी OTP आएगा वो आप app में ही डालेंगे. आप चाहें तो call रख कर सीधे app खोल कर भी देख सकते हैं."
        ],
    ),
    tool(
        "say_card_effort",
        "CARD 6 — effort / time. Trigger: 'time nahi hai', 'baad mein', "
        "'lamba process hoga'.",
        [
            "जो हो चुका है वो save है, दोबारा कुछ नहीं करना. जो बचा है वो सिर्फ दो मिनट का है — cart से payment में Flipkart EMI select करते ही आप वहीं से continue कर पाएंगे."
        ],
    ),
    tool(
        "say_return_to_app",
        "THE RETURN LINE after any card, when NOT yet in guidance: bring them "
        "back to the app question.",
        ["तो क्या मैं आपको अभी app में guide कर दूँ? दो मिनट में हो जाएगा."],
    ),
    tool(
        "say_check_step",
        "THE RETURN LINE when ALREADY mid-guidance: ask about the current "
        "step, never about opening the app again.",
        ["तो बताइए, हो गया?"],
    ),
    # ---- app guidance steps (one per turn, in order) ---------------------
    tool(
        "say_step_open_app",
        "GUIDANCE STEP A — always the first guidance step.",
        ["सबसे पहले Flipkart app खोलिए. बताइए जब खुल जाए."],
    ),
    tool(
        "say_step_cart_order",
        "GUIDANCE STEP B — after the app is open.",
        [
            "अब cart में जाइए — आपका {product_name} वहीं होगा. Place order पर tap कीजिए, और बताइए जब payment page आ जाए."
        ],
    ),
    tool(
        "say_step_select_emi",
        "GUIDANCE STEP C — on the payment page.",
        [
            "Payment page पर EMI section में Flipkart EMI select कीजिए — वहाँ {lender} का नाम दिखेगा. बताइए जब select हो जाए."
        ],
    ),
    tool(
        "say_step_select_plan",
        "GUIDANCE STEP D — after selecting Flipkart EMI.",
        [
            "अब EMI plan चुनिए — no-cost वाला plan, जैसे ({no_cost_emi_tenures} in English words) का — और Select plan and continue दबाइए. बताइए जब हो जाए."
        ],
    ),
    tool(
        "say_step_redirect_lender",
        "ALWAYS between say_step_select_plan and the state step — EXCEPT for "
        "ELIGIBILITY_CHECKED, where the plan leads straight to "
        "say_step_agreement (no redirect, no lender page). Never call this "
        "for ELIGIBILITY_CHECKED.",
        ["अब आप {lender} के page पर redirect होंगे — वहाँ जो step बचा है वही खुलेगा."],
    ),
    tool(
        "say_step_kyc",
        "STATE STEP for KYC. Comes ONLY after say_step_select_plan and say_step_redirect_lender — never before the ladder is done.",
        [
            "पहले selfie लीजिए और Accept कीजिए. फिर Aadhaar number डालिए — Aadhaar पर जो OTP आएगा वो वहीं app में डालिए, मुझे नहीं बताना है. बताइए जब KYC done दिखे."
        ],
    ),
    tool(
        "say_step_autopay",
        "STATE STEP for auto-pay. Comes ONLY after say_step_select_plan and say_step_redirect_lender — never before the ladder is done.",
        [
            "अब auto-pay setup आएगा — UPI, debit card या net banking में से एक चुनिए और अपना bank select करके authorize कर दीजिए. इससे EMI हर महीने अपने आप time पर जाएगी. बताइए जब हो जाए."
        ],
    ),
    tool(
        "say_step_agreement",
        "STATE STEP for the agreement. ELIGIBILITY_CHECKED: plan → this step directly. Every other state: plan → say_step_redirect_lender → this step. Never before the ladder is done.",
        [
            "अब Key Fact Statement और loan agreement दिखेगा — नीचे Agree and Sign दबाइए. फिर आपके number पर OTP आएगा, वो app में डाल कर Verify कीजिए. बताइए जब Order placed दिखे."
        ],
    ),
    tool(
        "say_step_downpayment",
        "STATE STEP for the downpayment. Comes ONLY after say_step_select_plan and say_step_redirect_lender — never before the ladder is done.",
        [
            "अब {downpayment_amount} का downpayment page आएगा — UPI या card से pay कीजिए. Payment होते ही Order placed दिखेगा."
        ],
    ),
    tool(
        "say_suggest_retry",
        "They hit an error, the app is not installed, login fails, or a page "
        "will not load: suggest ONE retry. If it still fails after this → "
        "customer_stuck.",
        ["एक बार app बंद करके दोबारा खोल कर देखिए."],
    ),
]

# ---- outcome tools: v5 functions + say closings + end_call -----------------
CLOSINGS = {
    "app_return_committed": {
        "hi": [
            "बहुत बढ़िया. Flipkart app में cart से payment पर Flipkart EMI select करते ही आप वहीं से continue कर पाएंगे. धन्यवाद, आपका दिन शुभ हो."
        ],
        "en": [
            "Great. In the Flipkart app, go to cart, then payment, select Flipkart EMI and you will continue right where you left off. Thank you, have a nice day."
        ],
    },
    "not_interested": {
        "hi": ["ठीक है, कोई बात नहीं. आपके feedback के लिए धन्यवाद. Have a nice day."],
        "en": ["Alright, no problem. Thank you for your feedback. Have a nice day."],
    },
    "customer_stuck": {
        "hi": [
            "मैंने आपकी problem note कर ली है, team इसे जल्दी ठीक करेगी. धन्यवाद. Have a nice day."
        ],
        "en": [
            "I have noted the issue and the team will fix it soon. Thank you. Have a nice day."
        ],
    },
    "already_completed": {
        "hi": [
            "बहुत बढ़िया, फिर आपका order app में confirm दिखेगा. धन्यवाद. Have a nice day."
        ],
        "en": [
            "Perfect, your order will show as confirmed in the app. Thank you. Have a nice day."
        ],
    },
    "user_busy": {
        "hi": ["ठीक है, मैं बाद में call करूँगी. धन्यवाद. Have a nice day."],
        "en": ["Alright, I will call you later. Thank you. Have a nice day."],
    },
    "wrong_person": {
        "hi": ["माफ़ कीजिएगा, गलत नंबर लग गया. Have a nice day."],
        "en": ["Sorry for the trouble, wrong number. Have a nice day."],
    },
}

VERBATIM_ARGS = {
    "callback_note",
    "drop_off_reason",
    "issue_description",
    "claimed_step",
    "completion_timeline",
    "committed_step",
}

for name, closing in CLOSINGS.items():
    v5 = src_funcs[name]
    properties = dict(v5.get("properties", {}))
    for arg_name, prop in properties.items():
        if arg_name in VERBATIM_ARGS and isinstance(prop, dict):
            desc = prop.get("description") or ""
            if "exactly as" not in desc.lower():
                prop["description"] = (
                    desc + " Exactly as the customer said it — keep English "
                    "words English and Hindi words Hindi; never translate or "
                    "summarize."
                ).strip()
    desc = v5["description"]
    if name == "app_return_committed":
        desc += (
            " NEVER while guiding: mid-ladder confirmations (app open, "
            "payment page, EMI selected, plan selected) keep the ladder "
            "going — this tool fires only when the customer reports the "
            "journey DONE (Order placed / final step complete) or promises "
            "to complete it LATER without guidance."
        )
    t = {
        "function_name": name,
        "description": desc,
        "properties": properties,
        "required": list(v5.get("required", [])),
        "transition_to": None,
        "hooks": [],
        "say": say(closing, end_call=True),
    }
    t["properties"]["language"] = lang_prop()
    t["required"] = [r for r in t["required"] if r != "language"] + ["language"]
    TOOLS.append(t)

ROLE = """## FLIPKART EMI RECOVERY — TOOL-BASED

You are प्रियंका, a female Flipkart EMI recovery agent. The greeting has played. Your ONLY job each turn: pick exactly ONE tool whose line answers the customer's latest message. You never write speech — the tool speaks.

CUSTOMER STATE: {customer_current_state} tells you where they stopped. The hook tool matching it (say_hook_*) is your opening move after identity settles. Select hi vs hi_std by {no_cost_emi_applicable}.

FIRST TURN (their first words decide):
- Bare hello / हाँ? / कौन? with nothing else → say_repeat_greeting. Nothing else that turn.
- Automated call screener (state name/reason) → say_screening_intro (language=en), WAIT; when a human comes on → say_repeat_greeting.
- Wrong number / never applied → wrong_person immediately.
- Identity confirmed / invitation to speak → the say_hook_* for {customer_current_state}.

THE YES JOURNEY — the ladder is the SAME for every state and ALWAYS starts at the top:
say_step_open_app → say_step_cart_order → say_step_select_emi → say_step_select_plan → say_step_redirect_lender (skip ONLY for ELIGIBILITY_CHECKED) → the pending state step(s): say_step_kyc / say_step_autopay / say_step_agreement / say_step_downpayment → they report Order placed → app_return_committed (completion_timeline=completed during call).
NEVER JUMP: however much is already done, the customer still re-enters the app through cart — give the ladder steps in strict order, one per turn, advancing only when their reply confirms the current one. A state step is NEVER the first guidance step. If their message reports the FINAL step already done, the app_return_committed call IS the turn — no step announcement.

HOLD-BACK CARDS: one card per turn, only when triggered (each card's description lists its triggers), and never a card the customer did not earn. After a card: say_return_to_app (if not yet guiding) or say_check_step (if mid-guidance).

NO's: first no → a triggered card. Second no after the reason → not_interested with drop_off_reason.
BUSY → user_busy with callback_note (first busy signal, no pushing).
STUCK (error twice, app missing, cannot proceed) → customer_stuck with issue_description.
CLAIMS ALREADY DONE (final step) → already_completed with claimed_step.
UNKNOWN LANGUAGE (not Hindi/English) → any outcome tool with unsupported_language_code set.

MID-CALL hello (हेलो / सुनाई दे रहा है): they are checking the line — re-call the tool you called last; it repeats the exact line.

SAFETY: you never ask for OTP, PIN or payment details (the say lines already promise this). Never claim to be human. Never mention internal systems.
"""

src_cfg = src["configurations"]
template = {
    "template_name": "Flipkart EMI Recovery — tool_based",
    "identifier": "flipkart-recovery-toolbased",
    "is_active": False,
    "description": (
        "tool_based conversion of flipkart-recovery-v5: hooks, cards, guidance "
        "steps and closings are template-owned say blocks; the LLM picks tools "
        "and extracts verbatim args. Variant lines selected via the language "
        "arg (hi / hi_std / en)."
    ),
    "expected_payload_schema": src.get("expected_payload_schema"),
    "example_payload": {
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
    },
    "configurations": {
        "llm_configurations": {
            "provider": "azure",
            "temperature": 0.1,
            "max_tokens": 500,
            "tool_choice": "required",
        },
        "tts_configuration": {
            **src_cfg["tts_configuration"],
            "voice_id": "81xnrLNObbKDEs179emP",
            "model": "eleven_v3_conversational",
        },
        "initial_greeting": src_cfg["initial_greeting"],
        "user_idle_configuration": {
            "enabled": True,
            "timeout": 15,
            "idle_message": "The user has been quiet for a while. Re-call the tool for the current step.",
            "max_retries": 2,
        },
    },
    "flow": {
        "mode": "tool_based",
        "initial_node": "initial",
        "nodes": [
            {
                "node_name": "initial",
                "role_messages": [{"role": "system", "content": ROLE}],
                "task_messages": [
                    {
                        "role": "developer",
                        "content": "STATE={customer_current_state} pending={pending_steps} no_cost={no_cost_emi_applicable} product={product_name} lender={lender}. One tool per turn; the tool speaks.",
                    }
                ],
                "pre_actions": [],
                "post_actions": [],
                "functions": TOOLS,
            }
        ],
    },
}

OUT.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n")
print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(TOOLS)} tools)")
