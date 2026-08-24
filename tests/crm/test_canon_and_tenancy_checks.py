"""Tests for the A3 gates: canon-conformance diff + tenancy CI.

The point of these is that the checks can go RED. A gate that only ever
passes is indistinguishable from no gate, so every rule has a test that
feeds it a violation and asserts it is caught — plus the regression that
made the query check fire on prose.

Both checkers keep their rule logic pure (``diff``, ``evaluate_schema_laws``,
``check_query_predicates``), so none of this needs a database.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str):
    """Import a scripts/*.py checker as a module (not on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_canon = _load("check_canon")
check_tenancy = _load("check_tenancy")

TS = "timestamp with time zone"


def _live_customer(**overrides):
    """A conforming crm_customer shape, per the sealed schema."""
    from app.crm.shared.sealed import CRM_CUSTOMER

    live = {c.name: (c.type, c.not_null) for c in CRM_CUSTOMER.columns}
    live.update(overrides)
    return {"crm_customer": live}


# ---------------------------------------------------------------------------
#  Canon conformance — MISSING is pending, EXTRA/MISMATCHED fail
# ---------------------------------------------------------------------------


def test_missing_table_is_pending_not_a_failure():
    """The whole point: the gate runs before the tables exist."""
    failures, pending = check_canon.diff({})
    assert failures == []
    assert any("MISSING table crm_customer" in p for p in pending)
    assert any("MISSING table platform_identity" in p for p in pending)


def test_conforming_schema_passes():
    """A database matching every sealed table exactly is clean.

    Derived from SEALED rather than a hand-listed pair, so sealing a new
    canon table doesn't break this test.
    """
    from app.crm.shared.sealed import SEALED

    live = {
        name: {c.name: (c.type, c.not_null) for c in table.columns}
        for name, table in SEALED.items()
    }
    failures, pending = check_canon.diff(live)
    assert failures == []
    assert pending == []


def test_missing_column_is_pending():
    live = _live_customer()
    del live["crm_customer"]["timezone"]
    failures, pending = check_canon.diff(live)
    assert failures == []
    assert any("MISSING column crm_customer.timezone" in p for p in pending)


def test_extra_column_fails():
    live = _live_customer(sneaky_column=("text", False))
    failures, _ = check_canon.diff(live)
    assert any("EXTRA column crm_customer.sneaky_column" in f for f in failures)


def test_extra_governed_table_fails():
    live = _live_customer()
    live["crm_undeclared"] = {"id": ("uuid", True)}
    failures, _ = check_canon.diff(live)
    assert any("EXTRA table crm_undeclared" in f for f in failures)


def test_mismatched_type_fails():
    live = _live_customer(merchant_id=("integer", True))
    failures, _ = check_canon.diff(live)
    assert any(
        "MISMATCHED crm_customer.merchant_id" in f and "integer" in f for f in failures
    )


def test_mismatched_nullability_fails():
    """Dropping NOT NULL from merchant_id must not slip through."""
    live = _live_customer(merchant_id=("text", False))
    failures, _ = check_canon.diff(live)
    assert any(
        "MISMATCHED crm_customer.merchant_id" in f and "not_null" in f for f in failures
    )


def test_ungoverned_tables_are_ignored():
    """The ~45 pre-CPaaS tables must never register as EXTRA."""
    from app.crm.shared.sealed import is_governed

    assert not is_governed("lead_call_tracker")
    assert not is_governed("template")
    assert is_governed("crm_customer")
    assert is_governed("platform_identity")


def test_naming_divergence_is_reported_but_not_fatal():
    """crm_customer vs canon's crm.customer stays visible, never fails."""
    from app.crm.shared.sealed import divergences

    reported = dict(divergences())
    assert reported["crm_customer"] == "crm.customer"
    assert reported["platform_identity"] == "platform.identity"


# ---------------------------------------------------------------------------
#  Tenancy laws
# ---------------------------------------------------------------------------


def test_law1_reseller_column_on_crm_table_fails():
    violations = check_tenancy.evaluate_schema_laws(
        {"crm_customer": {"merchant_id": True, "reseller_id": True}}, {}
    )
    assert any("law 1" in v and "reseller_id" in v for v in violations)


def test_law2_tenant_scoped_table_without_merchant_id_fails():
    violations = check_tenancy.evaluate_schema_laws({"crm_customer": {"id": True}}, {})
    assert any("law 2" in v for v in violations)


def test_law2_nullable_merchant_id_fails():
    violations = check_tenancy.evaluate_schema_laws(
        {"crm_customer": {"merchant_id": False}}, {}
    )
    assert any("law 2" in v for v in violations)


def test_law2_exempts_platform_tables():
    """platform_identity is cross-merchant by design, not in violation."""
    violations = check_tenancy.evaluate_schema_laws(
        {"platform_identity": {"kind": True, "value": True}}, {}
    )
    assert violations == []


def test_law3_unique_index_not_leading_with_merchant_id_fails():
    violations = check_tenancy.evaluate_schema_laws(
        {"crm_customer": {"merchant_id": True}},
        {("crm_customer", "crm_customer_phone_uq"): ["phone", "merchant_id"]},
    )
    assert any("law 3" in v and "crm_customer_phone_uq" in v for v in violations)


def test_law3_merchant_first_unique_index_passes():
    violations = check_tenancy.evaluate_schema_laws(
        {"crm_customer": {"merchant_id": True}},
        {("crm_customer", "ok_uq"): ["merchant_id", "phone"]},
    )
    assert violations == []


def test_law3_primary_key_is_exempt():
    violations = check_tenancy.evaluate_schema_laws(
        {"crm_customer": {"merchant_id": True}},
        {("crm_customer", "crm_customer_pkey"): ["id"]},
    )
    assert violations == []


# --- law 3, query half ------------------------------------------------------


def _write(tmp_path: Path, source: str) -> Path:
    (tmp_path / "queries.py").write_text(source, encoding="utf-8")
    return tmp_path


def test_query_without_merchant_predicate_is_caught(tmp_path):
    package = _write(
        tmp_path,
        'QUERY = """\n    SELECT id FROM crm_customer WHERE phone = $1\n"""\n',
    )
    violations = check_tenancy.check_query_predicates(package)
    assert any("law 3" in v for v in violations)


def test_query_with_merchant_predicate_passes(tmp_path):
    package = _write(
        tmp_path,
        'QUERY = """\n    SELECT id FROM crm_customer\n'
        '    WHERE merchant_id = $1 AND phone = $2\n"""\n',
    )
    assert check_tenancy.check_query_predicates(package) == []


def test_docstring_mentioning_a_root_table_is_not_a_violation(tmp_path):
    """Regression: the first cut flagged resolve.py's module docstring.

    It reads 'no other INSERT INTO crm_customer may exist anywhere' — names
    a root table, carries no predicate, and is prose, not SQL.
    """
    package = _write(
        tmp_path,
        '"""No other INSERT INTO crm_customer may exist anywhere."""\n',
    )
    assert check_tenancy.check_query_predicates(package) == []


def test_insert_is_not_treated_as_a_read(tmp_path):
    """INSERT carries merchant_id as a column, not a predicate."""
    package = _write(
        tmp_path,
        'QUERY = """\n    INSERT INTO crm_customer (merchant_id) VALUES ($1)\n"""\n',
    )
    assert check_tenancy.check_query_predicates(package) == []


def test_update_on_root_table_without_predicate_is_caught(tmp_path):
    package = _write(
        tmp_path,
        'QUERY = """\n    UPDATE crm_customer SET display_name = $1 WHERE id = $2\n"""\n',
    )
    assert any("law 3" in v for v in check_tenancy.check_query_predicates(package))


# ---------------------------------------------------------------------------
#  The real repository must satisfy its own gates
# ---------------------------------------------------------------------------


def test_shipped_crm_package_obeys_the_query_law():
    assert check_tenancy.check_query_predicates() == []


def test_sealed_schema_declares_every_column_once():
    from app.crm.shared.sealed import SEALED

    for name, table in SEALED.items():
        names = [c.name for c in table.columns]
        assert len(names) == len(set(names)), f"{name} declares a column twice"


@pytest.mark.parametrize("canon_id", ["T02", "T05"])
def test_sealed_tables_are_findable_by_canon_id(canon_id):
    from app.crm.shared.sealed import find_by_canon_id

    assert find_by_canon_id(canon_id) is not None


def test_predicate_inside_a_line_comment_does_not_satisfy_law3(tmp_path):
    """Regression: a trailing `-- merchant_id = $2` bypassed the check.

    A predicate in a comment is not a predicate.
    """
    package = _write(
        tmp_path,
        'Q = """\n    SELECT id FROM crm_customer WHERE phone = $1'
        '  -- merchant_id = $2\n"""\n',
    )
    assert any("law 3" in v for v in check_tenancy.check_query_predicates(package))


def test_predicate_inside_a_block_comment_does_not_satisfy_law3(tmp_path):
    package = _write(
        tmp_path,
        'Q = """\n    SELECT id FROM crm_customer /* merchant_id = $2 */\n'
        '    WHERE phone = $1\n"""\n',
    )
    assert any("law 3" in v for v in check_tenancy.check_query_predicates(package))


