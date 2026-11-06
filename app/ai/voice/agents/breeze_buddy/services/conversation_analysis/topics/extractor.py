"""Topic extraction and output cleanup."""

import asyncio
import json
import re
import time
from typing import Annotated, Any, Dict, List, Mapping, Optional, Tuple, Type, cast

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService
from pydantic import AfterValidator, BaseModel, WithJsonSchema, create_model

from app.ai.voice.agents.breeze_buddy.accounts import Accounts
from app.ai.voice.agents.breeze_buddy.accounts.types import KeyAccount, VertexAccount
from app.ai.voice.agents.breeze_buddy.llm import get_llm_service, resolve_openai
from app.ai.voice.llm import LLMConfiguration, LLMProvider, LLMSdk
from app.ai.voice.llm._pools import get_genai_vertex_client, get_openai_httpx_client
from app.core.config import static
from app.core.logger import logger
from app.schemas.breeze_buddy.conversation_analysis import TopicExtractionResult
from app.services.live_config.store import get_config
from app.services.model_provider import (
    OPENROUTER,
    GenerateRequest,
    GenerationSettings,
    Message,
    ModelProvider,
    OpenRouterProvider,
    ProviderConfig,
    VertexProvider,
    fill,
)

from .segments import validate_segments

_FIRST_TOKEN_TIMEOUT_SECONDS = 30
_GATEWAY_TIMEOUT_SECONDS = 240


class TopicModelResponseError(ValueError):
    """The model answered, but not with usable topics."""


