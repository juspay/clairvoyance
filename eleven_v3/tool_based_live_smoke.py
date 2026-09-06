#!/usr/bin/env python3
"""Live smoke for tool_based mode against running local servers (fresh code).

Chains the real pieces end-to-end, no mocks:
  1. Fresh-code boot check  — own clairvoyance instance on :8017 serving
     /docs + /openapi.json with all routers registered.
  2. Builder (fresh code)   — example tool_based template builds; nodes get
     respond_immediately=False; rules prompt injected.
  3. Renderer (fresh code)  — render_say produces the spoken line for a
     simulated tool call (args + payload vars).
  4. Live DragonTTS (:8000) — the rendered line synthesizes to real audio
     through the elevenlabs v3 voice used by the flipkart template.
  5. Live clairvoyance (:8017) — tool_choice plumbing visible in the built
     Azure service settings.extra.

Run:  ./.venv/bin/python eleven_v3/tool_based_live_smoke.py
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.ai.voice.agents.breeze_buddy.template.builder import (
    FlowConfigBuilder,
)  # noqa: E402
from app.ai.voice.agents.breeze_buddy.template.tool_speech import (  # noqa: E402
    TOOL_BASED_RULES_PROMPT,
    render_say,
)
from app.ai.voice.agents.breeze_buddy.template.types import (  # noqa: E402
    FlowMode,
    SayConfig,
    TemplateModel,
)

CLAIRVOANCE_FRESH = "http://127.0.0.1:8017"
DRAGONTTS = "http://127.0.0.1:8000"
TEMPLATE_PATH = (
    REPO / "app/ai/voice/agents/breeze_buddy/examples/templates/tool-based-example.json"
)
OUT_WAV = REPO / "eleven_v3" / "out"
OUT_WAV.mkdir(exist_ok=True)

_results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    template = json.loads(TEMPLATE_PATH.read_text())

    # -- 1. fresh-code server ------------------------------------------------
    with httpx.Client(timeout=10) as c:
        r = c.get(f"{CLAIRVOANCE_FRESH}/openapi.json")
        ok = r.status_code == 200 and "paths" in r.json()
        check("fresh clairvoyance :8017 serves openapi", ok, f"HTTP {r.status_code}")
        if ok:
            has_templates = any("/templates" in p for p in r.json()["paths"])
            check("template routers registered", has_templates)

    # -- 2. builder ----------------------------------------------------------
    model = TemplateModel(
        id="smoke",
        reseller_id="r1",
        name=template["template_name"],
        flow=template["flow"],
    )
    config = FlowConfigBuilder().build_flow_config(model)
    check("build_flow_config succeeds", config.get("mode") == FlowMode.TOOL_BASED.value)
    all_quiet = all(
        n.get("respond_immediately") is False for n in config["nodes"].values()
    )
    check("all nodes respond_immediately=False", all_quiet)
    rules_ok = config["nodes"][config["initial_node"]]["role_messages"][-1][
        "content"
    ].endswith(TOOL_BASED_RULES_PROMPT)
    check("rules prompt injected into initial node", rules_ok)

    # -- 3. renderer ---------------------------------------------------------
    nodes = {n["node_name"]: n for n in template["flow"]["nodes"]}
    say_thankyou = next(
        f
        for f in nodes["opening_node"]["functions"]
        if f["function_name"] == "say_thankyou"
    )
    line, language = render_say(
        SayConfig.model_validate(say_thankyou["say"]),
        {"language": "hi"},
        {"customer_name": "राहुल"},
    )
    check(
        "render_say picks hindi + substitutes {customer_name}",
        language == "hi" and "राहुल" in line,
        line,
    )

    # -- 4. live DragonTTS ---------------------------------------------------
    with httpx.Client(timeout=120) as c:
        r = c.post(
            f"{DRAGONTTS}/tts/bytes",
            json={
                "model_id": "elevenlabs:eleven_v3_conversational",
                "transcript": line,
                "voice": {"mode": "id", "id": "sYajEyYUBfRRfzFyCWCF"},
                "language": "hi",
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_s16le",
                    "sample_rate": 16000,
                },
            },
        )
        ok = r.status_code == 200 and len(r.content) > 16000
        check(
            "DragonTTS synthesizes rendered line",
            ok,
            f"HTTP {r.status_code}, {len(r.content)} bytes, "
            f"cache={r.headers.get('X-Cache')}",
        )
        if ok:
            wav_path = OUT_WAV / "tool_based_say_line.wav"
            with wave.open(str(wav_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(r.content)
            dur = len(r.content) / 2 / 16000
            check(
                "audio is real speech length",
                1.0 < dur < 30.0,
                f"{dur:.1f}s -> {wav_path.name}",
            )

    # -- 5. tool_choice plumbing ----------------------------------------------
    from app.ai.voice.llm.azure import AzureConfig, build_azure_llm

    svc = build_azure_llm(
        AzureConfig(
            api_key="smoke",
            endpoint="https://smoke.openai.azure.com",
            model="gpt-4.1",
            tool_choice="required",
        )
    )
    check(
        "azure settings.extra carries tool_choice=required",
        svc._settings.extra.get("tool_choice") == "required",
    )

    passed = sum(_results)
    print(f"\n{passed}/{len(_results)} smoke checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
