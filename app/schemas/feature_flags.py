"""Feature flags schemas."""

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
#  Targeting / A-B test flag structures
# ---------------------------------------------------------------------------


class FlagDistribution(BaseModel):
    """Variation assignment entry within a targeting rule.

    ``_variation`` is the variation id; ``percentage`` is the share of
    matched users (0-100). All entries in a single target's distribution
    list must sum to 100.
    """

    variation: str = Field(..., alias="_variation")
    percentage: float = Field(..., ge=0, le=100)

    model_config = {"populate_by_name": True}


class FlagAudienceFilter(BaseModel):
    """A single predicate applied against an incoming user context.

    Supported ``type`` + ``subType`` combinations:
    - ``type="user"``, ``subType="email"``  → match ``user_email``
    - ``type="user"``, ``subType="userId"`` → match ``user_id``
    - ``type="customData"``, ``subType=<key>`` → match ``custom_data[key]``

    Supported ``comparator`` values:
    - ``"="``        exact match (value in ``values`` list)
    - ``"!="``       not in ``values`` list
    - ``"contain"``  any entry in ``values`` is a substring of the candidate
    - ``"!contain"`` no entry in ``values`` is a substring
    """

    type: str
    subType: str
    comparator: str = "="
    values: List[Any]


class FlagAudienceFilters(BaseModel):
    filters: List[FlagAudienceFilter]
    operator: Literal["and", "or"] = "and"


class FlagAudience(BaseModel):
    filters: FlagAudienceFilters


class FlagTarget(BaseModel):
    """One targeting rule: audience match → bucketed variation assignment."""

    name: str = ""
    distribution: List[FlagDistribution]
    audience: FlagAudience


class TargetingFlagValue(BaseModel):
    """A feature flag that supports user-based targeting and A/B variation.

    Example payload for POST /feature-flags::

        {
          "flags": {
            "MY_AB_FLAG": {
              "has_targeting": true,
              "value": "control",
              "targets": [
                {
                  "name": "Beta users",
                  "distribution": [
                    {"_variation": "treatment", "percentage": 50},
                    {"_variation": "control",   "percentage": 50}
                  ],
                  "audience": {
                    "filters": {
                      "operator": "and",
                      "filters": [
                        {
                          "type": "user",
                          "subType": "email",
                          "comparator": "=",
                          "values": ["alice@example.com"]
                        }
                      ]
                    }
                  }
                }
              ],
              "variation_values": {
                "control":   "value_a",
                "treatment": "value_b"
              }
            }
          }
        }
    """

    has_targeting: Literal[True]
    value: Any = Field(..., description="Default value when no target matches")
    targets: List[FlagTarget]
    variation_values: Dict[str, Any]


# ---------------------------------------------------------------------------
#  CRUD request / response models
# ---------------------------------------------------------------------------


class FeatureFlagUpdate(BaseModel):
    """Update one or more feature flags.

    Each key maps to either:
    - A **simple scalar** (``str``, ``int``, ``bool``, ``float``) — plain flag.
    - A :class:`TargetingFlagValue` dict — flag with A/B targeting rules.
    """

    flags: Dict[str, Any] = Field(
        ...,
        description=(
            "Flag key-value pairs to upsert. Values may be plain scalars "
            "or TargetingFlagValue objects for A/B / user-targeted flags."
        ),
    )


class FeatureFlagResponse(BaseModel):
    flags: Dict[str, Any]
    total_count: int


class FeatureFlagUpdateResponse(BaseModel):
    status: str
    message: str
    updated_flags: list[str]
    total_flags: int


class FeatureFlagDeleteResponse(BaseModel):
    status: str
    message: str
    remaining_flags: int
