"""Template custom python_code sandbox + kill switch (PT-01).

Proves the classic CPython sandbox-escape payload no longer compiles/executes,
that a legitimate handler still works, and that the feature is disabled by
default at the flow-build gate.
"""

from __future__ import annotations

import pytest

from app.ai.voice.agents.breeze_buddy.utils import parser
from app.ai.voice.agents.breeze_buddy.utils.parser import compile_custom_function

_ESCAPE = (
    "P=[c for c in ().__class__.__base__.__subclasses__() "
    "if c.__name__=='Popen'][0]\n"
    "P(['/bin/sh','-c','id'])\n"
    "def handler(args, context):\n    return {}"
)


def test_dunder_traversal_escape_is_blocked():
    # Compilation is rejected (RestrictedPython), so the module-level payload
    # never runs and no handler is returned.
    assert compile_custom_function("pwn", _ESCAPE) is None


def test_builtins_recovery_variants_blocked():
    for src in (
        "b = [].__class__.__base__.__subclasses__()\n"
        "def handler(a, c):\n    return {}",
        "x = {}.__class__\ndef handler(a, c):\n    return {}",
        "m = [].__class__.__mro__[1]\ndef handler(a, c):\n    return {}",
    ):
        assert compile_custom_function("x", src) is None


def test_legitimate_handler_compiles_and_runs():
    src = (
        "def handler(args, context):\n"
        "    total = sum([1, 2, 3])\n"
        "    label = str(args.get('name', '')).upper()\n"
        "    return {'total': total, 'label': label}"
    )
    handler = compile_custom_function("ok", src)
    assert callable(handler)
    assert handler({"name": "hi"}, {}) == {"total": 6, "label": "HI"}


def test_import_statements_are_blocked():
    src = "import os\ndef handler(a, c):\n    return {}"
    assert compile_custom_function("imp", src) is None


def test_write_inplacevar_print_hooks_available():
    # RestrictedPython rewrites subscript assignment, augmented assignment, and
    # print into _write_/_inplacevar_/_print_ calls; the namespace must supply
    # them or ordinary trusted code NameErrors at runtime.
    src = (
        "def handler(args, context):\n"
        "    context['seen'] = True\n"
        "    n = args.get('n', 0)\n"
        "    n += 1\n"
        "    print('debug')\n"
        "    return {'n': n, 'ctx': context}"
    )
    handler = compile_custom_function("hooks", src)
    assert callable(handler)
    assert handler({"n": 1}, {}) == {"n": 2, "ctx": {"seen": True}}


def test_full_write_guard_still_blocks_arbitrary_object_writes():
    # Adding _write_ must NOT reopen the sandbox: full_write_guard lets dict/list
    # mutate but keeps attribute writes to a plain object guarded.
    src = "def handler(args, context):\n    context.pwned = True\n    return {}"
    handler = compile_custom_function("neg", src)
    assert callable(handler)

    class _Plain:
        pass

    with pytest.raises(TypeError):
        handler({}, _Plain())


def test_custom_python_disabled_by_default_at_build(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.template import global_function as gf

    # Default config: feature OFF — build_schema short-circuits and never
    # compiles the code (no exec of author-supplied python).
    monkeypatch.setattr(gf, "ENABLE_CUSTOM_PYTHON_FUNCTIONS", False)
    called = {"n": 0}
    monkeypatch.setattr(
        parser,
        "compile_custom_function",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    adapter = gf.CustomPythonGlobalFunctionAdapter()
    result = adapter.build_schema(
        {
            "type": "custom",
            "name": "calc",
            "description": "d",
            "python_code": "def handler(a, c):\n    return {}",
        },
        wrapped_handler=lambda *a, **k: None,
    )
    assert result is None
