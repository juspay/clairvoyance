"""The CI boundary guard: each violation class must actually fire."""

from pathlib import Path
from typing import Dict

from scripts.check_crm_boundaries import check


def _tree(tmp_path: Path, files: Dict[str, str]) -> Path:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def test_clean_tree_passes(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "app/crm/identity/db/queries.py": 'T = "crm_customer"\nq = "SELECT id FROM crm_customer"',
            "app/crm/identity/resolve.py": "from app.crm.platform.contracts import x\n",
        },
    )
    assert check(root) == []


def test_table_literal_outside_owner_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/crm/record/ingest.py": 'q = "crm_customer"'},
    )
    assert any("outside its owner" in e for e in check(root))


def test_buddy_touching_crm_table_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/ai/voice/thing.py": 'q = "platform_identity"'},
    )
    assert any("outside its owner" in e for e in check(root))


def test_sql_outside_queries_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": 'q = "SELECT x FROM t WHERE y=$1"'},
    )
    assert any("SQL statement outside db/queries" in e for e in check(root))


def test_asyncpg_in_logic_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "import asyncpg\n"},
    )
    assert any("asyncpg import outside" in e for e in check(root))


def test_crm_importing_ai_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "from app.ai.voice import x\n"},
    )
    assert any("must never import app.ai" in e for e in check(root))


def test_cross_module_bypass_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "from app.crm.platform.suppression import x\n"},
    )
    assert any("bypasses contracts.py" in e for e in check(root))


def test_buddy_deep_import_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/ai/mirror.py": "from app.crm.identity.resolve import resolve\n"},
    )
    assert any("only app.crm.<module>.contracts" in e for e in check(root))


def test_data_layer_importing_crm_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/database/accessor/foo.py": "from app.crm.identity.contracts import x\n"},
    )
    assert any("data layer imports neither" in e for e in check(root))


def test_handle_call_in_logic_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "row = await txn.fetchrow(q)\n"},
    )
    assert any("driver method called" in e for e in check(root))


def test_nesting_via_driver_transaction_in_logic_fails(tmp_path: Path) -> None:
    """Nesting has its own door. A raw ``txn.transaction()`` emits the same
    SAVEPOINT as savepoint(txn) but reads as a second transaction — so a
    reader concludes the row commits on its own, which it does not."""
    root = _tree(
        tmp_path,
        {"app/crm/record/workers.py": "async with txn.transaction():\n    pass\n"},
    )
    assert any("driver method called" in e for e in check(root))


def test_unowned_table_in_migration_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/database/migrations/099_x.sql": "CREATE TABLE crm_mystery (id int);"},
    )
    assert any("has no owner" in e for e in check(root))


def test_raw_transaction_in_logic_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "async with transaction() as txn:\n    pass\n"},
    )
    assert any("raw transaction outside shared/db" in e for e in check(root))


def test_atomically_callee_must_be_in_txn(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "x = await atomically(do_stuff, 1)\n"},
    )
    assert any("must be named *_in_txn" in e for e in check(root))


def test_in_txn_body_needs_atomic_docstring(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "app/crm/identity/resolve.py": (
                "async def _x_in_txn(txn):\n" '    """does things"""\n' "    return 1\n"
            )
        },
    )
    assert any("lacks an 'ATOMIC:" in e for e in check(root))


def test_connection_handle_in_logic_fails(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "app/crm/platform/suppression.py": "async with connection() as conn:\n    pass\n"
        },
    )
    assert any("connection handle in logic" in e for e in check(root))
