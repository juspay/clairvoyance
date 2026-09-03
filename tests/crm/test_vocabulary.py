"""The status vocabularies have ONE home, and SQL never spells them itself.

A grep cannot pin this: the builders' DOCSTRINGS quote the same words on
purpose ("Only ever touches a row that is still 'draft'"), and that prose is
worth keeping. So this walks the AST and looks only inside the SQL string a
builder actually returns — docstrings are out of scope by construction.

Why it matters is not tidiness. A status spelled in SQL text is a value
interpolated into a statement, which CLAUDE.md calls a blocker, and it is a
second definition of a word whose first definition lives in status.py. The
day the two disagree, a filter silently returns the wrong half of its rows —
the scar ProviderTemplateState's docstring records, and the one accounts.py
opens with.
"""

import ast
from pathlib import Path

from app.crm.connectivity import accounts, status

QUERIES_DIR = Path(__file__).resolve().parents[2] / "app/crm/connectivity/db/queries"

#: Every word of all four vocabularies, as SQL would spell it.
STATUS_WORDS = sorted(
    v
    for k, v in vars(status).items()
    if not k.startswith("_") and isinstance(v, str) and k.isupper()
)

#: The tables this module writes a status on — a FIXED list, so a fifth
#: table whose words never reach status.py cannot hide from this test the
#: way crm_message's did (its words lived in dispatch.py and its SQL spelled
#: 'sending' while every check here passed — the 3 Sep 2026 audit).
STATUS_FAMILIES = ("TEMPLATE_", "INSTALLATION_", "BINDING_", "MESSAGE_")


#: Names in status.py, so an interpolated constant is recognised by NAME as
#: well as by value — f"... status = '{TEMPLATE_DRAFT}'" puts the word in the
#: statement just as surely as typing it, and leaves no literal to find.
STATUS_NAMES = frozenset(
    k for k, v in vars(status).items() if k.isupper() and not k.startswith("_")
)


def _interpolated_names(node: ast.expr) -> list[str]:
    """Every name an f-string interpolation could be reaching for."""
    names = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.append(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.append(sub.attr)
    return names


def _sql_strings(tree: ast.AST) -> list[tuple[str, str, list[str]]]:
    """Every ``query = ...`` string, as (function, literal sql, interpolated
    names). The third element is what keeps a constant from sneaking into the
    statement through a ``{}`` where no literal would ever appear."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "query" for t in node.targets
            ):
                continue
            value = node.value
            interpolated: list[str] = []
            if isinstance(value, ast.JoinedStr):
                sql = "".join(
                    p.value
                    for p in value.values
                    if isinstance(p, ast.Constant) and isinstance(p.value, str)
                )
                # {TABLE} interpolations are IDENTIFIERS and legal; a status
                # constant here would be a VALUE, which is the blocker.
                for part in value.values:
                    if isinstance(part, ast.FormattedValue):
                        interpolated += _interpolated_names(part.value)
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                sql = value.value
            else:
                continue
            out.append((fn.name, sql, interpolated))
    return out


def test_no_status_word_is_spelled_inside_a_sql_string() -> None:
    """Statuses reach the database as $n parameters, never as SQL text."""
    assert STATUS_WORDS, "status.py exports no words — the test would pass vacuously"
    offences = []
    for path in sorted(QUERIES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn_name, sql, interpolated in _sql_strings(tree):
            for word in STATUS_WORDS:
                if f"'{word}'" in sql:
                    offences.append(f"{path.name}:{fn_name} spells '{word}' in SQL")
            for name in interpolated:
                if name in STATUS_NAMES:
                    offences.append(
                        f"{path.name}:{fn_name} interpolates {name} into SQL — "
                        f"a status is a VALUE and binds as $n, never as f-string text"
                    )
    assert not offences, "\n".join(offences)


def test_every_table_family_has_its_words_in_status_py() -> None:
    """Each family must export at least its column default; a table whose
    vocabulary lives elsewhere is invisible to the SQL walk above."""
    for family in STATUS_FAMILIES:
        words = [k for k in vars(status) if k.startswith(family) and k.isupper()]
        assert words, f"{family} words are not in status.py"
    # the manifest's ladder, as canon T16 col 12 spells it
    assert status.MESSAGE_QUEUED == "queued" and status.MESSAGE_SENDING == "sending"
    assert status.MESSAGE_DEAD == "dead"


def test_no_second_home_for_the_manifest_words() -> None:
    """dispatch.py once defined STATUS_QUEUED… beside status.py's words."""
    from app.crm.connectivity import dispatch

    assert not [n for n in vars(dispatch) if n.startswith("STATUS_")]


def test_the_builders_were_actually_read() -> None:
    """Guard on the guard: a glob that stops matching must fail, not pass."""
    found = [
        fn
        for p in QUERIES_DIR.glob("*.py")
        for fn, _, _ in _sql_strings(ast.parse(p.read_text()))
    ]
    assert len(found) >= 20, f"only {len(found)} builders parsed — the walk broke"


def test_usable_installation_states_has_one_definition() -> None:
    """accounts.py keeps the NAME, status.py keeps the words."""
    root = Path(__file__).resolve().parents[2] / "app/crm"
    definitions = [
        f"{p}:{n.lineno}"
        for p in root.rglob("*.py")
        for n in ast.walk(ast.parse(p.read_text()))
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "USABLE_INSTALLATION_STATES"
            for t in n.targets
        )
        and not isinstance(n.value, ast.Name)  # an alias is not a definition
    ]
    assert definitions == [], f"second definition of the policy: {definitions}"
    assert accounts.USABLE_INSTALLATION_STATES is status.INSTALLATION_USABLE


def test_the_vocabularies_match_the_shipped_ddl() -> None:
    """The words are the column defaults the migrations actually wrote."""
    assert status.TEMPLATE_DRAFT == "draft"  # 061 default
    assert status.INSTALLATION_CONNECTING == "connecting"  # 060 default
    assert status.BINDING_ACTIVE == "active"  # 060 default
    # retiring a template writes 'deleted'; 'retired' belongs to a binding
    assert status.TEMPLATE_DELETED == "deleted"
    assert status.BINDING_RETIRED == "retired"
    assert status.TEMPLATE_DELETED != status.BINDING_RETIRED
