import json
from typing import List, Mapping, Optional

from google import genai
from google.genai import errors, types

from app.services.model_provider.models import (
    GenerateRequest,
    GenerateResponse,
    ProviderError,
    ReplyError,
    Usage,
)


class VertexProvider:
    name = "google_vertex"

    def __init__(self, client: genai.Client) -> None:
        self._client = client

    async def close(self) -> None:
        return None

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        if isinstance(request.input, Mapping):
            raise ProviderError(
                self.name, "input must be a sequence of messages, not a mapping"
            )
        config = types.GenerateContentConfig(
            system_instruction=request.system_prompt,
            temperature=request.settings.temperature,
            max_output_tokens=request.settings.max_tokens,
            top_p=request.settings.top_p,
            stop_sequences=(
                list(request.settings.stop) if request.settings.stop else None
            ),
        )
        if request.schema is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = dict(request.schema)
        contents: List[types.ContentUnion] = [
            types.Content(
                role="user" if message.role == "user" else "model",
                parts=[types.Part(text=message.content)],
            )
            for message in request.input
        ]
        try:
            reply = await self._client.aio.models.generate_content(
                model=request.model, contents=contents, config=config
            )
        except errors.APIError as error:
            raise ProviderError(
                self.name,
                str(error),
                status=error.code,
                retryable=error.code == 429 or error.code >= 500,
            ) from error

        candidate = reply.candidates[0] if reply.candidates else None
        parts: List[types.Part] = []
        if candidate is not None and candidate.content is not None:
            parts = candidate.content.parts or []
        content = "".join(part.text for part in parts if part.text and not part.thought)
        finish_reason: Optional[str] = None
        if candidate is not None and candidate.finish_reason is not None:
            finish_reason = candidate.finish_reason.value

        structured: Optional[object] = None
        if request.schema is not None:
            if finish_reason == types.FinishReason.MAX_TOKENS.value:
                raise ReplyError(
                    self.name,
                    f"reply truncated at max_tokens={request.settings.max_tokens}",
                )
            try:
                structured = json.loads(content)
            except json.JSONDecodeError:
                structured = None

        usage = reply.usage_metadata
        return GenerateResponse(
            content=content,
            model=request.model,
            structured=structured,
            finish_reason=finish_reason,
            usage=(
                Usage(
                    input_tokens=usage.prompt_token_count or 0,
                    output_tokens=(usage.candidates_token_count or 0)
                    + (usage.thoughts_token_count or 0),
                )
                if usage is not None
                else None
            ),
        )
