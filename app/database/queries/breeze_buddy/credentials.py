"""
Database query functions for the credentials table.
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

CREDENTIALS_TABLE = "credentials"
TEMPLATE_TABLE = "template"  # the same word queries/breeze_buddy/template.py owns


def insert_credential_query(
    id: str,
    reseller_id: Optional[str],
    name: str,
    credential_type: str,
    value: str,
    is_encrypted: bool,
    description: Optional[str],
    merchant_id: Optional[str] = None,
    provider: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to insert a credential record."""
    text = f"""
        INSERT INTO "{CREDENTIALS_TABLE}"
        ("id", "reseller_id", "merchant_id", "name", "credential_type", "value",
         "is_encrypted", "description", "is_active", "created_at", "updated_at",
         "provider")
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE, $9, $10, $11)
        RETURNING *;
    """
    values = [
        id,
        reseller_id,
        merchant_id,
        name,
        credential_type,
        value,
        is_encrypted,
        description,
        datetime.now(),
        datetime.now(),
        provider,
    ]
    return text, values


def get_credential_by_id_query(
    credential_id: str, placeholder_only: bool = False
) -> Tuple[str, List[Any]]:
    """Generate query to get a credential by ID. ``placeholder_only`` reads
    only a row with no `provider` — a {name} placeholder / hook credential,
    never a provider ACCOUNT (an LLM / STT / TTS key a template names by
    credential_id): the pre-check context asks with it, so an account's key
    can never be pulled into a hook's variables."""
    where = ' AND "provider" IS NULL' if placeholder_only else ""
    text = f'SELECT * FROM "{CREDENTIALS_TABLE}" WHERE "id" = $1{where};'
    return text, [credential_id]


# A template names a provider account by credential_id inside its
# configurations jsonb — a reference no FK can see. This is the in-use guard,
# exact (jsonb_path_exists, never LIKE over text: an uppercase or hyphen-less
# spelling, or the id inside prompt text, must not fool it) and ATOMIC — the
# same statement that deletes or strands the row asks it, so no window exists
# between a check and the write. $1::text is the uuid's one canonical spelling.
_NAMED_BY_A_TEMPLATE = f"""
    EXISTS (
        SELECT 1 FROM "{TEMPLATE_TABLE}"
        WHERE "configurations" IS NOT NULL
          AND jsonb_path_exists(
                "configurations",
                '$.** ? (@.credential_id == $id)',
                jsonb_build_object('id', $1::text)
              )
    )
"""


