"""v4: stop the LLM from INVENTING a rating the customer never said.

v3 left exactly one violation across 32 runs (Azure, badtrip_nonumber):
negative_feedback was called WITH a rating although the customer never gave
a number. The existing 'Never invent one' wording was ignored, so v4 makes
the field description example-driven and extends hard rule R4.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = json.loads((HERE / "redbus-flow-v3.json").read_text())

RATING_DESC_NEW = (
    "The rating the customer said out loud — only 1, 2 or 3 are valid here. "
    "Leave this field OUT when the customer never said a number, even if their "
    "complaint makes a low rating obvious to you — never infer it. "
    "Example: customer says 'बहुत खराब थी' and then gives a reason -> call "
    "negative_feedback WITHOUT rating. Example: customer says 'दो' -> pass rating=2."
)

R4_OLD = "R4. Never call a function the customer has not earned: positive_feedback needs the number चार/पाँच already spoken; negative_feedback needs the reason already answered; user_busy needs a busy/wrong-person reply."
R4_NEW = (
    "R4. Never call a function the customer has not earned: positive_feedback needs the number चार/पाँच already spoken; "
    "negative_feedback needs the reason already answered; user_busy needs a busy/wrong-person reply. "
    "Any rating number you pass must be a number the customer SPOKE — never a number you inferred or guessed."
)

changed = 0
for node in src["flow"]["nodes"]:
    for msg in node.get("role_messages", []):
        c = msg["content"]
        if R4_OLD in c:
            c = c.replace(R4_OLD, R4_NEW)
            changed += 1
        msg["content"] = c
    for f in node.get("functions", []):
        if f["function_name"] == "negative_feedback":
            props = f.get("properties", {})
            if "rating" in props:
                props["rating"]["description"] = RATING_DESC_NEW
                changed += 1

src["name"] = "redbus-flow-v4"
src["id"] = "redbus-flow-v4"
(HERE / "redbus-flow-v4.json").write_text(json.dumps(src, ensure_ascii=False, indent=2))
print(f"redbus-flow-v4.json written ({changed} edits applied)")
