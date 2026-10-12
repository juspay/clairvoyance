"""Closes the reply rows a crashed send left behind.

A session send (session.py) writes its manifest row in flight, with no
claim, and closes it itself. The dispatcher never claims such a row — its
words are not on it — so if the sender dies mid-reply, nothing else would
ever close it. The dispatcher runs this sweep on every pass, beside its own
stale-claim sweep (outbound/dispatch.py).
"""

from app.core.config.static import CRM_DISPATCH_STALE_MINUTES
from app.core.logger import logger
from app.crm.connectivity.db.accessors import message as message_accessor
from app.crm.connectivity.messaging.logs import sample_ids


async def close_abandoned_sessions() -> None:
    """Session sends (free-form replies) are never claimed and never
    requeued — their words are not on the row — so a sender that died
    mid-reply leaves a row only this sweep can close, as dead."""
    abandoned = await message_accessor.abandon_stale_session_sends(
        CRM_DISPATCH_STALE_MINUTES
    )
    if abandoned:
        logger.error(
            f"crm dispatch closed {len(abandoned)} abandoned session send(s): "
            f"{sample_ids(abandoned)}"
        )
