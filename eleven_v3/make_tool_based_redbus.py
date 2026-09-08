#!/usr/bin/env python3
"""Build the tool_based redbus template from redbus-flow-v4.json.

Latency-optimized for streaming tool calls:
- SHORT function names with DISTINCT first words — the name is decoded
  token-by-token, so `rating_hi` completes (and uniquely prefix-matches for
  early-fire) far sooner than `say_rating_request_hi`.
- A trimmed role: every prose-mode rule (gender, markdown, one-thought) is
  dead weight when the model never writes speech.

Output: eleven_v3/redbus-toolbased.json
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent
SRC = REPO / "redbus-flow-v4.json"
OUT = REPO / "redbus-toolbased.json"

src = json.loads(SRC.read_text())


def say(hi: list[str], *, end_call: bool = False) -> dict:
    block: dict = {
        "utterances": {"hi": hi},
        "phrasing": "first",  # v4 demands exact scripts, word for word
        "default_language": "hi",
    }
    if end_call:
        block["end_call"] = True
    return block


def speech_tool(name: str, description: str, hi: list[str]) -> dict:
    return {
        "function_name": name,
        "description": description,
        "properties": {},
        "required": [],
        "transition_to": None,
        "hooks": [],
        "say": say(hi),
    }


ROLE = """## BUS TRIP FEEDBACK — HINDI, TOOL-BASED

You are राम, customer care from {bus_operator_name}, calling for feedback on a recent bus trip. The greeting already introduced you and used the customer's name — never do either again. Never reveal or imply you are AI. You never write speech: calling a tool IS your reply, exactly ONE tool per turn.

STAGES
1. Right person (the greeting asked). Any conversational reply → rating_hi. Busy / wrong person / not that person / never took this trip / call later → user_busy.
2. Rating. A spoken number 1-5 → the matching outcome tool that same turn. No number → the matching speech tool.
3. Rating was 1/2/3, or they said the trip was bad → reason_hi. Their ANSWER to reason_hi is negative_feedback's feedback — never the rating itself. While they are giving the reason, a pause with more coming → more_hi.

NUMBERS THE CUSTOMER SPEAKS
एक=1, दो=2, तीन=3, चार=4, पाँच=5. English-looking transcriptions are VALID numbers: ek=एक, do/doh/door/dore=दो, teen=तीन, char/chaar=चार, paanch/panch/punch=पाँच. A doubled number (55, पाँच पाँच) is that number said once. Ignore words around the number ('जी चार', 'चार दे दो', 'पाँच star'). A bare 'no'/'नहीं' is NOT a number. Never invent, suggest, confirm or repeat back a rating.

