"""Emit the two final Flipkart templates from the validated v2 flow.

Both carry the identical flow (13/13 clean on Azure). They differ ONLY in
llm_configurations:

  - flipkart-recovery-azure.json          Azure OpenAI (model null -> the
                                          env default deployment; gpt-4o has
                                          no deployment on the current
                                          endpoint, gpt-4.1 does).
  - flipkart-recovery-grid-deepseek.json  Juspay Grid gateway, model
                                          "deepseek", key GRID_API_KEY.

Temperature is pinned to 0.1 (the live default is 0.7) so real calls phrase
replies near-deterministically — that is what makes the DragonTTS warm cache
actually pay off. prefill_system_prompt stays true (turn-1 Azure prompt-cache
warm); on the Grid/OpenAI path the runtime skips it automatically if the
gateway has no chat.completions prefix cache.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = json.loads((HERE / "flipkart-recovery-v5.json").read_text())


def llm_cfg(**over) -> dict:
    cfg = {
        "provider": None, "sdk": None, "model": None, "region": None,
        "endpoint": None, "api_key_name": None, "temperature": None,
        "max_tokens": None, "thinking": None, "function_call_timeout_secs": None,
        "prefill_system_prompt": True, "realtime": None,
    }
    cfg.update(over)
    return cfg


variants = {
    "flipkart-recovery-azure": llm_cfg(
        provider="azure", temperature=0.1, max_tokens=500
    ),
    "flipkart-recovery-grid-deepseek": llm_cfg(
        provider="openai", model="deepseek", endpoint="https://grid.ai.juspay.net",
        api_key_name="GRID_API_KEY", temperature=0.1, max_tokens=500,
    ),
}

for name, cfg in variants.items():
    out = json.loads(json.dumps(src))  # deep copy
    out["name"] = name
    out["id"] = name
    out["configurations"]["llm_configurations"] = cfg
    # Calls route DIRECT to the TTS provider — no DragonTTS cache hop.
    out["configurations"]["tts_configuration"]["enable_tts_caching"] = False
    (HERE / f"{name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"{name}.json written (enable_tts_caching=false)")
