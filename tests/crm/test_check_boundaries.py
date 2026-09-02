"""The CI boundary guard: each violation class must actually fire."""

from pathlib import Path
from typing import Dict

from scripts.check_crm_boundaries import check


def _tree(tmp_path: Path, files: Dict[str, str]) -> Path:
    """Materialise the given files under a temp root."""
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def test_clean_tree_passes(tmp_path: Path) -> None:
    """Clean tree passes."""
    root = _tree(
        tmp_path,
        {
            "app/crm/identity/db/queries.py": 'T = "crm_customer"\nq = "SELECT id FROM crm_customer"',
            "app/crm/identity/resolve.py": "from app.crm.platform.contracts import x\n",
        },
    )
    assert check(root) == []


def test_table_literal_outside_owner_fails(tmp_path: Path) -> None:
    """Table literal outside owner fails."""
    root = _tree(
        tmp_path,
        {"app/crm/record/ingest.py": 'q = "crm_customer"'},
    )
    assert any("outside its owner" in e for e in check(root))


def test_buddy_touching_crm_table_fails(tmp_path: Path) -> None:
    """Buddy touching crm table fails."""
    root = _tree(
        tmp_path,
        {"app/ai/voice/thing.py": 'q = "platform_identity"'},
    )
    assert any("outside its owner" in e for e in check(root))


def test_sql_outside_queries_fails(tmp_path: Path) -> None:
    """Sql outside queries fails."""
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": 'q = "SELECT x FROM t WHERE y=$1"'},
    )
    assert any("SQL statement outside db/queries" in e for e in check(root))


def test_asyncpg_in_logic_fails(tmp_path: Path) -> None:
    """Asyncpg in logic fails."""
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "import asyncpg\n"},
    )
    assert any("asyncpg import outside" in e for e in check(root))


def test_crm_importing_ai_fails(tmp_path: Path) -> None:
    """Crm importing ai fails."""
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "from app.ai.voice import x\n"},
    )
    assert any("must never import app.ai" in e for e in check(root))


def test_cross_module_bypass_fails(tmp_path: Path) -> None:
    """Cross module bypass fails."""
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "from app.crm.platform.suppression import x\n"},
    )
    assert any("bypasses contracts.py" in e for e in check(root))


def test_buddy_deep_import_fails(tmp_path: Path) -> None:
    """Buddy deep import fails."""
    root = _tree(
        tmp_path,
        {"app/ai/mirror.py": "from app.crm.identity.resolve import resolve\n"},
    )
    assert any("only app.crm.<module>.contracts" in e for e in check(root))


def test_data_layer_importing_crm_fails(tmp_path: Path) -> None:
    """Data layer importing crm fails."""
    root = _tree(
        tmp_path,
        {"app/database/accessor/foo.py": "from app.crm.identity.contracts import x\n"},
    )
    assert any("data layer imports neither" in e for e in check(root))


def test_handle_call_in_logic_fails(tmp_path: Path) -> None:
    """Handle call in logic fails."""
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
    """Unowned table in migration fails."""
    root = _tree(
        tmp_path,
        {"app/database/migrations/099_x.sql": "CREATE TABLE crm_mystery (id int);"},
    )
    assert any("has no owner" in e for e in check(root))


def test_raw_transaction_in_logic_fails(tmp_path: Path) -> None:
    """Raw transaction in logic fails."""
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "async with transaction() as txn:\n    pass\n"},
    )
    assert any("raw transaction outside shared/db" in e for e in check(root))


def test_atomically_callee_must_be_in_txn(tmp_path: Path) -> None:
    """Atomically callee must be in txn."""
    root = _tree(
        tmp_path,
        {"app/crm/identity/resolve.py": "x = await atomically(do_stuff, 1)\n"},
    )
    assert any("must be named *_in_txn" in e for e in check(root))


def test_in_txn_body_needs_atomic_docstring(tmp_path: Path) -> None:
    """In txn body needs atomic docstring."""
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
    """Connection handle in logic fails."""
    root = _tree(
        tmp_path,
        {
            "app/crm/platform/suppression.py": "async with connection() as conn:\n    pass\n"
        },
    )
    assert any("connection handle in logic" in e for e in check(root))


def test_adapter_import_outside_the_doors_fails(tmp_path: Path) -> None:
    # Any file that is not a named door, however innocent it looks: dispatch
    # reaching an adapter directly would send around send()'s route
    # resolution and suppression gate.
    """Adapter import outside the doors fails."""
    root = _tree(
        tmp_path,
        {
            "app/crm/connectivity/dispatch.py": (
                "from app.crm.connectivity.providers import adapter_for\n"
            )
        },
    )
    assert any("provider face imported outside its door" in e for e in check(root))


def test_a_new_root_file_is_not_a_door_by_being_next_to_them(tmp_path: Path) -> None:
    # The map is closed, not "anything at the module root". A file added
    # beside the doors has to be registered in PROVIDER_ROOTS/PROVIDER_FACES
    # deliberately, which is the moment somebody has to name its direction.
    """A new root file is not a door by being next to them."""
    root = _tree(
        tmp_path,
        {
            "app/crm/connectivity/refresh.py": (
                "from app.crm.connectivity.providers.whatsapp.onboard import x\n"
            )
        },
    )
    assert any("provider face imported outside its door" in e for e in check(root))


