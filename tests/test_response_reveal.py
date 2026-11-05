"""``response_reveal``: template field → widget surface block, presentation-only."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel
from app.api.routers.breeze_buddy.widget.handlers import (
    _extract_widget_config,
    _surface_wire,
)
from app.schemas.breeze_buddy.chat import WidgetSurfaceWire


def test_template_field_defaults_to_stream_and_is_closed():
    assert ConfigurationModel().response_reveal == "stream"
    assert ConfigurationModel(response_reveal="complete").response_reveal == "complete"
    with pytest.raises(ValidationError):
        ConfigurationModel.model_validate({"response_reveal": "instant"})


async def test_surface_block_carries_the_template_value():
    template = SimpleNamespace(
        configurations=ConfigurationModel(response_reveal="complete"),
        supported_channels=["chat"],
    )
    surface = _extract_widget_config(template)
    assert surface.response_reveal == "complete"
    # The builder is sync today and becomes async once the session overlay
    # lands (it fetches registry defs); this test must pass on both sides
    # of that merge, whichever order the two PRs land in.
    block = _surface_wire(surface, template, catalog_active="v1", ui_flavors=[])
    if inspect.isawaitable(block):
        block = await block
    assert block.response_reveal == "complete"
    # absent configurations → the wire default, never a missing key
    bare = _extract_widget_config(SimpleNamespace(configurations=None))
    assert bare.response_reveal == "stream"
    assert WidgetSurfaceWire().response_reveal == "stream"
