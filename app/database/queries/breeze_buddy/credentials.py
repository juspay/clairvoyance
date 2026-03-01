"""
Database query functions for the credentials table.
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

CREDENTIALS_TABLE = "credentials"


def insert_credential_query(
    id: str,
    merchant_id: Optional[str],
    name: str,
    credential_type: str,
    value: str,
    is_encrypted: bool,
    description: Optional[str],
) -> Tuple[str, List[Any]]:
    """Generate query to insert a credential record."""
    text = f"""
        INSERT INTO "{CREDENTIALS_TABLE}"
        ("id", "merchant_id", "name", "credential_type", "value",
         "is_encrypted", "description", "is_active", "created_at", "updated_at")
        VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, $8, $9)
        RETURNING *;
    """
    values = [
        id,
        merchant_id,
        name,
        credential_type,
        value,
        is_encrypted,
        description,
        datetime.now(),
        datetime.now(),
    ]
    return text, values


def get_credential_by_id_query(credential_id: str) -> Tuple[str, List[Any]]:
    """Generate query to get a credential by ID."""
    text = f'SELECT * FROM "{CREDENTIALS_TABLE}" WHERE "id" = $1;'
    return text, [credential_id]


def get_credentials_by_merchant_query(
    merchant_id: Optional[str],
) -> Tuple[str, List[Any]]:
    """
    Generate query to get credentials for a merchant.
    If merchant_id is provided, returns merchant-specific + global credentials.
    If merchant_id is None, returns only global credentials.
    """
    if merchant_id:
        text = f"""
            SELECT * FROM "{CREDENTIALS_TABLE}"
            WHERE ("merchant_id" = $1 OR "merchant_id" IS NULL)
            AND "is_active" = TRUE
            ORDER BY "merchant_id" NULLS FIRST, "name" ASC;
        """
        return text, [merchant_id]
    else:
        text = f"""
            SELECT * FROM "{CREDENTIALS_TABLE}"
            WHERE "merchant_id" IS NULL AND "is_active" = TRUE
            ORDER BY "name" ASC;
        """
        return text, []


def get_all_credentials_query() -> Tuple[str, List[Any]]:
    """Generate query to get all credentials."""
    text = f'SELECT * FROM "{CREDENTIALS_TABLE}" where "is_active" = TRUE ORDER BY "merchant_id" NULLS FIRST, "name" ASC;'
    return text, []


def update_credential_query(
    credential_id: str,
    name: Optional[str] = None,
    credential_type: Optional[str] = None,
    value: Optional[str] = None,
    is_encrypted: Optional[bool] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to update a credential. Only updates provided fields."""
    updates = []
    values: List[Any] = []
    param_count = 1

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

    text = f"""
        UPDATE "{CREDENTIALS_TABLE}"
        SET {", ".join(updates)}
        WHERE "id" = ${param_count}
        RETURNING *;
    """

    return text, values


def delete_credential_query(credential_id: str) -> Tuple[str, List[Any]]:
    """Generate query to delete a credential by ID."""
    text = f'DELETE FROM "{CREDENTIALS_TABLE}" WHERE "id" = $1 RETURNING *;'
    return text, [credential_id]
