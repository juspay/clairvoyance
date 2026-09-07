"""Agentic-payments policy: which operators the standing mandate may pay,
the default limits, the chooser amounts, and the seller on the ticket cart.

TEMPORARY HOME — environment variables (UAP_*). This is merchant policy, not
a secret, and it belongs with the merchant (template ``configurations`` or a
merchant-scoped config row) so two merchants — or two templates of one
merchant — can differ. Today there is exactly one agentic merchant (Chennai
One), so the policy is read from the service environment and applies to
every tenant this service serves. When a second merchant arrives, move it:
the model below is the contract, only ``load_agentic_policy`` changes.
"""

from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.config.static import (
    UAP_LIMIT_CHOICES,
    UAP_MAX_DRAWS,
    UAP_MAX_PER_DRAW,
    UAP_MAX_TOTAL,
    UAP_SELLER_MIC,
    UAP_SELLER_NAME,
    UAP_VALIDITY_DAYS,
    UAP_VERIFIED_NAMES,
)


class AgenticPaymentLimits(BaseModel):
    """Defaults for the standing rule a rider approves. Decimal rupee strings."""

    max_per_draw: Optional[str] = Field(default=None, pattern=r"^\d+\.\d{2}$")
    max_total: Optional[str] = Field(default=None, pattern=r"^\d+\.\d{2}$")
    max_draws: Optional[int] = Field(default=None, ge=1, le=10000)
    validity_days: Optional[int] = Field(default=None, ge=1, le=365)


class AgenticPaymentsConfig(BaseModel):
    """Merchant policy for agentic (UAP) payments — read by the /uap routes.
    Required in full (operators, seller, all four limits): the code holds
    no defaults, a policy missing any of them cannot onboard or draw."""

    # AE-verified legal names of the operators the agent may pay
    # (intent_constraints.bound_verified_names).
    verified_names: List[str] = Field(default_factory=list)
    limits: Optional[AgenticPaymentLimits] = None
    # Per-ticket amounts offered in the page's limit chooser, in order.
    limit_choices: List[str] = Field(default_factory=list)
    # Seller on the /txns ticket cart: the operator receiving the money.
    seller_name: Optional[str] = None
    seller_mic: Optional[str] = None


def _csv(raw: str) -> List[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _int(raw: str) -> Optional[int]:
    try:
        return int(raw) if raw.strip() else None
    except ValueError:
        return None


def load_agentic_policy() -> Tuple[Optional[AgenticPaymentsConfig], List[str]]:
    """(policy, missing-or-invalid field names). The policy is None when a
    required field is absent or malformed — callers fail closed."""
    missing: List[str] = []
    try:
        limits = AgenticPaymentLimits(
            max_per_draw=UAP_MAX_PER_DRAW or None,
            max_total=UAP_MAX_TOTAL or None,
            max_draws=_int(UAP_MAX_DRAWS),
            validity_days=_int(UAP_VALIDITY_DAYS),
        )
    except ValueError as exc:
        return None, [f"limits: {exc}"]
    cfg = AgenticPaymentsConfig(
        verified_names=_csv(UAP_VERIFIED_NAMES),
        limits=limits,
        limit_choices=_csv(UAP_LIMIT_CHOICES),
        seller_name=UAP_SELLER_NAME or None,
        seller_mic=UAP_SELLER_MIC or None,
    )
    for name, value in (
        ("UAP_VERIFIED_NAMES", cfg.verified_names),
        ("UAP_SELLER_NAME", cfg.seller_name),
        ("UAP_SELLER_MIC", cfg.seller_mic),
        ("UAP_MAX_PER_DRAW", limits.max_per_draw),
        ("UAP_MAX_TOTAL", limits.max_total),
        ("UAP_MAX_DRAWS", limits.max_draws),
        ("UAP_VALIDITY_DAYS", limits.validity_days),
    ):
        if not value:
            missing.append(name)
    return (None if missing else cfg), missing
