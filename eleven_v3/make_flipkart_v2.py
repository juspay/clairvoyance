"""flipkart-recovery v2 — minimal, evidence-driven fixes from the harness
baselines (Azure 9/13, Grid 6/13). Hindi dialogue phrases stay as-is except
the राम/masculine contradictions, which the feminine cloned voice and the
greeting (प्रियंका बोल रही हूँ) already contradict.

Fixes:
  A. Gender/name consistency — the template was ported from a male agent
     (राम) but the voice, greeting and screening line are प्रियंका (female).
  B. busy / wrong-person paths told the bot to speak BEFORE calling, which
     contradicts SPEAK OR CALL and double-speaks the closing line.
  C. Objection handling had no exit for a soft yes after a hold-back card.
  D. Completion-during-call: models spoke while calling app_return_committed.
  E. Function descriptions: lead with the zero-words rule where missing.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = json.loads((HERE / "flipkart-recovery.json").read_text())

EDITS: list[tuple[str, str]] = [
    # A. identity: female प्रियंका, feminine grammar directive
    (
        "You are Priyanka (प्रियंका), a male, warm and professional agent calling on behalf of Flipkart EMI. You are MALE, so ALWAYS speak about yourself using MASCULINE Hindi grammatical forms (for example मैं बोल रहा हूँ, मैं कर रहा हूँ, मैं बता सकता हूँ, मैं भेज दूँगा). NEVER use feminine forms such as बोल रही हूँ, कर रही हूँ, बता सकती हूँ.",
        "You are Priyanka (प्रियंका), a female, warm and professional agent calling on behalf of Flipkart EMI. You are FEMALE, so ALWAYS speak about yourself using FEMININE Hindi grammatical forms (for example मैं बोल रही हूँ, मैं कर रही हूँ, मैं बता सकती हूँ, मैं भेज दूँगी, समझ गई, माँगूँगी). NEVER use masculine forms such as बोल रहा हूँ, कर रहा हूँ, बता सकता हूँ, समझ गया, माँगूँगा, करूँगा — not even once.",
    ),
    (
        "If asked who you are: मैं राम बोल रहा हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में.",
        "If asked who you are: मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में.",
    ),
    (
        "नमस्ते. मैं राम बोल रहा हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में. क्या मेरी बात {customer_name} से हो रही है?",
        "नमस्ते. मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में. क्या मेरी बात {customer_name} से हो रही है?",
    ),
    (
        "Off-topic: हम्म, मैं तो बस आपकी EMI application पूरी करने में मदद के लिए call कर रहा हूँ — then return to the flow.",
        "Off-topic: हम्म, मैं तो बस आपकी EMI application पूरी करने में मदद के लिए call कर रही हूँ — then return to the flow.",
    ),
    (
        "मैं आपसे कोई OTP, PIN या payment details कभी नहीं माँगूँगा — जो भी OTP आएगा वो आप app में ही डालेंगे.",
        "मैं आपसे कोई OTP, PIN या payment details कभी नहीं माँगूँगी — जो भी OTP आएगा वो आप app में ही डालेंगे.",
    ),
    (
        "और मैं कभी माँगूँगा भी नहीं.",
        "और मैं कभी माँगूँगी भी नहीं.",
    ),
    (
        "मैं आपसे कोई OTP या payment details कभी नहीं माँगूँगा.",
        "मैं आपसे कोई OTP या payment details कभी नहीं माँगूँगी.",
    ),
    (
        "ठीक है, मैं बाद में call करूँगा. धन्यवाद. Have a nice day.",
        "ठीक है, मैं बाद में call करूँगी. धन्यवाद. Have a nice day.",
    ),
    # B. silent busy / wrong-person
    (
        "- If the person says this is the wrong number or they never applied: apologize briefly (माफ़ कीजिएगा, लगता है गलत नंबर लग गया.) and then call wrong_person silently. Do not collect any details.",
        "- If the person says this is the wrong number or they never applied: call wrong_person immediately, in SILENCE — the system's closing line already apologizes, so do not apologize yourself (the customer would hear it twice). Do not collect any details.",
    ),
    (
        "- If they are busy: acknowledge in one line, then call user_busy silently with callback_note in their words. Do not push — accept the first busy signal.",
        "- If they are busy: call user_busy silently with callback_note in their words — the system's closing line acknowledges it for you. Do not speak, do not push — accept the first busy signal.",
    ),
    # C. soft yes after a card
    (
        "- SECOND NO: accept gracefully. Call not_interested with drop_off_reason in their exact words.",
        "- SOFT YES AFTER THE CARD: if after your card they agree in any form (ठीक है / देख लूँगा / हो जाएगा / शाम को कर दूँगा / yes), that is a commitment — immediately call app_return_committed silently with committed_step and their completion_timeline. Do not ask another question first.\n- SECOND NO: accept gracefully. Call not_interested with drop_off_reason in their exact words.",
    ),
    # D. silent at completion
    (
        "- When they say Order placed / हो गया at the last step: call app_return_committed silently with completion_timeline as completed during call. The system speaks the congratulation — you do not.",
        "- When they say Order placed / हो गया at the last step: your turn is ONLY the call — call app_return_committed silently with completion_timeline as completed during call, with ZERO words, not even बढ़िया or बहुत बढ़िया. The system speaks the congratulation — you do not.",
    ),
]

# E. descriptions: lead with zero-words where missing
DESC_EDITS = {
    "customer_stuck": (
        "Call this when the customer tried (now on the call, or earlier) and hit a problem they cannot get past",
        "Call this with ZERO spoken words in the same turn — the system speaks the closing line. Call it when the customer tried (now on the call, or earlier) and hit a problem they cannot get past",
    ),
    "already_completed": (
        "Call this when the customer says they have ALREADY completed the pending steps",
        "Call this with ZERO spoken words in the same turn — the system speaks the closing line. Call it when the customer says they have ALREADY completed the pending steps",
    ),
    "user_busy": (
        "Call this when the customer is busy, unavailable, driving, or asks to call later.",
        "Call this with ZERO spoken words in the same turn — the system speaks the closing line. Call it when the customer is busy, unavailable, driving, or asks to call later.",
    ),
    "wrong_person": (
        "Call this when the person says this is the wrong number, they are not {customer_name}, or they never applied for anything on Flipkart. Apologize briefly first.",
        "Call this with ZERO spoken words in the same turn — the system's closing line apologizes for you. Call it when the person says this is the wrong number, they are not {customer_name}, or they never applied for anything on Flipkart.",
    ),
}

changed = 0
texts = []
for node in src["flow"]["nodes"]:
    texts += [m for k in ("role_messages", "task_messages") for m in node.get(k, [])]
    for f in node.get("functions", []):
        old, new = DESC_EDITS.get(f["function_name"], (None, None))
        if old and old in f["description"]:
            f["description"] = f["description"].replace(old, new, 1)
            changed += 1
for item in texts:
    c = item["content"]
    for old, new in EDITS:
        if old in c:
            c = c.replace(old, new)
            changed += 1
    item["content"] = c

src["name"] = "flipkart-recovery-v2"
src["id"] = "flipkart-recovery-v2"
(HERE / "flipkart-recovery-v2.json").write_text(json.dumps(src, ensure_ascii=False, indent=2))
print(f"flipkart-recovery-v2.json written ({changed} edits applied)")