def test_commented_out_table_reference_is_not_a_read(tmp_path):
    """Stripping comments must also stop a commented table from counting."""
    package = _write(
        tmp_path,
        'Q = """\n    SELECT 1 -- FROM crm_customer\n"""\n',
    )
    assert check_tenancy.check_query_predicates(package) == []


def test_real_predicate_still_passes_with_a_comment_present(tmp_path):
    package = _write(
        tmp_path,
        'Q = """\n    -- scoped read\n    SELECT id FROM crm_customer\n'
        '    WHERE merchant_id = $1\n"""\n',
    )
    assert check_tenancy.check_query_predicates(package) == []


def test_fstring_query_on_a_root_table_is_caught(tmp_path):
    """THE regression: every real builder writes FROM {CRM_CUSTOMER_TABLE}.

    Matching only ast.Constant meant the literal 'crm_customer' never
    appeared in any scanned string, so the checker examined zero real
    queries and reported clean — a metal detector everyone walks around.
    """
    package = _write(
        tmp_path,
        'CRM_CUSTOMER_TABLE = "crm_customer"\n\n'
        "def q():\n"
        '    return f"""\n        SELECT id\n'
        '        FROM {CRM_CUSTOMER_TABLE}\n        WHERE phone = $1\n    """\n',
    )
    violations = check_tenancy.check_query_predicates(package)
    assert any("law 3" in v for v in violations), "f-string query was not examined"


