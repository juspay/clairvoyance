"""Topic extraction and deterministic output cleanup."""

import json
import re
from typing import Any, Dict, List, Mapping, Optional

from openai import AsyncOpenAI

from app.core.config.dynamic import GRID_API_KEY, LITELLM_BASE_URL
from app.schemas.breeze_buddy.conversation_analysis import TopicExtractionResult

_DEFAULT_MODEL = "minimaxai/minimax-m2"
_DEFAULT_MAX_TOPICS = 3

_PROMPT_ONLY_RESPONSE_INSTRUCTION = """Return only valid JSON with exactly this shape:
{"customer_needs":[{"summary":"short customer need","evidence_turns":[1]}],"topics":[{"type":"short_snake_case_key","label":"short label","phrase":"exact customer words","evidence_turns":[1]}]}
Every listed field is required. Use empty arrays when there are no meaningful customer needs or topics. Do not wrap the JSON in markdown."""


def resolve_topic_evaluation_configuration(
    configuration: Optional[Mapping[str, Any] | str] = None,
) -> Dict[str, Any]:
    """Resolve one agent's evaluator JSON against safe runtime defaults."""
    raw: Dict[str, Any]
    if isinstance(configuration, str):
        parsed = json.loads(configuration)
        raw = parsed if isinstance(parsed, dict) else {}
    elif isinstance(configuration, Mapping):
        raw = dict(configuration)
    else:
        raw = {}

    model = str(raw.get("model") or _DEFAULT_MODEL).strip()
    system_prompt = str(raw.get("system_prompt") or "").strip()[:50000] or None
    settings = raw.get("settings")
    settings = dict(settings) if isinstance(settings, Mapping) else {}

    try:
        temperature = float(settings.get("temperature", 0))
    except (TypeError, ValueError):
        temperature = 0
    temperature = min(2.0, max(0.0, temperature))

    try:
        max_output_tokens = int(settings.get("max_output_tokens", 10000))
    except (TypeError, ValueError, OverflowError):
        max_output_tokens = 10000
    max_output_tokens = min(10000, max(128, max_output_tokens))

    try:
        max_topics = int(settings.get("max_topics", _DEFAULT_MAX_TOPICS))
    except (TypeError, ValueError, OverflowError):
        max_topics = _DEFAULT_MAX_TOPICS
    max_topics = min(5, max(1, max_topics))

    return {
        "model": model,
        "system_prompt": system_prompt,
        "settings": {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "max_topics": max_topics,
        },
    }


def _decode_json_object(content: Any) -> Dict[str, Any]:
    if isinstance(content, list):
        content = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping)
        )
    text = str(content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Topic evaluator response is not a JSON object")
    return value


async def _request_grid(
    prompt: str,
    transcript: str,
    runtime: Mapping[str, Any],
) -> Dict[str, Any]:
    base_url = (await LITELLM_BASE_URL()).strip()
    if not base_url:
        raise RuntimeError("Grid base URL is not configured; set LITELLM_BASE_URL")
    api_key = await GRID_API_KEY()
    if not api_key:
        raise RuntimeError("Grid API key is not configured; set GRID_API_KEY")
    base_url = base_url.rstrip("/").removesuffix("/chat/completions")
    async with AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=90, max_retries=0
    ) as client:
        response = await client.chat.completions.create(
            model=runtime["model"],
            messages=[
                {
                    "role": "system",
                    "content": prompt + "\n\n" + _PROMPT_ONLY_RESPONSE_INSTRUCTION,
                },
                {"role": "user", "content": transcript},
            ],
            temperature=runtime["settings"]["temperature"],
            max_tokens=runtime["settings"]["max_output_tokens"],
            response_format={"type": "json_object"},
        )
    if not response.choices:
        raise ValueError("Grid topic extraction returned no choices")
    return _decode_json_object(response.choices[0].message.content)


def normalize_topic_label(label: str) -> str:
    label = re.sub(r"\s+", " ", label.strip().lower())
    return label.strip(" .,:;!?-_/")[:120]


def normalize_topic_type(topic_type: str) -> str:
    """Turn model-created types into stable, index-friendly identifiers."""
    topic_type = re.sub(r"[^a-z0-9]+", "_", topic_type.strip().lower())
    return topic_type.strip("_")[:120]


