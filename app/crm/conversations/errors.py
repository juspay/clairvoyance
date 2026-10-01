"""Conversations' refusals — each family earns one status code in api.py's
TranslatingRoute, and each message is written for the person who acted."""


class ConversationError(Exception):
    """Base: a request understood and refused (400)."""


class ThreadNotFound(ConversationError):
    """No such thread for this merchant (404)."""


class ThreadConflict(ConversationError):
    """The thread moved under the request — someone else took it, it was
    resolved, the window shut (409)."""


class NotAllowed(ConversationError):
    """This caller may not do this here — not the thread's teammate, a
    read-only session, human handoff switched off (403)."""
