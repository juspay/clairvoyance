"""Exceptions for wallet domain services."""


class UnsupportedCurrencyError(Exception):
    """Raised when a currency has no entry in the currency-to-credits map."""

    def __init__(self, currency: str):
        self.currency = currency
        super().__init__(f"Unsupported currency: {currency}")


class InvalidRechargeAmountError(Exception):
    """Raised when a recharge amount is not strictly positive.

    services.breeze_buddy.wallet.recharge is a public function and can be called directly
    (bypassing WalletRechargeRequest's Pydantic validation), so this invariant
    is re-checked at the service layer as well.
    """

    def __init__(self, amount):
        self.amount = amount
        super().__init__(f"Recharge amount must be positive, got {amount}")


class InsufficientCreditsError(Exception):
    """Raised when a merchant's wallet balance is insufficient to allow a
    billable event to proceed (e.g. a chat turn reaching the LLM).

    Scaffolded originally in the phase-1 wallet design but unused until the
    chat-deduction feature -- zero/negative balance was explicitly allowed
    for recharges/deductions in general, but new billable events (like chat
    turns) are gated on a sufficient-balance pre-check before they start.
    """

    def __init__(self, merchant_id: str):
        self.merchant_id = merchant_id
        super().__init__(f"Insufficient credits for merchant '{merchant_id}'")


class UnknownEventTypeError(Exception):
    """Raised when deduct() is called with an event_type not present in
    BILLING_RULES."""

    def __init__(self, event_type: str):
        self.event_type = event_type
        super().__init__(f"Unknown billing event type: {event_type}")


class WalletNotFoundError(Exception):
    """Raised when a wallet operation targets a merchant with no wallet row."""

    def __init__(self, merchant_id: str):
        self.merchant_id = merchant_id
        super().__init__(f"No wallet found for merchant '{merchant_id}'")
