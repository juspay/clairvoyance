"""Response schemas for merchant endpoints."""

from typing import List

from pydantic import BaseModel


class MerchantsResponse(BaseModel):
    """Response for listing all merchants.

    Returns unique shop_identifiers from call_execution_config.
    Each shop_identifier represents a distinct merchant in the system.
    """

    merchants: List[str]
    total: int
