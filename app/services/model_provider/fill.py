from dataclasses import replace
from typing import Any, Mapping, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.logger import logger
from app.services.model_provider.base import ModelProvider
from app.services.model_provider.models import (
    GenerateRequest,
    GenerateResponse,
    Message,
    ReplyError,
)

FormT = TypeVar("FormT", bound=BaseModel)

REPLY_AS_JSON = "Reply with one JSON object only."
ASK_AGAIN = (
    "That reply does not fit the required JSON ({problems}). "
    "Reply again with only the corrected JSON object."
)


def without_refs(node: Any, definitions: Mapping[str, Any]) -> Any:
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.removeprefix("#/$defs/")
            return without_refs(definitions[name], definitions)
        return {
            key: without_refs(value, definitions)
            for key, value in node.items()
            if key != "$defs"
        }
    if isinstance(node, list):
        return [without_refs(item, definitions) for item in node]
    return node


def _fit(form: Type[FormT], response: GenerateResponse) -> Tuple[Optional[FormT], str]:
    try:
        return form.model_validate(response.structured), ""
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(map(str, detail['loc'])) or 'reply'}: {detail['msg']}"
            for detail in error.errors()[:3]
        )
        return None, problems


async def fill(
    provider: ModelProvider, request: GenerateRequest, form: Type[FormT]
) -> FormT:
    conversation = request.input
    if isinstance(conversation, Mapping):
        raise ValueError("fill needs a conversation input, not a mapping")

    system_prompt = REPLY_AS_JSON
    if request.system_prompt:
        system_prompt = f"{request.system_prompt}\n\n{REPLY_AS_JSON}"
    schema = form.model_json_schema()
    request = replace(
        request,
        system_prompt=system_prompt,
        schema=without_refs(schema, schema.get("$defs", {})),
    )

    calls = 1
    response = await provider.generate(request)
    filled, problems = _fit(form, response)
    if filled is None:
        bad_reply = Message(
            role="assistant", content=response.content or "(empty reply)"
        )
        ask_again = Message(role="user", content=ASK_AGAIN.format(problems=problems))
        calls = 2
        response = await provider.generate(
            replace(request, input=[*conversation, bad_reply, ask_again])
        )
        filled, problems = _fit(form, response)
    if filled is None:
        raise ReplyError(
            provider.name,
            f"reply does not fit {form.__name__} after asking again: {problems}",
        )

    usage = response.usage
    logger.info(
        f"{provider.name} {response.model} filled {form.__name__} in {calls} call(s): "
        f"{usage.input_tokens if usage else '?'} input tokens, "
        f"{usage.output_tokens if usage else '?'} output tokens"
    )
    return filled
