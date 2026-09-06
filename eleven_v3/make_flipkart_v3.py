"""v3: three hardening edits from the full 23-persona matrix (Azure 20/23).

  1. Final-step completion: the model announced the next guidance step IN THE
     SAME TURN as app_return_committed when the customer's message reported
     the last step done.
  2. Bare first "hello": the model jumped to the hook instead of repeating
     the greeting once as STEP 2 prescribes.
  3. After call screening: the model delivered a partial greeting (missed
     नमस्ते and the name question) instead of the FULL greeting.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = json.loads((HERE / "flipkart-recovery-v2.json").read_text())

EDITS: list[tuple[str, str]] = [
    (
        "- When they say Order placed / हो गया at the last step: your turn is ONLY the call — call app_return_committed silently with completion_timeline as completed during call, with ZERO words, not even बढ़िया or बहुत बढ़िया. The system speaks the congratulation — you do not.",
        "- When they say Order placed / हो गया at the last step: your turn is ONLY the call — call app_return_committed silently with completion_timeline as completed during call, with ZERO words, not even बढ़िया or बहुत बढ़िया. The system speaks the congratulation — you do not. If their message reports the FINAL step already done (e.g. 'Agree and Sign कर दिया, OTP डाल दिया, order place हो गया'), announcing the next step is FORBIDDEN — the journey is over; the silent call is the whole turn.",
    ),
    (
        "- If this is their VERY FIRST response and they JUST say hello (or similar), repeat the greeting once: नमस्ते. मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में. क्या मेरी बात {customer_name} से हो रही है? (Never repeat it later in the call.)",
        "- If this is their VERY FIRST response and they JUST say hello (or similar), that is NOT identity confirmation — they have not answered you yet. Repeat the greeting once, word for word: नमस्ते. मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में. क्या मेरी बात {customer_name} से हो रही है? Do NOT jump to the hook instead. (Never repeat the greeting later in the call.)",
    ),
    (
        "Then WAIT silently — the real person has not heard anything yet. When a human comes on (Yes / Hello / हाँ), deliver the FULL greeting again as if the call just started. Do not skip to the hook.",
        "Then WAIT silently — the real person has not heard anything yet. When a human comes on (Yes / Hello / हाँ), deliver the FULL greeting again, word for word — starting with नमस्ते, including the customer's name question — exactly as if the call just started. A partial intro (without नमस्ते or without the name question) is not acceptable. Do not skip to the hook.",
    ),
]

changed = 0
for node in src["flow"]["nodes"]:
    for key in ("role_messages", "task_messages"):
        for msg in node.get(key, []):
            c = msg["content"]
            for old, new in EDITS:
                if old in c:
                    c = c.replace(old, new)
                    changed += 1
            msg["content"] = c

assert changed == len(EDITS), f"expected {len(EDITS)} edits, applied {changed}"
src["name"] = "flipkart-recovery-v3"
src["id"] = "flipkart-recovery-v3"
(HERE / "flipkart-recovery-v3.json").write_text(json.dumps(src, ensure_ascii=False, indent=2))
print(f"flipkart-recovery-v3.json written ({changed} edits applied)")
