"""Create redbus-flow-v3.json from v2 with structure-first instruction changes.

v2's prose tightening changed nothing on Grid deepseek (same 4 personas fail:
script+function same turn, WHY CALLING+RATING REQUEST merge, SCALE+RE-ASK
merge, English framing leak on user_busy). DeepSeek weighs the START of the
system prompt and tool descriptions far more than mid-prompt negations, so
v3 restructures instead of re-wording:

  1. A "### 0) HARD RULES" block leads the main system message.
  2. positive_feedback / user_busy descriptions lead with the forbidden case.
  3. Worked customer/you examples in §4 and §5 (models copy examples).
  4. §8 script entries get an explicit "one script per turn" rider.

Hindi dialogue phrases remain byte-identical.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = json.loads((HERE / "redbus-flow-v2.json").read_text())

HARD_RULES = """### 0) HARD RULES — THESE OVERRIDE EVERYTHING ELSE
R1. A turn in which you call a function contains ZERO spoken words. No prefix, no suffix, no apology, no English, no narration. The closing line is spoken for you.
R2. One script per turn. If two scripts are needed (answer + ask, WHY CALLING + RATING REQUEST, SCALE EXPLAINED + re-ask), speak the first this turn and the second on your NEXT turn.
R3. Never call ANY function in a turn where you also speak.
R4. Never call a function the customer has not earned: positive_feedback needs the number चार/पाँच already spoken; negative_feedback needs the reason already answered; user_busy needs a busy/wrong-person reply.
R5. Every spoken word is Hindi in Devanagari.

"""

POS_DESC_OLD = "Call this function the moment the customer gives a rating of 4 or 5, and only then."
POS_DESC_NEW = (
    "FORBIDDEN unless the customer has ALREADY said चार or पाँच (four/five/4/5) out loud "
    "in reply to the rating question. Until that exact moment this function does not exist for you. "
    "Call it the moment they give a rating of 4 or 5, and only then."
)

BUSY_DESC_OLD = "Call this function ONLY when the customer has said one of these three things:"
BUSY_DESC_NEW = (
    "When you call this function you speak NOTHING (rule R1) — the closing line is spoken for you. "
    "Call it ONLY when the customer has said one of these things:"

)

STEP4_EXAMPLES = """

WORKED EXAMPLES for step 1:
- Customer: "कौन बोल रहा है?" -> You speak ONLY the WHY CALLING line, then stop. On your NEXT turn (after they reply) you speak RATING REQUEST.
- Customer: "हाँ बोलिए" -> You speak RATING REQUEST.
- Customer: "अभी व्यस्त हूँ" -> You call user_busy and speak NOTHING."""

STEP5_EXAMPLES = """

WORKED EXAMPLES for step 2:
- Customer: "चार" -> you call positive_feedback(rating=4) and speak NOTHING.
- Customer: "दो" -> you speak ONLY the ASK REASON line and wait. Customer: "बस लेट थी" -> you call negative_feedback(feedback="बस लेट थी", rating=2) and speak NOTHING.
- Customer: "बहुत अच्छी थी" -> no number yet: you speak RATING RE-ASK.
- Customer: "रेटिंग कैसे दें?" -> you speak ONLY SCALE EXPLAINED and wait; you re-ask on your NEXT turn."""

SCALE_RIDER_OLD = "SCALE EXPLAINED — when they ask how the rating works or which number is highest or lowest.\nजी. इसमें एक सबसे कम है और पाँच सबसे ज़्यादा."
SCALE_RIDER_NEW = (
    "SCALE EXPLAINED — when they ask how the rating works or which number is highest or lowest. "
    "This script is ONE turn by itself: after it, stop. The re-ask comes on your NEXT turn.\n"
    "जी. इसमें एक सबसे कम है और पाँच सबसे ज़्यादा."
)

changed = 0
for node in src["flow"]["nodes"]:
    for msg in node.get("role_messages", []):
        c = msg["content"]
        if c.startswith("## BUS TRIP FEEDBACK CALL"):
            assert "### 0) HARD RULES" not in c
            c = c.replace("## BUS TRIP FEEDBACK CALL — SYSTEM INSTRUCTIONS (HINDI)\n\n",
                          "## BUS TRIP FEEDBACK CALL — SYSTEM INSTRUCTIONS (HINDI)\n\n" + HARD_RULES, 1)
            changed += 1
        if "### 4) STEP 1" in c and "WORKED EXAMPLES for step 1" not in c:
            marker = "- You heard almost nothing:"
            head, sep, tail = c.partition(marker)
            assert sep
            # insert examples just before the "heard almost nothing" bullet block
            c = head.rstrip() + STEP4_EXAMPLES + "\n\n" + marker + tail
            changed += 1
        if "### 5) STEP 2" in c and "WORKED EXAMPLES for step 2" not in c:
            marker = "NEVER suggest a number"
            head, sep, tail = c.partition(marker)
            assert sep
            end = tail.find("\n\n")
            rest = tail[end:] if end >= 0 else ""
            c = head + marker + tail[:end] + STEP5_EXAMPLES + rest
            changed += 1
        if SCALE_RIDER_OLD in c:
            c = c.replace(SCALE_RIDER_OLD, SCALE_RIDER_NEW)
            changed += 1
        msg["content"] = c
    for f in node.get("functions", []):
        if f["function_name"] == "positive_feedback" and POS_DESC_OLD in f["description"]:
            f["description"] = f["description"].replace(POS_DESC_OLD, POS_DESC_NEW, 1)
            changed += 1
        if f["function_name"] == "user_busy" and BUSY_DESC_OLD in f["description"]:
            f["description"] = f["description"].replace(BUSY_DESC_OLD, BUSY_DESC_NEW, 1)
            changed += 1

src["name"] = "redbus-flow-v3"
src["id"] = "redbus-flow-v3"
(HERE / "redbus-flow-v3.json").write_text(json.dumps(src, ensure_ascii=False, indent=2))
print(f"redbus-flow-v3.json written ({changed} structural edits applied)")