def test_fstring_query_with_merchant_predicate_passes(tmp_path):
    package = _write(
        tmp_path,
        'CRM_CUSTOMER_TABLE = "crm_customer"\n\n'
        "def q():\n"
        '    return f"""\n        SELECT id FROM {CRM_CUSTOMER_TABLE}\n'
        '        WHERE merchant_id = $1\n    """\n',
    )
    assert check_tenancy.check_query_predicates(package) == []


def test_predicate_held_in_a_local_variable_is_resolved(tmp_path):
    """list_customers_query builds `WHERE {where}` from a local literal."""
    package = _write(
        tmp_path,
        'CRM_CUSTOMER_TABLE = "crm_customer"\n\n'
        "def q():\n"
        "    where = \"merchant_id = $1 AND status = 'active'\"\n"
        '    return f"""\n        SELECT id FROM {CRM_CUSTOMER_TABLE}\n'
        '        WHERE {where}\n    """\n',
    )
    assert check_tenancy.check_query_predicates(package) == []


def test_unresolvable_interpolation_does_not_forge_a_predicate(tmp_path):
    """An unknown {var} must not read as a merchant_id predicate."""
    package = _write(
        tmp_path,
        'CRM_CUSTOMER_TABLE = "crm_customer"\n\n'
        "def q(clause):\n"
        '    return f"""\n        SELECT id FROM {CRM_CUSTOMER_TABLE}\n'
        '        WHERE {clause}\n    """\n',
    )
    assert any("law 3" in v for v in check_tenancy.check_query_predicates(package))


def test_canon_id_set_is_complete_and_has_no_phantoms():
    """17 tables + 1 view = 18; T01/T03/T04/T06/T10 do not exist."""
    from app.crm.shared.sealed import (
        CANON_TABLE_IDS,
        CANON_VIEW_IDS,
        PENDING_CANON_IDS,
        SEALED,
    )

    assert len(CANON_TABLE_IDS) == 17
    assert len(CANON_VIEW_IDS) == 1
    assert len(SEALED) + len(PENDING_CANON_IDS) == 18
    for phantom in ("T01", "T03", "T04", "T06", "T10"):
        assert phantom not in CANON_TABLE_IDS