def topic_labels_to_catalog(labels: Optional[List[str]]) -> List[Dict[str, str]]:
    """Convert plain configuration labels into the model's key/label catalog."""
    catalog: List[Dict[str, str]] = []
    seen = set()
    for raw_label in labels or []:
        label = normalize_topic_label(str(raw_label))
        topic_type = normalize_topic_type(label)
        identity = (topic_type, label)
        if not topic_type or not label or identity in seen:
            continue
        seen.add(identity)
        catalog.append({"type": topic_type, "label": label})
    return catalog


def normalize_topics(
    raw: Dict[str, Any],
    max_topics: int = _DEFAULT_MAX_TOPICS,
    existing_topics: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    parsed = TopicExtractionResult.model_validate(raw)
    catalog_by_type: Dict[str, str] = {}
    catalog_by_label: Dict[str, tuple[str, str]] = {}
    for existing in existing_topics or []:
        topic_type = normalize_topic_type(str(existing.get("type") or ""))
        label = normalize_topic_label(str(existing.get("label") or ""))
        if not topic_type or not label:
            continue
        catalog_by_type[topic_type] = label
        catalog_by_label[label] = (topic_type, label)

    normalized: List[Dict[str, Any]] = []
    seen = set()
    for topic in parsed.topics:
        topic_type = normalize_topic_type(topic.type)
        label = normalize_topic_label(topic.label)
        if topic_type in catalog_by_type:
            label = catalog_by_type[topic_type]
        elif label in catalog_by_label:
            topic_type, label = catalog_by_label[label]
        else:
            topic_type = normalize_topic_type(label)

        if not topic_type or not label or topic_type in seen:
            continue
        seen.add(topic_type)
        normalized.append(
            {
                "type": topic_type,
                "label": label,
                "phrase": topic.phrase.strip()[:500],
                "evidence_turns": sorted(set(topic.evidence_turns)),
            }
        )
        if len(normalized) >= min(5, max(1, max_topics)):
            break
    return normalized


def format_transcript(transcript: List[Dict[str, Any]]) -> str:
    lines = []
    for position, turn in enumerate(transcript):
        role = str(turn.get("role", "")).lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        turn_number = turn.get("idx", position)
        lines.append(f"[{turn_number}] {role}: {content}")
    return "\n".join(lines)


def validate_topic_evidence(
    topics: List[Dict[str, Any]], transcript: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Keep only topics grounded in a real customer turn."""
    user_turns: Dict[int, str] = {}
    for position, turn in enumerate(transcript):
        if str(turn.get("role", "")).lower() != "user":
            continue
        content = re.sub(r"\s+", " ", str(turn.get("content") or "").strip())
        if content:
            turn_id = int(turn.get("idx", position))
            user_turns[turn_id] = content.lower()

    grounded = []
    for topic in topics:
        evidence = [turn for turn in topic["evidence_turns"] if turn in user_turns]
        phrase = re.sub(r"\s+", " ", topic["phrase"].strip()).lower()
        if not evidence or not phrase:
            continue
        matching_evidence = [turn for turn in evidence if phrase in user_turns[turn]]
        if matching_evidence:
            grounded.append({**topic, "evidence_turns": matching_evidence})
    return grounded


async def extract_topics(
    transcript: List[Dict[str, Any]],
    accepted_topics: Optional[List[str]] = None,
    configuration: Optional[Mapping[str, Any] | str] = None,
) -> List[Dict[str, Any]]:
    formatted = format_transcript(transcript)
    if not formatted:
        return []
    runtime = resolve_topic_evaluation_configuration(configuration)
    max_topics = runtime["settings"]["max_topics"]
    approved_catalog = topic_labels_to_catalog(accepted_topics)
    base_prompt = runtime["system_prompt"]
    if not base_prompt:
        raise ValueError(
            "evaluation_config has no system_prompt; update the global default row"
        )
    prompt = base_prompt.format(
        max_topics=max_topics,
        accepted_topics=json.dumps(approved_catalog, ensure_ascii=False),
    )
    raw_topics = await _request_grid(prompt, formatted, runtime)
    return validate_topic_evidence(
        normalize_topics(
            raw_topics,
            max_topics=max_topics,
            existing_topics=approved_catalog,
        ),
        transcript,
    )
