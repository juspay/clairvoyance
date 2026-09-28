"""The saved widget look carries the panel's colours past the brand colour."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.breeze_buddy.widget_config import WidgetAppearance

LOOK = {
    "primary_color": "#1c1c1c",
    "theme": "dark",
    "surface_color": "#141416",
    "text_color": "#f2f2f3",
    "user_bubble_bg": "#1c1c1c",
    "user_bubble_fg": "#ffffff",
    "quick_reply_bg": "#232326",
    "quick_reply_fg": "#f2f2f3",
    "quick_reply_color": "#1c1c1c",
}


def test_the_whole_look_round_trips() -> None:
    saved = WidgetAppearance.model_validate(LOOK).model_dump(exclude_none=True)
    assert saved == LOOK


def test_only_the_two_themes_are_accepted() -> None:
    with pytest.raises(ValidationError):
        WidgetAppearance.model_validate({"theme": "sepia"})
