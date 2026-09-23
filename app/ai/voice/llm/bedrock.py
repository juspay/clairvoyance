"""AWS Bedrock LLM config and builder.

Targets Bedrock's native Converse API through pipecat's
``AWSBedrockLLMService`` (``aioboto3``): every Converse model is reachable and
the OpenAI-compatibility layer's limits (tools vs reasoning, temperature)
do not apply.

Auth is a Bedrock API key when the template names one (``api_key_name``),
installed as the bearer token of the service's own botocore session;
otherwise the pod's default AWS credential chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import aioboto3
from aiobotocore.session import AioSession
from botocore.config import Config
from botocore.tokens import ScopedEnvTokenProvider
from pipecat.services.aws.llm import AWSBedrockLLMService, AWSBedrockLLMSettings

from app.core.logger import logger

__all__ = ["BedrockConfig", "build_bedrock_llm", "is_openai_model"]

# botocore looks a Bedrock API key up by this name; ``_api_key_session`` feeds
# it a private dict instead of ``os.environ`` so concurrent services never
# share one key, and the in-code ``signature_version`` keeps botocore's
# bearer-preference handler from consulting the real environment.
_BEARER_TOKEN_ENV = "AWS_BEARER_TOKEN_BEDROCK"


def is_openai_model(model: str) -> bool:
    """Bedrock model ids are ``[region.]vendor.name``; GPT models carry ``openai``."""
    return "openai" in model.split(".")


@dataclass
class BedrockConfig:
    """Configuration for AWS Bedrock (Converse API)."""

    model: str
    region: str
    max_tokens: int
    api_key: Optional[str] = None
    temperature: Optional[float] = None
    reasoning_effort: Optional[str] = None
    thinking_budget_tokens: Optional[int] = None
    function_call_timeout_secs: float = 10.0


def build_bedrock_llm(config: BedrockConfig) -> AWSBedrockLLMService:
    """Create an AWS Bedrock LLM service.

    ``temperature`` is sent only when set; GPT models take it as a native
    request field and only with reasoning off. Reasoning rides
    ``additionalModelRequestFields`` in the model family's own shape. Prompt
    caching stays off: GPT models cache the prompt prefix automatically and
    reject explicit cache points.
    """
    logger.info(
        f"Building Bedrock LLM service with model={config.model}, "
        f"region={config.region}, reasoning_effort={config.reasoning_effort}, "
        f"auth={'api_key' if config.api_key else 'default_chain'}"
    )

    request_fields: dict[str, Any] = {}
    if config.reasoning_effort:
        request_fields["reasoning"] = {"effort": config.reasoning_effort}
    elif config.thinking_budget_tokens:
        request_fields["thinking"] = {
            "type": "enabled",
            "budget_tokens": config.thinking_budget_tokens,
        }

    settings_kwargs: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "additional_model_request_fields": request_fields,
    }
    if config.temperature is not None:
        if is_openai_model(config.model):
            request_fields["temperature"] = config.temperature
        else:
            settings_kwargs["temperature"] = config.temperature

    service = AWSBedrockLLMService(
        aws_region=config.region,
        settings=AWSBedrockLLMSettings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
    if config.api_key:
        # pipecat 1.1.0 builds the session and client config in __init__ with
        # no hook to supply them; re-verify on upgrade.
        service._aws_session = _api_key_session(config.api_key)
        service._aws_params["config"] = service._aws_params["config"].merge(
            Config(signature_version="bearer")
        )
    return service


def _api_key_session(api_key: str) -> aioboto3.Session:
    session = AioSession()
    session.register_component(
        "token_provider",
        ScopedEnvTokenProvider(session, environ={_BEARER_TOKEN_ENV: api_key}),
    )
    return aioboto3.Session(botocore_session=session)
