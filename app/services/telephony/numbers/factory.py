"""
Factory for instantiating NumberProviders.
"""

from app.schemas.breeze_buddy.core import CallProvider
from app.services.telephony.numbers.provider import NumberProvider
from app.services.telephony.numbers.providers.plivo import PlivoNumberProvider


def get_number_provider(provider_name: CallProvider) -> NumberProvider:
    """Returns the appropriate NumberProvider instance for the given provider type."""
    if provider_name == CallProvider.PLIVO:
        return PlivoNumberProvider()
    # Support for TWILIO or others can be added here
    raise ValueError(
        f"Provider {provider_name} is not supported for number purchasing."
    )
