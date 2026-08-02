"""
Currency conversion logic for the wallet feature.

Pure domain logic, no database or I/O access. Converts a paid amount in a
given currency into platform credits using a static currency-to-credits map
combined with the merchant-specific conversion_rate multiplier stored on
wallets.conversion_rate.
"""

from decimal import Decimal
from typing import Dict

from app.services.wallet.exceptions import UnsupportedCurrencyError

# How many credits 1 unit of each currency is worth, before applying the
# merchant-specific conversion_rate multiplier.
CURRENCY_TO_CREDITS: Dict[str, Decimal] = {
    "INR": Decimal(1),
    "USD": Decimal(60),
}


def compute_credits(
    amount: Decimal, currency: str, conversion_rate: Decimal
) -> Decimal:
    """Compute the number of credits an amount paid in a given currency is
    worth, for a merchant with the given conversion_rate.

    credits = amount * CURRENCY_TO_CREDITS[currency] * conversion_rate

    Raises:
        UnsupportedCurrencyError: if currency is not in CURRENCY_TO_CREDITS.
    """
    currency = currency.upper()
    if currency not in CURRENCY_TO_CREDITS:
        raise UnsupportedCurrencyError(currency)
    return amount * CURRENCY_TO_CREDITS[currency] * conversion_rate