def get_credentials_by_merchant_query(
    reseller_id: Optional[str],
    merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get the credentials a tenant can see.

    reseller given            -> global + that reseller's reseller-wide rows
    reseller + merchant given -> the above + that merchant's rows
    neither                   -> only global rows

    Ordered least-specific first (global, reseller, merchant) so a caller
    that merges by name ends up with the most specific row winning.
    """
    if reseller_id and merchant_id:
        text = f"""
            SELECT * FROM "{CREDENTIALS_TABLE}"
            WHERE ("reseller_id" IS NULL
                   OR ("reseller_id" = $1
                       AND ("merchant_id" IS NULL OR "merchant_id" = $2)))
            AND "is_active" = TRUE
            AND "provider" IS NULL
            ORDER BY "reseller_id" NULLS FIRST, "merchant_id" NULLS FIRST, "name" ASC;
        """
        return text, [reseller_id, merchant_id]
    if reseller_id:
        text = f"""
            SELECT * FROM "{CREDENTIALS_TABLE}"
            WHERE ("reseller_id" = $1 OR "reseller_id" IS NULL)
            AND "merchant_id" IS NULL
            AND "is_active" = TRUE
            AND "provider" IS NULL
            ORDER BY "reseller_id" NULLS FIRST, "name" ASC;
        """
        return text, [reseller_id]
    else:
        text = f"""
            SELECT * FROM "{CREDENTIALS_TABLE}"
            WHERE "reseller_id" IS NULL AND "is_active" = TRUE
            AND "provider" IS NULL
            ORDER BY "name" ASC;
        """
        return text, []


def get_credential_by_name_query(
    reseller_id: Optional[str], name: str
) -> Tuple[str, List[Any]]:
    """Generate query to get one credential by its name.

    One row, decrypted once — the alternative is reading every credential a
    reseller owns and scanning for the name, which costs a KMS decrypt per
    credential and grows with the reseller.

    A reseller's own row WINS over the global fallback: NULLS LAST, not the
    NULLS FIRST the list query uses. That query is a listing where globals
    sensibly come first; this one picks a single winner, and an override that
    loses to the thing it overrides is not an override.
    """
    if reseller_id:
        text = f"""
            SELECT * FROM "{CREDENTIALS_TABLE}"
            WHERE ("reseller_id" = $1 OR "reseller_id" IS NULL)
            AND "name" = $2 AND "is_active" = TRUE
            ORDER BY "reseller_id" NULLS LAST
            LIMIT 1;
        """
        return text, [reseller_id, name]
    else:
        text = f"""
            SELECT * FROM "{CREDENTIALS_TABLE}"
            WHERE "reseller_id" IS NULL AND "name" = $1 AND "is_active" = TRUE
            LIMIT 1;
        """
        return text, [name]


def get_all_credentials_query() -> Tuple[str, List[Any]]:
    """Generate query to get all credentials."""
    text = f'SELECT * FROM "{CREDENTIALS_TABLE}" where "is_active" = TRUE ORDER BY "reseller_id" NULLS FIRST, "merchant_id" NULLS FIRST, "name" ASC;'
    return text, []


def update_credential_query(
    credential_id: str,
    name: Optional[str] = None,
    credential_type: Optional[str] = None,
    value: Optional[str] = None,
    is_encrypted: Optional[bool] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
    provider: Optional[str] = None,
    unless_named_by_a_template: bool = False,
) -> Tuple[str, List[Any]]:
    """Generate query to update a credential. Only updates provided fields.

    ``unless_named_by_a_template``: the edit would strand every template
    that names this row as its provider account (switching it off,
    re-labelling its provider), so the statement itself refuses while one
    does — 0 rows back, which the accessor reports as "in use"."""
    updates = []
    values: List[Any] = []
    param_count = 1

    if provider is not None:
        updates.append(f'"provider" = ${param_count}')
        values.append(provider)
        param_count += 1

    if name is not None:
        updates.append(f'"name" = ${param_count}')
        values.append(name)
        param_count += 1

    if credential_type is not None:
        updates.append(f'"credential_type" = ${param_count}')
        values.append(credential_type)
        param_count += 1

    if value is not None:
        updates.append(f'"value" = ${param_count}')
        values.append(value)
        param_count += 1

    if is_encrypted is not None:
        updates.append(f'"is_encrypted" = ${param_count}')
        values.append(is_encrypted)
        param_count += 1

    if description is not None:
        updates.append(f'"description" = ${param_count}')
        values.append(description)
        param_count += 1

    if is_active is not None:
        updates.append(f'"is_active" = ${param_count}')
        values.append(is_active)
        param_count += 1

    # Always update updated_at
    updates.append(f'"updated_at" = ${param_count}')
    values.append(datetime.now())
    param_count += 1

    values.append(credential_id)
    guard = ""
    if unless_named_by_a_template:
        guard = f" AND NOT {_NAMED_BY_A_TEMPLATE.replace('$1', f'${param_count}')}"

    text = f"""
        UPDATE "{CREDENTIALS_TABLE}"
        SET {', '.join(updates)}
        WHERE "id" = ${param_count}{guard}
        RETURNING *;
    """

    return text, values


def delete_credential_query(credential_id: str) -> Tuple[str, List[Any]]:
    """Delete a credential by ID — unless a template names it as its provider
    account, in the same statement (see _NAMED_BY_A_TEMPLATE). 0 rows back
    means either no such row or a row in use; the accessor tells them apart."""
    text = f"""
        DELETE FROM "{CREDENTIALS_TABLE}"
        WHERE "id" = $1 AND NOT {_NAMED_BY_A_TEMPLATE}
        RETURNING *;
    """
    return text, [credential_id]
