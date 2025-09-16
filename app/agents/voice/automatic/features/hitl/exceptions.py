"""
Custom exceptions for voice agent function confirmation system.
These exceptions signal to the LLM framework that operations should NOT be retried.
"""


class HITLUserRejectedOperationError(Exception):
    """
    Raised when user explicitly rejects a dangerous operation.
    But can be retried if user changes their mind.
    """


class HITLOperationTimeoutError(Exception):
    """
    Raised when operation times out waiting for user response.
    This signals to the LLM that the operation should not be retried.
    """


class HITLConfirmationError(Exception):
    """
    Raised when confirmation process fails due to system errors.
    This signals to the LLM that the operation should not be retried.
    """
