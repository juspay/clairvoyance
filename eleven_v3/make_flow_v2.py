"""Create redbus-flow-v2.json from the original template with flow-only edits.

Every Hindi dialogue phrase stays byte-identical; only the English
instruction text is tightened, targeting the violations the harness found:

  1. WHY CALLING + RATING REQUEST merged into one 6-sentence turn (both LLMs)
  2. DeepSeek speaking a script AND calling positive_feedback in the same
     turn, with no rating ever said (premature call end, fabricated outcome)
  3. DeepSeek speaking text alongside the user_busy call (English framing
     leak — the closing line is spoken for it)
  4. DeepSeek merging SCALE EXPLAINED + RATING RE-ASK into one turn
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = json.loads((HERE / "redbus-llm-test.json").read_text())

EDITS = [
    # (old, new) — instruction text only
    (
        "- ONE THOUGHT PER TURN: at most two sentences, then STOP. Ask one question and wait for the answer. Never merge two scripts into one turn.",
        "- ONE THOUGHT PER TURN: at most two sentences, then STOP. Ask one question and wait for the answer. NEVER merge two scripts into one turn — if a situation needs two (WHY CALLING then RATING REQUEST, SCALE EXPLAINED then RATING RE-ASK, an answer then RATING RE-ASK), speak the FIRST one this turn, stop, and speak the second only on your NEXT turn.",
    ),
    (
        "ONE function per call, never two in a turn, never the same one twice. In the turn you call it, speak NO words at all — the closing line is spoken for you.",
        "ONE function per call, never two in a turn, never the same one twice. A function-call turn contains the call and NOTHING else: no sentence before it, no sentence after it, no apology, no thanks, no summary — the closing line is spoken for you. If you also want to say something, you are wrong: call the function (or say the words) and stop.",
    ),
    (
        "Call this function the moment the customer gives a rating of 4 or 5, and only then. Pass the exact number they said.",
        "Call this function the moment the customer gives a rating of 4 or 5, and only then. Pass the exact number they said. It is FORBIDDEN to call this function in a turn where you also speak a script or answer a question, and forbidden until the customer has actually said the number चार or पाँच out loud.",
    ),
    (
        "Anything else at all: do not call it. Speak a script and wait.",
        "Anything else at all — including a question, a trip detail, praise, or silence: do not call it. Speak a script (or nothing) and wait. Never call this function and speak in the same turn.",
    ),
]

changed = 0
for node in src["flow"]["nodes"]:
    texts = [msg for key in ("role_messages", "task_messages") for msg in node.get(key, [])]
    texts += node.get("functions", [])  # function descriptions too
    for item in texts:
        c = item["content"] if "content" in item else item.get("description", "")
        for old, new in EDITS:
            if old in c:
                c = c.replace(old, new)
                changed += 1
        if "content" in item:
            item["content"] = c
        else:
            item["description"] = c

src["name"] = "redbus-flow-v2"
src["id"] = "redbus-flow-v2"
del src["created_at"], src["updated_at"]

assert changed == len(EDITS), f"expected {len(EDITS)} edits, applied {changed}"
(HERE / "redbus-flow-v2.json").write_text(json.dumps(src, ensure_ascii=False, indent=2))
print(f"redbus-flow-v2.json written ({changed} instruction edits applied)")
