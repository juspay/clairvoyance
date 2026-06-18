"""SQL query builders for customer_identity table."""

from typing import Any, List, Tuple

CUSTOMER_IDENTITY_TABLE = "customer_identity"


def upsert_alias_query(
    reseller_id: str,
    merchant_id: str,
    phone: str,
    customer_id: str,
) -> Tuple[str, List[Any]]:
    text = f"""
        INSERT INTO "{CUSTOMER_IDENTITY_TABLE}"
        (reseller_id, merchant_id, phone, customer_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (reseller_id, merchant_id, phone)
        DO UPDATE SET customer_id = EXCLUDED.customer_id, updated_at = now()
        RETURNING *;
    """
    values: List[Any] = [reseller_id, merchant_id, phone, customer_id]
    return text, values


def get_customer_id_for_phone_query(
    reseller_id: str,
    merchant_id: str,
    phone: str,
) -> Tuple[str, List[Any]]:
    text = f"""
        SELECT * FROM "{CUSTOMER_IDENTITY_TABLE}"
        WHERE reseller_id = $1 AND merchant_id = $2 AND phone = $3
        LIMIT 1;
    """
    values: List[Any] = [reseller_id, merchant_id, phone]
    return text, values
