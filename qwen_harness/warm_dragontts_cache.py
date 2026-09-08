"""Warm the DragonTTS cache with every phrase a template can speak.

Renders each ``say`` utterance (and the initial greeting) with payload values,
splits them exactly like the live sentence pacing does, and POSTs each string
to DragonTTS ``/tts/stream`` — the same request body the live
``DragonTTSService`` builds — discarding the audio. Live calls then stream the
cached blobs instead of waiting on the nested provider.

The cache key is the request body, so this must build the body the same way
``get_tts_service`` does: ``model_id = "<provider>:<model>"``, params via
``_collect_params`` on the validated ``TTSConfig``, telephony sample rate
8000. Placeholder-carrying lines are only cached for the values passed here —
re-run with the lead's real values when they differ.

Usage:
  .venv/bin/python qwen_harness/warm_dragontts_cache.py \
      qwen_harness/redbus-customer-trip-feedback-toolbased.json \
      --url http://localhost:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

from app.ai.voice.agents.breeze_buddy.template.tool_speech import split_say_for_tts
from app.ai.voice.agents.breeze_buddy.template.types import TTSConfig
from app.ai.voice.tts.dragontts import _collect_params

# Default payload: the values the local test calls run with (they match the
# template's expected_payload_schema examples; customer_name matches the
# test lead).
DEFAULT_VALUES: Dict[str, str] = {
    "bus_operator_name": "ABC travels",
    "source": "Mumbai",
    "destination": "Punjab",
    "customer_name": "Narsimha Reddy",
}


def render(text: str, values: Dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def collect_phrases(template: Dict[str, Any], values: Dict[str, str]) -> List[str]:
    """Every speakable string: greeting + one entry per say utterance, then
    both the full rendering and each sentence split of it (the live path
    requests per sentence)."""
    texts: List[str] = []
    greeting = template.get("configurations", {}).get("initial_greeting")
    if greeting:
        texts.append(render(greeting, values))
    for node in template.get("flow", {}).get("nodes", []):
        for func in node.get("functions", []):
            say = func.get("say") or {}
            for utterances in (say.get("utterances") or {}).values():
                for utterance in utterances:
                    texts.append(render(utterance, values))

    phrases: List[str] = []
    seen: set[str] = set()
    for text in texts:
        for piece in [text, *split_say_for_tts(text)]:
            if piece and piece not in seen:
                seen.add(piece)
                phrases.append(piece)
    return phrases


async def warm(url: str, phrases: List[str], body_template: Dict[str, Any]) -> None:
    timeout = httpx.Timeout(30.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        slow: List[str] = []
        failed = 0
        for i, phrase in enumerate(phrases, 1):
            body = {**body_template, "transcript": phrase}
            started = time.monotonic()
            try:
                async with client.stream("POST", f"{url}/tts/stream", json=body) as resp:
                    if resp.status_code >= 400:
                        failed += 1
                        detail = (await resp.aread()).decode("utf-8", "replace")[:200]
                        print(f"[{i}/{len(phrases)}] HTTP {resp.status_code}: {detail}")
                        continue
                    bytes_read = 0
                    async for chunk in resp.aiter_bytes():
                        bytes_read += len(chunk)
                elapsed = (time.monotonic() - started) * 1000
                if elapsed >= 40:
                    slow.append(phrase)
                print(
                    f"[{i}/{len(phrases)}] {elapsed:7.0f}ms {bytes_read/1024:6.0f}KB"
                    f"{' (cache?)' if elapsed < 40 else ''}  {phrase[:60]}"
                )
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(phrases)}] FAILED: {e}  {phrase[:60]}")
        print(
            f"\n{len(phrases) - failed - len(slow)}/{len(phrases)} phrases served fast "
            f"(cache hits), {len(slow)} synthesized fresh, {failed} failed"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path, help="template JSON file")
    parser.add_argument("--url", default="http://localhost:8000", help="DragonTTS base URL")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="payload value for placeholder rendering (repeatable)",
    )
    parser.add_argument(
        "--sample-rate", type=int, default=8000, help="telephony output rate (default 8000)"
    )
    args = parser.parse_args()

    template = json.loads(args.template.read_text())
    values = {
        **DEFAULT_VALUES,
        **dict(kv.split("=", 1) for kv in args.set),
    }
    voice_config = TTSConfig.model_validate(
        template["configurations"]["tts_configuration"]
    )

    # Same auto-wrap get_tts_service applies for an enable_tts_caching
    # template: the proxy model id is "<upstream provider>:<model>". The
    # provider field is an enum — serialize to its value.
    provider = getattr(voice_config.provider, "value", voice_config.provider)
    body_template = {
        "model_id": f"{provider}:{voice_config.model}",
        "voice": {"id": voice_config.voice_id or ""},
        "language": voice_config.language or "",
        "output_format": {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": args.sample_rate,
        },
        "params": _collect_params(voice_config),
    }

    phrases = collect_phrases(template, values)
    print(f"DragonTTS cache warm-up: {args.url}")
    print(f"model_id={body_template['model_id']} voice={body_template['voice']['id']} "
          f"language={body_template['language']!r} params={body_template['params']}")
    print(f"values={values}\n{len(phrases)} distinct phrases (full + per-sentence)\n")
    asyncio.run(warm(args.url, phrases, body_template))


if __name__ == "__main__":
    main()
