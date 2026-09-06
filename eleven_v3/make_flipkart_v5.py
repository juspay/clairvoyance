"""v5: stabilize the three flickering first-turn cases (20-22/23 across runs).

  1. Bare hello → hook: the rule lived deep in CONVERSATION FLOW; models weigh
     the prompt opening more. Add it to the IDENTITY section as well.
  2. कौन है आप + क्यों कॉल किया (combined): models answered with the hook only,
     skipping the name intro. Make intro-first explicit for combined asks.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = json.loads((HERE / "flipkart-recovery-v4.json").read_text())

EDITS: list[tuple[str, str]] = [
    (
        "Never reveal you are an AI, bot, or virtual assistant. If asked who you are: मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में.",
        "Never reveal you are an AI, bot, or virtual assistant. FIRST-TURN RULE: if their very first words are a bare hello / हेलो / हाँ? and nothing else, answer with the greeting repeat (नमस्ते. मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में. क्या मेरी बात {customer_name} से हो रही है?) — never the hook. If asked who you are (कौन है / कौन बोल रही है — even combined with क्यों कॉल किया), your first line is the intro: मैं प्रियंका बोल रही हूँ, Flipkart EMI की तरफ से — आपकी {product_name} की purchase के बारे में; the hook comes on your NEXT turn.",
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
src["name"] = "flipkart-recovery-v5"
src["id"] = "flipkart-recovery-v5"
(HERE / "flipkart-recovery-v5.json").write_text(json.dumps(src, ensure_ascii=False, indent=2))
print(f"flipkart-recovery-v5.json written ({changed} edits applied)")
