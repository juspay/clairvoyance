"""The dashboard's LLM provider menus are built from the playground options
endpoint, so Azure stays off them by being left out here. Azure itself still
runs for templates that already name it."""

from app.ai.voice.llm.types import LLMProvider
from app.api.routers.breeze_buddy.playground.handlers import (
    get_configuration_options_handler,
)


async def test_azure_is_not_offered():
    options = await get_configuration_options_handler()
    offered = [p["value"] for p in options["llm_providers"]]
    assert offered == [p.value for p in LLMProvider if p is not LLMProvider.AZURE]
    assert "azure" not in offered


async def test_azure_is_still_a_backend_provider():
    assert LLMProvider("azure") is LLMProvider.AZURE
