"""
NumberProvider interface.
"""

from abc import ABC, abstractmethod

from app.schemas.breeze_buddy.telephony_numbers import (
    ProviderBuyResult,
    TelephonyNumberBuyRequest,
    TelephonyNumberSearchParams,
    TelephonyNumberSearchResponse,
)


class NumberProvider(ABC):
    """Abstract base class for telephony number providers."""

    @abstractmethod
    async def search_numbers(
        self, params: TelephonyNumberSearchParams
    ) -> TelephonyNumberSearchResponse:
        """Search available phone numbers from the provider inventory."""

    @abstractmethod
    async def buy_number(self, request: TelephonyNumberBuyRequest) -> ProviderBuyResult:
        """Purchase a phone number from the provider.

        Returns the normalized purchase result. Implementations raise on
        transport/API failure; a non-fulfilled purchase is returned, not raised.
        """

    @abstractmethod
    async def unrent_number(self, number: str) -> bool:
        """Unrent (release) a phone number from the provider account.

        Used to roll back a purchase when the number cannot be registered in our
        database. Must not raise -- returns False if the release failed.
        """
