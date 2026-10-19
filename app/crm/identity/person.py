"""One person, however many records — the reads across a merge.

A merge staples the younger record to the older one (canon/01-identity.md:
"staple, never melt"; resolve.py). Everything stamped before the merge —
runs, letters — keeps the record it was stamped with, so a question about
the PERSON must read every record she has: the canon's "reads across a
pair", `id = $2 OR merged_into_id = $2`. Other modules cannot read
crm_customer, so they ask here, through contracts.

A customer nobody ever merged is one record, and gets exactly that back:
every caller then reads what it read before this file existed.
"""

from typing import Dict, List

from app.crm.identity.db import accessor


async def person_records(merchant_id: str, customer_id: str) -> List[str]:
    """Every record id of the person ``customer_id`` is — the survivor
    first, then the records stapled into it. Never merged: just
    ``[customer_id]``. An id identity does not know is returned as itself
    too, so the caller reads by it exactly as before."""
    return await accessor.person_records(merchant_id, customer_id) or [customer_id]


async def merged_pairs(merchant_id: str) -> Dict[str, str]:
    """Every merged-away record of the merchant -> its survivor (one hop).
    Empty for a merchant nobody was ever merged in."""
    return await accessor.merged_pairs(merchant_id)
