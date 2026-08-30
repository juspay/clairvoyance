"""Query-builder laws for the connectivity module: $1 params only, and the
binding lifecycle rules that live in SQL (disconnect clearing is_primary,
re-onboard reactivating status)."""

from app.crm.connectivity.db import queries
from app.crm.connectivity.db.queries import (
    pause_bindings_for_installation_query,
    upsert_channel_binding_query,
)


def test_disconnect_pause_also_clears_is_primary() -> None:
    """Without this, crm_channel_binding_primary_uq (merchant_id, channel
    WHERE is_primary) blocks connecting a new number until someone manually
    clears the flag on the paused row."""
    sql, params = pause_bindings_for_installation_query("m1", "inst-1")
    assert "status = 'paused'" in sql
    assert "is_primary = false" in sql
    assert params == ["m1", "inst-1"]


def test_reonboard_reactivates_status_to_active() -> None:
    """A re-onboard of the same number must reactivate a paused binding,
    not just leave it paused with a fresh installation_id."""
    sql, params = upsert_channel_binding_query(
        "m1", "whatsapp", "inst-1", "+15550001111", True
    )
    assert "status = 'active'" in sql
    assert params == ["m1", "whatsapp", "inst-1", "+15550001111", True]


def test_read_shape_queries_select_every_column_their_decoder_reads() -> None:
    """The module carries two query/decoder families: the send path reads
    INSTALLATION_COLUMNS/BINDING_COLUMNS (no timestamps, no health_detail),
    the console reads SELECT *. Pointing a console query at the narrow
    column list is a KeyError on the first real call and NOTHING in the
    suite would catch it — every accessor is monkeypatched above the row."""
    installation_reads = [
        queries.upsert_installation_query(
            "m1", "whatsapp", "waba-1", None, "cred-1", "healthy", "{}"
        ),
        queries.get_installation_query("m1", "inst-1"),
        queries.list_installations_query("m1"),
        queries.disconnect_installation_query("m1", "inst-1"),
    ]
    for sql, _ in installation_reads:
        assert "*" in sql, sql
        assert "INSTALLATION_COLUMNS" not in sql

    binding_reads = [
        queries.get_channel_binding_by_address_query("m1", "whatsapp", "+1555"),
        queries.upsert_channel_binding_query("m1", "whatsapp", "inst-1", "+1555", True),
    ]
    for sql, _ in binding_reads:
        assert "*" in sql, sql


def test_send_path_queries_stay_on_the_narrow_column_lists() -> None:
    """The converse: the send path must not start dragging health_detail and
    five timestamps through the dispatcher's hot loop."""
    sql, _ = queries.installation_by_id_query("m1", "inst-1")
    assert queries.INSTALLATION_COLUMNS.strip() in sql
    sql, _ = queries.primary_binding_query("m1", "whatsapp")
    assert queries.BINDING_COLUMNS.strip() in sql
