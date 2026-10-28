"""Handle kinds stored as ROWS (crm_customer_handle) — the vocabulary,
defined ONCE: identity resolves on it, record reads it from payloads,
outreach keeps it out of template variables and predicates.

The five canon handles — phone, email, igsid, shopify_customer_id,
external_ref — are COLUMNS on crm_customer and are not listed here. A new
channel's id is a ROW KIND: one entry in ROW_KINDS, no migration. Each entry
is the kind's normalizer: the stored form is the probed form, so the one
function decides both, and anything it cannot accept (blank, a placeholder,
the wrong shape) comes back None and is dropped — the same treatment an
unparseable phone gets — rather than stored as an id many people share.

Every kind here is unscoped today: its ids are unique on their own, so the
row's scope is UNSCOPED. A kind whose ids repeat across accounts (Slack per
workspace, Instagram per business account) brings its scope with it when it
is added.
"""

import re
from typing import Callable, Dict, FrozenSet, Optional

#: The scope of every kind below — their ids are unique on their own.
UNSCOPED = ""

# A Xyne user id is the primary key of Xyne's User table: a cuid
# (xyne-spaces prisma/schema.prisma, model User). The shape is kept loose so
# a change of id generator on their side does not silently drop people;
# the blocked values are the placeholders that would otherwise staple
# strangers together — "env-api-user" is the id Xyne answers with when its
# own user lookup fails (services/apiKeyService.ts).
_XYNE_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_XYNE_BLOCKED = frozenset({"0", "null", "none", "undefined", "env-api-user"})


def normalize_xyne_user_id(raw: str) -> Optional[str]:
    """The stored form of a Xyne user id, or None for anything that is not
    one person's id."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if value.lower() in _XYNE_BLOCKED or not _XYNE_USER_ID.match(value):
        return None
    return value


#: kind -> its normalizer. Insertion order is the probe order, after the
#: crm_customer columns.
ROW_KINDS: Dict[str, Callable[[str], Optional[str]]] = {
    "xyne_user_id": normalize_xyne_user_id,
}

ROW_KIND_NAMES: FrozenSet[str] = frozenset(ROW_KINDS)


def normalize_row_handle(kind: str, raw: str) -> Optional[str]:
    """PURE: the stored form of a row-kind handle, or None — for a kind
    this file does not know, too."""
    normalize = ROW_KINDS.get(kind)
    return normalize(raw) if normalize is not None else None
