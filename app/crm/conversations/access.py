"""Who may do what in the Inbox (D19) — PURE.

any user of the merchant   read, take over, reply, note, hand back,
                           resolve, mark read
merchant admins and up     also assign a thread to someone else
login-link sessions        read only: a Nautilus launch token carries a
                           synthetic id ("merchant:<id>", minted by the
                           launch route), not a real user, and a thread
                           can only be held by a real teammate.
"""

from dataclasses import dataclass

from app.schemas import UserInfo, UserRole

#: The id prefix of a login-link (launch) session — the launch route mints
#: ``user_id=f"merchant:{merchant_id}"`` for a session with no users row.
LAUNCH_SESSION_PREFIX = "merchant:"

_MANAGER_ROLES = frozenset({UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT})


@dataclass(frozen=True)
class Actor:
    user_id: str
    read_only: bool
    manager: bool


def actor_of(user: UserInfo) -> Actor:
    """PURE: the Inbox's view of a caller."""
    read_only = str(user.id).startswith(LAUNCH_SESSION_PREFIX)
    return Actor(
        user_id=str(user.id),
        read_only=read_only,
        manager=not read_only and user.role in _MANAGER_ROLES,
    )
