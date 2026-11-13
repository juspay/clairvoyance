"""A widget's hourly limits cannot be set to 0.

The rate limiter reads a limit of 0 as "limit off" (an ops switch). The
widget config API accepted 0, so one request could remove the per-IP cap on
a widget's anonymous sessions, messages or voice sessions.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.breeze_buddy.widget_config import (
    WidgetConfigCreate,
    WidgetConfigUpdate,
)

HOURLY_LIMITS = [
    "max_sessions_per_ip_hour",
    "max_messages_per_ip_hour",
    "max_voice_sessions_per_ip_hour",
]
CREATE = {"reseller_id": "R1", "merchant_id": "acme-shop", "template_id": "t1"}


@pytest.mark.parametrize("field", HOURLY_LIMITS)
@pytest.mark.parametrize("value", [0, -1])
def test_create_refuses_a_limit_below_one(field: str, value: int) -> None:
    with pytest.raises(ValidationError) as refused:
        WidgetConfigCreate.model_validate({**CREATE, field: value})
    assert [error["loc"] for error in refused.value.errors()] == [(field,)]


@pytest.mark.parametrize("field", HOURLY_LIMITS)
@pytest.mark.parametrize("value", [0, -1])
def test_update_refuses_a_limit_below_one(field: str, value: int) -> None:
    with pytest.raises(ValidationError) as refused:
        WidgetConfigUpdate.model_validate({field: value})
    assert [error["loc"] for error in refused.value.errors()] == [(field,)]


@pytest.mark.parametrize("field", HOURLY_LIMITS)
def test_one_is_the_least_a_limit_can_be(field: str) -> None:
    assert getattr(WidgetConfigCreate.model_validate({**CREATE, field: 1}), field) == 1
    assert getattr(WidgetConfigUpdate.model_validate({field: 1}), field) == 1
