"""Exceptions for wallet domain services."""


class UnsupportedCurrencyError(Exception):
    """Raised when a currency has no entry in the currency-to-credits map."""

    def __init__(self, currency: str):
        self.currency = currency
        super().__init__(f"Unsupported currency: {currency}")


class InvalidRechargeAmountError(Exception):
    """Raised when a recharge amount is not strictly positive.

    services.wallet.recharge is a public function and can be called directly
    (bypassing WalletRechargeRequest's Pydantic validation), so this invariant
    is re-checked at the service layer as well.
    """

    def __init__(self, amount):
        self.amount = amount
        super().__init__(f"Recharge amount must be positive, got {amount}")