def test_each_direction_has_its_door(tmp_path: Path) -> None:
    # One root per face of provider traffic — send, onboard/administer,
    # receive — plus providers/ importing itself.
    """Each direction has its door."""
    root = _tree(
        tmp_path,
        {
            "app/crm/connectivity/send.py": (
                "from app.crm.connectivity.providers import adapter_for\n"
                "from app.crm.connectivity.providers.base import ChannelAdapter\n"
                "from app.crm.connectivity.providers.whatsapp.adapter import A\n"
            ),
            "app/crm/connectivity/ingress.py": (
                "import app.crm.connectivity.providers.meta.inbound as meta\n"
            ),
            "app/crm/connectivity/connectors.py": (
                "from app.crm.connectivity.providers.base import Err\n"
                "from app.crm.connectivity.providers.whatsapp.onboard import W\n"
            ),
            "app/crm/connectivity/providers/whatsapp/adapter.py": (
                "from app.crm.connectivity.providers.base import ChannelAdapter\n"
                "from app.crm.connectivity.providers.meta.graph import call\n"
            ),
        },
    )
    assert check(root) == []


def test_the_inbound_face_outside_ingress_fails(tmp_path: Path) -> None:
    """The inbound face answers to ingress.py alone: a route or worker that
    read it directly would receive letters without the owner resolution in
    front of them."""
    root = _tree(
        tmp_path,
        {
            "app/crm/record/api.py": (
                "import app.crm.connectivity.providers.meta.inbound as meta\n"
            )
        },
    )
    assert any("provider face imported outside its door" in e for e in check(root))


def test_record_importing_a_subscriber_fails(tmp_path: Path) -> None:
    # Rule 12: not even the subscriber's contracts — worker_main registers
    # consumers through record/consumers.py; record never reaches back.
    """Record importing a subscriber fails."""
    root = _tree(
        tmp_path,
        {"app/crm/record/workers.py": "from app.crm.outreach.contracts import x\n"},
    )
    assert any("record imports a subscriber" in e for e in check(root))


def test_record_may_import_identity_and_shared(tmp_path: Path) -> None:
    """Record may import identity and shared."""
    root = _tree(
        tmp_path,
        {
            "app/crm/record/workers.py": (
                "from app.crm.identity.contracts import resolve\n"
                "from app.crm.shared.db import crm_connection\n"
            )
        },
    )
    assert not any("record imports a subscriber" in e for e in check(root))


# ---- rule 11 is face-precise: each face has ONE root outside providers/ ----


def test_onboard_face_outside_connectors_fails(tmp_path: Path) -> None:
    """The scar this rule exists for, in reverse: onboarding.py must reach a
    provider's onboard face through connectors.py, never by importing it."""
    root = _tree(
        tmp_path,
        {
            "app/crm/connectivity/onboarding.py": (
                "from app.crm.connectivity.providers.whatsapp.onboard import W\n"
            )
        },
    )
    assert any("provider face imported outside its door" in e for e in check(root))


def test_adapter_face_in_connectors_fails(tmp_path: Path) -> None:
    """The other direction: connectors.py owns the non-send faces only. An
    adapter reached from there would bypass send()'s checks."""
    root = _tree(
        tmp_path,
        {
            "app/crm/connectivity/connectors.py": (
                "from app.crm.connectivity.providers.whatsapp.adapter import A\n"
            )
        },
    )
    assert any("provider face imported outside its door" in e for e in check(root))


def test_connectors_may_import_the_non_send_faces(tmp_path: Path) -> None:
    """Connectors may import the non send faces."""
    root = _tree(
        tmp_path,
        {
            "app/crm/connectivity/connectors.py": (
                "from app.crm.connectivity.providers.base import ConnectorOnboarder\n"
                "from app.crm.connectivity.providers.whatsapp.onboard import W\n"
                "from app.crm.connectivity.providers.whatsapp.templates import T\n"
            )
        },
    )
    assert check(root) == []


def test_vendor_transport_never_leaves_providers(tmp_path: Path) -> None:
    """meta/graph.py is the file the old rule pushed to the module root.
    Neither root may import it — it is transport, not a face."""
    root = _tree(
        tmp_path,
        {
            "app/crm/connectivity/connectors.py": (
                "from app.crm.connectivity.providers.meta.graph import call\n"
            )
        },
    )
    assert any("providers/ itself" in e for e in check(root))


# ---- rule 2 admits the per-table split of a module's db/ ------------------


def test_sql_in_split_queries_folder_passes(tmp_path: Path) -> None:
    """Sql in split queries folder passes."""
    root = _tree(
        tmp_path,
        {
            "app/crm/connectivity/db/queries/template.py": (
                'T = "crm_channel_template"\n'
                'q = "SELECT id FROM crm_channel_template"\n'
            )
        },
    )
    assert check(root) == []


def test_sql_in_split_accessors_folder_fails(tmp_path: Path) -> None:
    """The split moves the queries, not the confinement: an accessor under
    db/accessors/ is still forbidden to carry SQL."""
    root = _tree(
        tmp_path,
        {
            "app/crm/connectivity/db/accessors/template.py": (
                'q = "SELECT id FROM crm_channel_template"\n'
            )
        },
    )
    assert any("SQL statement outside db/queries" in e for e in check(root))