REPLY → TOOL
- चार or पाँच (4/5) → positive_feedback THAT SAME TURN. A spoken number is never a detour, never a reason to re-ask — not even right after bad_rating_hi.
- एक, दो or तीन (1/2/3), or 'trip was bad' (अच्छी नहीं थी, बेकार, बस late थी) → NOTHING this turn; reason_hi.
- Praise with no number (बढ़िया, मस्त) → reask_hi (rating_hi if it hasn't been asked yet this call).
- Off-scale (शून्य, zero, छह, दस) or vague ('ठीक था', 'पता नहीं') → bad_rating_hi; if the NEXT reply still has no number → scale_hi, then keep reask_hi.
- Who is calling / why → why_hi. Which trip / which bus → trip_hi. How the scale works → scale_hi. Then reask_hi on your NEXT turn.
- Busy, driving, in a meeting, call back later, wrong number, not that person → user_busy, first such reply, no persuasion.
- Almost nothing heard → unclear_hi. A reply that stops midway (तो, मैं, ये, वो) → partial_hi — they were still speaking; not an answer, never user_busy. Unmistakable mumble ONCE → repeat_hi, then reask_hi. Silence → there_hi. None of these end the call.
- A mid-call hello/हेलो is them checking the line: re-call the LAST tool you called (it repeats the exact line) — NOT there_hi.

OUTCOME TOOLS (each speaks its closing and ends the call)
- positive_feedback(rating, feedback) — only when चार/पाँच was just spoken. rating = the digit; feedback = any trip comment in their words, "" if only the number.
- negative_feedback(feedback, rating?) — only AFTER reason_hi AND their answer. feedback = their reason verbatim, never the number; rating only if they actually said one.
- user_busy — the NOT-NOW / NOT-THE-RIGHT-PERSON cases above.

Everything the customer hears is Hindi in Devanagari — the tools already carry it."""

TOOLS = [
    # ---- speech tools (short, distinct-first-word names) ----------------
    speech_tool(
        "rating_hi",
        "The ask that INCLUDES the trip details. Use it the FIRST time the "
        "rating is asked this call — including right after you answered a "
        "detour question (who/trip/scale) when no rating ask has happened "
        "yet. After this has run once, every later ask is reask_hi.",
        [
            "ठीक है. आपने {bus_operator_name} बस से {source} से {destination} तक ट्रिप की थी. कृपया अपनी ट्रिप को एक से पाँच के बीच रेटिंग दीजिए, जहाँ एक सबसे कम और पाँच सबसे ज़्यादा है."
        ],
    ),
    speech_tool(
        "reask_hi",
        "The SHORT re-ask — ONLY when rating_hi has already been called this "
        "call and they replied without a number.",
        ["हाँजी. तो आप अपनी ट्रिप को एक से पाँच के बीच कौन सी रेटिंग देना चाहेंगे?"],
    ),
    speech_tool(
        "bad_rating_hi",
        "Their answer was not a single number from एक to पाँच (सात, सिक्स, "
        "two numbers at once, ठीक था). If their NEXT reply contains a valid "
        "number, route by it normally — चार/पाँच → positive_feedback, एक/दो/तीन "
        "→ reason_hi.",
        ["जी. रेटिंग एक से पाँच के बीच सिर्फ़ एक ही नंबर होनी चाहिए."],
    ),
    speech_tool(
        "trip_hi",
        "They asked which trip you mean, where they travelled, or which bus.",
        [
            "जी. यह आपकी {source} से {destination} वाली रीसेंट ट्रिप थी, हमारी {bus_operator_name} बस से."
        ],
    ),
    speech_tool(
        "why_hi",
        "They asked who is calling or why you are calling. reask_hi comes on your NEXT turn.",
        [
            "जी. मैं {bus_operator_name} से बोल रहा हूँ. बस आपकी रीसेंट ट्रिप की रेटिंग लेने के लिए कॉल किया है."
        ],
    ),
    speech_tool(
        "scale_hi",
        "They asked how the rating works / which number is highest. reask_hi comes next turn.",
        [
            "जी. इसमें एक सबसे कम है और पाँच सबसे ज़्यादा. आपको जो ठीक लगे वो रेटिंग दे दीजिए."
        ],
    ),
    speech_tool(
        "reason_hi",
        "They gave a low rating (एक/दो/तीन) or said the trip was bad — this asks why. Their answer becomes negative_feedback's feedback.",
        ["अच्छा. क्या आप बता सकते हैं कि इसका कारण क्या था?"],
    ),
    speech_tool(
        "more_hi",
        "During the reason stage ONLY: they paused mid-answer but seem to have more to say. negative_feedback comes only after they finish.",
        ["जी. और कुछ बताना चाहेंगे?"],
    ),
    speech_tool(
        "there_hi",
        "The customer has gone SILENT for a while (no reply at all). NOT for a mid-call hello — that re-calls your last tool.",
        ["हाँजी. आप कॉल पे हैं?"],
    ),
    speech_tool(
        "unclear_hi",
        "You heard almost nothing (noise only counts as nothing — then call nothing at all).",
        ["जी. आपकी आवाज़ नहीं आ रही है. ज़रा फिर से बोलिए."],
    ),
    speech_tool(
        "partial_hi",
        "Their reply was a fragment that stopped mid-way — a trailing single word like तो / मैं / ये / वो, or a sentence that cut off. Prefer this over reask_hi for fragments.",
        ["जी. पूरा सुनाई नहीं दिया. ज़रा फिर से बोलिए."],
    ),
    speech_tool(
        "repeat_hi",
        "Speech you could not make out, including mumbled or non-verbal sounds (mmm, hmm, huh) in reply to your question — ask ONCE, then reask_hi.",
        ["जी. क्या आप दोबारा बोल सकते हैं?"],
    ),
    # ---- outcome tools (say + end_call) --------------------------------
    {
        "function_name": "positive_feedback",
        "description": "FORBIDDEN unless the customer ALREADY said चार or पाँच "
        "(4/5, char/panch...) out loud in reply to the rating question. Same "
        "turn as the number. Speaks the thank-you closing and ends the call.",
        "properties": {
            "rating": {
                "type": "integer",
                "description": "The rating digit the customer actually said "
                "out loud — only 4 or 5 are valid here. Never invent one.",
            },
            "feedback": {
                "type": "string",
                "description": "Anything the customer said about the trip in "
                "their own words. Pass an empty string if they only said the "
                "number.",
            },
        },
        "required": ["rating"],
        "transition_to": None,
        "hooks": [],
        "say": say(
            ["ठीक है. आपकी रेटिंग के लिए बहुत बहुत धन्यवाद. आपका दिन शुभ हो."],
            end_call=True,
        ),
    },
    {
        "function_name": "negative_feedback",
        "description": "ONLY after reason_hi AND the customer answered it. The "
        "feedback argument is their answer to the why question — never the "
        "rating itself. Speaks the empathy closing and ends the call.",
        "properties": {
            "rating": {
                "type": "integer",
                "description": "The rating the customer said out loud — only "
                "1, 2 or 3 are valid here. OPTIONAL: leave it out entirely if "
                "they described their trip negatively without ever giving a "
                "number. Never invent one.",
            },
            "feedback": {
                "type": "string",
                "description": "The customer's answer to the reason question — "
                "why they were unhappy, in their own words. Include every issue "
                "they mentioned. NEVER the rating itself: 'दो', 'दो-दो', 'two', "
                "'2' are numbers, not reasons.",
            },
        },
        "required": ["feedback"],
        "transition_to": None,
        "hooks": [],
        "say": say(
            [
                "जी. मैं आपकी परेशानी पूरी तरह समझता हूँ. आपकी बात हमारी टीम तक ज़रूर पहुँचाई जाएगी. हमें बताने के लिए धन्यवाद. आपका दिन शुभ हो."
            ],
            end_call=True,
        ),
    },
    {
        "function_name": "user_busy",
        "description": "The customer said A) NOT NOW — busy, driving, in a "
        "meeting, or asked to call back later, or B) NOT THE RIGHT PERSON — "
        "wrong number, they are not that person, they never made this trip, or "
        "the person is not there. First such reply. Speaks the polite closing "
        "and ends the call.",
        "properties": {},
        "required": [],
        "transition_to": None,
        "hooks": [],
        "say": say(
            ["ठीक है. आपके समय के लिए धन्यवाद. आपका दिन शुभ हो."],
            end_call=True,
        ),
    },
]

template = {
    "template_name": "Redbus Bus Feedback — tool_based",
    "identifier": "redbus-toolbased",
    "is_active": False,
    "description": (
        "tool_based redbus trip-feedback, latency-optimized: short "
        "distinct-first-word function names (name decode gates early-fire "
        "TTS), trimmed role, exact prod scripts as say blocks, outcomes speak "
        "their closings, end the call and record the outcome."
    ),
    "expected_payload_schema": src.get("expected_payload_schema"),
    "example_payload": {
        "customer_name": "Rhea",
        "bus_operator_name": "ABC travels",
        "source": "Mumbai",
        "destination": "Punjab",
        "customer_mobile_number": "9999999999",
    },
    "configurations": {
        "llm_configurations": {
            "provider": "azure",
            "temperature": 0.1,
            "max_tokens": 500,
            "tool_choice": "required",
        },
        "tts_configuration": {
            **src["configurations"]["tts_configuration"],
            "voice_id": "iB2rIwm9cQCRGWoKDRtX",
            "model": "eleven_v3_conversational",
            "speed": 1,
            "stability": None,
            "similarity_boost": None,
            "enable_tts_caching": True,
        },
        "initial_greeting": "नमस्ते मैं {bus_operator_name} से राम बोल रहा हुं. क्या मैं {customer_name} से बात कर रहा हुं?",
        "user_idle_configuration": {
            "enabled": True,
            "timeout": 15,
            "idle_message": "The user has been quiet for a while. Call there_hi.",
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
                        "content": "Trip: {bus_operator_name} बस, {source} से {destination}. Customer: {customer_name}. Stage machine: right person -> rating -> (low? reason) -> outcome. One tool per turn.",
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