class TopicFirstTokenTimeout(TimeoutError):
    """The gateway sent no token, thinking included, in the first window."""


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

    provider = str(raw.get("provider") or LLMProvider.OPENAI.value).strip()
    if provider not in [p.value for p in LLMProvider] + [OPENROUTER.name]:
        raise ValueError(f"Unsupported topic evaluator provider: {provider}")
    sdk = str(raw.get("sdk") or "").strip() or None
    if sdk and sdk not in [s.value for s in LLMSdk]:
        raise ValueError(f"Unsupported topic evaluator sdk: {sdk}")
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ValueError("evaluation_config.model is required")
    system_prompt = str(raw.get("system_prompt") or "").strip()[:50000] or None
    region = str(raw.get("region") or "").strip() or None
    if provider == LLMProvider.GOOGLE_VERTEX and not region:
        raise ValueError(
            "evaluation_config.region is required for google_vertex provider"
        )
    settings = raw.get("settings")
    settings = dict(settings) if isinstance(settings, Mapping) else {}

    try:
        temperature = float(settings.get("temperature", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "evaluation_config.settings.temperature must be a number"
        ) from exc
    temperature = min(2.0, max(0.0, temperature))

    try:
        max_output_tokens = int(settings.get("max_output_tokens", 16384))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "evaluation_config.settings.max_output_tokens must be an integer"
        ) from exc
    max_output_tokens = min(16384, max(128, max_output_tokens))

    max_topics = settings.get("max_topics")
    if max_topics is not None:
        try:
            max_topics = int(max_topics)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "evaluation_config.settings.max_topics must be an integer"
            ) from exc
        if max_topics < 1:
            raise ValueError("evaluation_config.settings.max_topics must be >= 1")

    include_agent_prompt = settings.get("include_agent_prompt", False)
    if not isinstance(include_agent_prompt, bool):
        raise ValueError(
            "evaluation_config.settings.include_agent_prompt must be true or false"
        )
    stream = settings.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("evaluation_config.settings.stream must be true or false")
    if stream and provider != LLMProvider.OPENAI.value:
        raise ValueError("evaluation_config.settings.stream needs the openai provider")

    return {
        "provider": provider,
        "sdk": sdk,
        "model": model,
        "system_prompt": system_prompt,
        "region": region,
        "settings": {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "max_topics": max_topics,
            "include_agent_prompt": include_agent_prompt,
            "stream": stream,
        },
        "segments": validate_segments(raw.get("segments")),
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
        raise TopicModelResponseError("Topic evaluator response is not a JSON object")
    return value


class _Need(BaseModel):
    summary: str
    evidence_turns: List[int]


class _Topic(BaseModel):
    type: str
    label: str
    phrase: str
    evidence_turns: List[int]


class _TopicReply(BaseModel):
    customer_needs: List[_Need]
    topics: List[_Topic]


def _one_of(values: Tuple[str, ...]) -> Any:
    def check(value: str) -> str:
        if value not in values:
            raise ValueError(f"{value[:80]!r} is not in the known list")
        return value

    return Annotated[
        str,
        AfterValidator(check),
        WithJsonSchema({"type": "string", "enum": list(values)}),
    ]


def _reply_form(catalog: List[Dict[str, str]]) -> Type[BaseModel]:
    two_level = bool(catalog) and all("." in entry["label"] for entry in catalog)
    if not two_level:
        return _TopicReply
    types = tuple(entry["type"] for entry in catalog)
    labels = tuple(entry["label"] for entry in catalog)
    topic = create_model(
        "Topic",
        type=(_one_of(types), ...),
        label=(_one_of(labels), ...),
        phrase=(str, ...),
        evidence_turns=(List[int], ...),
    )
    return create_model(
        "TopicReply",
        customer_needs=(List[_Need], ...),
        topics=(List[topic], ...),
    )


async def _openai_account(runtime: Mapping[str, Any]) -> KeyAccount:
    grid = (await get_config("LITELLM_BASE_URL", "", str)).strip()
    if grid:
        if not static.GRID_TOPICS_API_KEY:
            raise ValueError("GRID_TOPICS_API_KEY is not set in the pod environment")
        endpoint = grid.rstrip("/").removesuffix("/chat/completions")
        return KeyAccount(api_key=static.GRID_TOPICS_API_KEY, endpoint=endpoint)
    gateway = (await get_config("OPENAI_GATEWAY_BASE_URL", "", str)).strip()
    if not gateway:
        raise ValueError(
            "OpenAI gateway base URL is not configured; set OPENAI_GATEWAY_BASE_URL"
        )
    block = LLMConfiguration(
        provider=LLMProvider.OPENAI,
        model=runtime["model"],
        endpoint=gateway.rstrip("/").removesuffix("/chat/completions"),
        api_key_name="OPENAI_GATEWAY_API_KEY",
    )
    return await Accounts().get(block, KeyAccount)


async def _fill_provider(runtime: Mapping[str, Any]) -> Optional[ModelProvider]:
    if runtime["provider"] == OPENROUTER.name:
        return OPENROUTER
    if (
        runtime["provider"] == LLMProvider.OPENAI.value
        and not runtime["settings"]["stream"]
    ):
        account = await _openai_account(runtime)
        return OpenRouterProvider(
            ProviderConfig(
                api_key=account.api_key,
                base_url=f"{account.endpoint}/chat/completions",
                timeout_seconds=_GATEWAY_TIMEOUT_SECONDS,
                max_retries=0,
            ),
            name=LLMProvider.OPENAI.value,
        )
    if (
        runtime["provider"] == LLMProvider.GOOGLE_VERTEX.value
        and runtime.get("sdk") != LLMSdk.ANTHROPIC.value
    ):
        block = LLMConfiguration(
            provider=LLMProvider.GOOGLE_VERTEX,
            model=runtime["model"],
            region=runtime["region"],
        )
        account = await Accounts().get(block, VertexAccount)
        client = get_genai_vertex_client(
            credentials_json=account.credentials_json,
            project_id=account.project_id,
            location=runtime["region"],
        )
        return VertexProvider(client)
    return None


async def _request_llm(
    prompt: str,
    transcript: str,
    runtime: Mapping[str, Any],
    catalog: List[Dict[str, str]],
) -> Dict[str, Any]:
    instruction = prompt + "\n\n" + _PROMPT_ONLY_RESPONSE_INSTRUCTION
    provider = await _fill_provider(runtime)
    if provider is not None:
        started_at = time.monotonic()
        try:
            reply = await fill(
                provider,
                GenerateRequest(
                    model=runtime["model"],
                    input=[Message(role="user", content=transcript)],
                    system_prompt=instruction,
                    settings=GenerationSettings(
                        temperature=runtime["settings"]["temperature"],
                        max_tokens=runtime["settings"]["max_output_tokens"],
                    ),
                ),
                _reply_form(catalog),
            )
        finally:
            if provider is not OPENROUTER:
                await provider.close()
        logger.info(
            f"Topic model answered by {runtime['provider']} ({runtime['model']}) "
            f"in {time.monotonic() - started_at:.1f}s"
        )
        return reply.model_dump()
    llm_config = LLMConfiguration(
        provider=runtime["provider"],
        sdk=runtime.get("sdk"),
        model=runtime["model"],
        region=runtime.get("region"),
        temperature=runtime["settings"]["temperature"],
        max_tokens=runtime["settings"]["max_output_tokens"],
    )
    if runtime["provider"] == LLMProvider.OPENAI.value:
        llm = await resolve_openai(llm_config, await _openai_account(runtime))
    else:
        llm = await get_llm_service(llm_config)
    # AzureLLMService subclasses OpenAILLMService, so Azure evaluations share
    # this pool too, on purpose: without it each one leaks its own client.
    if isinstance(llm, OpenAILLMService):
        llm._client = llm._client.with_options(
            max_retries=0, http_client=get_openai_httpx_client()
        )
    context = LLMContext([{"role": "user", "content": transcript}])
    if runtime["settings"]["stream"]:
        llm = cast(OpenAILLMService, llm)
        params = llm.build_chat_completion_params(
            llm.get_llm_adapter().get_llm_invocation_params(
                context,
                system_instruction=instruction,
                convert_developer_to_user=not llm.supports_developer_role,
            )
        )
        started_at = time.monotonic()
        first_token_s = 0.0
        parts: List[str] = []
        usage = None
        try:
            async with asyncio.timeout(_FIRST_TOKEN_TIMEOUT_SECONDS) as deadline:
                async with await llm._client.chat.completions.create(
                    **params
                ) as stream:
                    async for chunk in stream:
                        usage = chunk.usage or usage
                        delta = chunk.choices[0].delta if chunk.choices else None
                        if not delta:
                            continue
                        thinking = getattr(delta, "reasoning_content", None)
                        if not first_token_s and (delta.content or thinking):
                            first_token_s = time.monotonic() - started_at
                            deadline.reschedule(None)
                        if delta.content:
                            parts.append(delta.content)
        except TimeoutError as exc:
            raise TopicFirstTokenTimeout(
                f"no first token in {_FIRST_TOKEN_TIMEOUT_SECONDS}s"
            ) from exc
        content = "".join(parts)
        details = usage.completion_tokens_details if usage else None
        logger.info(
            f"Topic model answered by {runtime['provider']} in "
            f"{time.monotonic() - started_at:.1f}s: first token {first_token_s:.1f}s, "
            f"{usage.prompt_tokens if usage else '?'} input tokens, "
            f"{usage.completion_tokens if usage else '?'} output tokens "
            f"({details.reasoning_tokens if details else '?'} thinking)"
        )
    else:
        content = await llm.run_inference(context, system_instruction=instruction)
    if not content:
        raise TopicModelResponseError("Topic evaluator returned no content")
    return _decode_json_object(content)


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
        label = re.sub(r"\s+", " ", str(raw_label).strip()).strip(" .,:;!?-_/")[:120]
        topic_type = normalize_topic_type(label)
        identity = (topic_type, label.lower())
        if not topic_type or not label or identity in seen:
            continue
        seen.add(identity)
        catalog.append({"type": topic_type, "label": label})
    return catalog


def normalize_topics(
    raw: Dict[str, Any],
    max_topics: Optional[int],
    existing_topics: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    parsed = TopicExtractionResult.model_validate(raw)
    catalog_by_type: Dict[str, str] = {}
    catalog_by_label: Dict[str, tuple[str, str]] = {}
    for existing in existing_topics or []:
        topic_type = normalize_topic_type(str(existing.get("type") or ""))
        display = str(existing.get("label") or "").strip()
        label = normalize_topic_label(display)
        if not topic_type or not label:
            continue
        catalog_by_type[topic_type] = display
        catalog_by_label[label] = (topic_type, display)

    two_level = bool(catalog_by_type) and all(
        "." in display for display in catalog_by_type.values()
    )
    parents = set()
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
        parent, dot, _ = label.partition(".")
        if two_level and dot:
            if parent.strip().lower() in parents:
                continue
            parents.add(parent.strip().lower())
        seen.add(topic_type)
        normalized.append(
            {
                "type": topic_type,
                "label": label,
                "phrase": topic.phrase.strip()[:500],
                "evidence_turns": sorted(set(topic.evidence_turns)),
            }
        )
        if max_topics is not None and len(normalized) >= max_topics:
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
    prompt = base_prompt
    if max_topics is None:
        prompt = prompt.replace(
            "Return no more than {max_topics} topics.",
            "Return every distinct topic identified.",
        )
    prompt = prompt.replace("{max_topics}", str(max_topics or "unlimited"))
    prompt = prompt.replace(
        "{accepted_topics}",
        json.dumps(approved_catalog, ensure_ascii=False),
    )
    if runtime["settings"]["include_agent_prompt"]:
        # Voice transcripts keep the agent's system messages, one per node it
        # entered; chat stores none, so this adds nothing for chat.
        system_messages = [
            str(turn.get("content") or "").strip()
            for turn in transcript
            if str(turn.get("role", "")).lower() == "system"
        ]
        agent_prompt = "\n\n".join(dict.fromkeys(m for m in system_messages if m))
        if agent_prompt:
            prompt += (
                "\n\nThe agent in this conversation was given these instructions. "
                "Use them only as context about the agent, never as customer "
                "words:\n<agent_instructions>\n"
                + agent_prompt
                + "\n</agent_instructions>"
            )
    raw_topics = await _request_llm(prompt, formatted, runtime, approved_catalog)
    topics = normalize_topics(
        raw_topics,
        max_topics=max_topics,
        existing_topics=approved_catalog,
    )
    grounded = validate_topic_evidence(topics, transcript)
    # A topic whose phrase is not in the customer's own words is dropped here;
    # a high drop rate means the prompt or model paraphrases.
    logger.bind(
        model_topic_count=len(topics),
        grounded_topic_count=len(grounded),
        ungrounded_topic_count=len(topics) - len(grounded),
    ).info(f"Topic extraction kept {len(grounded)} of {len(topics)} topics")
    return grounded
