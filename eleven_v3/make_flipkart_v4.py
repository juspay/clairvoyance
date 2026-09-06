"""v4: one edit — a bare first hello still jumped to the hook (22/23).

Restructures STEP 2's opening into a mechanical first-turn decision table and
adds the missing gate on STEP 3 (the hook needs actual customer speech, not a
bare hello).
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = json.loads((HERE / "flipkart-recovery-v3.json").read_text())

EDITS: list[tuple[str, str]] = [
    (
        "STEP 2 — IDENTITY & PERMISSION\n- If this is their VERY FIRST response and they JUST say hello (or similar), that is NOT identity confirmation — they have not answered you yet. Repeat the greeting once, word for word: नमस्ते. मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में. क्या मेरी बात {customer_name} से हो रही है? Do NOT jump to the hook instead. (Never repeat the greeting later in the call.)",
        "STEP 2 — IDENTITY & PERMISSION\nTHEIR FIRST WORDS DECIDE YOUR FIRST TURN — exactly one row applies:\n- Bare hello / हेलो / हाँ? / who is this? with nothing else → repeat the greeting once, word for word: नमस्ते. मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में. क्या मेरी बात {customer_name} से हो रही है? Say NOTHING else that turn. (Never repeat the greeting later in the call.)\n- Identity confirmed or an invitation to speak (हाँ बोलो / हाँ बोल रहा हूँ / बताइए / जी कहिए) → go straight to STEP 3, the hook.",
    ),
    (
        "STEP 3 — THE HOOK\nSpeak ONLY the matching THE HOOK case — offer, done, left, question. Then wait.",
        "STEP 3 — THE HOOK\nComes only after the customer has actually spoken TO YOU (confirmed identity, invited you, or asked why you called). A bare hello is none of those — the greeting repeat comes first. Speak ONLY the matching THE HOOK case — offer, done, left, question. Then wait.",
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
src["name"] = "flipkart-recovery-v4"
src["id"] = "flipkart-recovery-v4"
(HERE / "flipkart-recovery-v4.json").write_text(json.dumps(src, ensure_ascii=False, indent=2))
print(f"flipkart-recovery-v4.json written ({changed} edits applied)")
