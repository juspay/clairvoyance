"""Credential visibility: global < reseller < merchant, scoped only when asked."""

from app.database.queries.breeze_buddy.credentials import (
    get_all_credentials_query,
    get_credentials_by_merchant_query,
    insert_credential_query,
)


def test_query_scopes_merchant_rows_only_when_asked() -> None:
    sql, values = get_credentials_by_merchant_query("breeze", "chennai_one")
    assert values == ["breeze", "chennai_one"] and '"merchant_id" = $2' in sql
    assert '"merchant_id" NULLS FIRST' in sql  # least specific first

    sql, values = get_credentials_by_merchant_query("breeze")
    assert values == ["breeze"] and '"merchant_id" IS NULL' in sql

    sql, values = get_credentials_by_merchant_query(None)
    assert values == [] and '"reseller_id" IS NULL' in sql


def test_all_credentials_orders_most_specific_last() -> None:
    sql, _ = get_all_credentials_query()
    assert '"reseller_id" NULLS FIRST, "merchant_id" NULLS FIRST' in sql


def test_insert_carries_merchant_id_in_position() -> None:
    sql, values = insert_credential_query(
        id="cred_1",
        reseller_id="breeze",
        name="uap",
        credential_type="custom",
        value="{}",
        is_encrypted=False,
        description=None,
        merchant_id="chennai_one",
    )
    assert '"reseller_id", "merchant_id", "name"' in sql
    assert values[1:4] == ["breeze", "chennai_one", "uap"]
