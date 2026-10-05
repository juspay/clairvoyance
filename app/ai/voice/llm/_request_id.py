"""Log the LLM provider's request id for every request a service makes.

Every provider stamps a request id on the response (Azure OpenAI
``apim-request-id``, OpenAI ``x-request-id``, Bedrock ``x-amzn-RequestId``).
It is the handle the provider's support team needs to trace a slow request.
Each builder calls :func:`log_request_ids` once on the service it built.

Hooks the HTTP client below pipecat: an httpx response hook for OpenAI-SDK
services (OpenAI, Azure), a botocore ``after-call`` handler for Bedrock.
Best-effort: a setup failure is logged and never affects the call.
"""

from __future__ import annotations

from typing import Any

from app.core.logger import logger

__all__ = ["log_request_ids"]

# Request-id headers an OpenAI-SDK response may carry. Azure sends both its
# gateway id (apim-request-id) and the backend id (x-request-id); every one
# present is logged, since support asks for different ones.
_HEADERS = ("apim-request-id", "x-ms-request-id", "x-request-id")


def log_request_ids(service: Any, *, label: str) -> None:
    """Log the provider request id of every request ``service`` makes."""
    try:
        if _hook_httpx(service, label) or _hook_botocore(service, label):
            return
        logger.info(f"{label}: no HTTP client found; request ids not logged")
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        logger.opt(exception=exc).warning(f"{label}: request id hook not attached")


def _hook_httpx(service: Any, label: str) -> bool:
    """OpenAI-SDK services: ``service._client`` is the SDK client, whose
    ``_client`` is the httpx AsyncClient every request goes through."""
    http_client = getattr(getattr(service, "_client", None), "_client", None)
    if http_client is None or not isinstance(
        getattr(http_client, "event_hooks", None), dict
    ):
        return False

    async def on_response(response: Any) -> None:
        found = [
            f"{name}={value}"
            for name in _HEADERS
            if (value := response.headers.get(name))
        ]
        if found:
            logger.info(f"{label} request id: {' '.join(found)}")

    hooks = {k: list(v) for k, v in http_client.event_hooks.items()}
    hooks.setdefault("response", []).append(on_response)
    http_client.event_hooks = hooks
    return True


def _hook_botocore(service: Any, label: str) -> bool:
    """Bedrock: every client the service opens comes from ``_aws_session``,
    whose botocore session emits ``after-call`` with the parsed response."""
    core = getattr(getattr(service, "_aws_session", None), "_session", None)
    if core is None or not callable(getattr(core, "register", None)):
        return False

    def after_call(parsed: Any = None, **_: Any) -> None:
        request_id = ((parsed or {}).get("ResponseMetadata") or {}).get("RequestId")
        if request_id:
            logger.info(f"{label} request id: {request_id}")

    core.register("after-call.bedrock-runtime", after_call)
    return True
