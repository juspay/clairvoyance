import datetime
import inspect
import operator
import types
from typing import Any, Callable, Dict, Optional

from RestrictedPython import (
    PrintCollector,
    compile_restricted_exec,
    safe_builtins,
    safe_globals,
)
from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter
from RestrictedPython.Guards import (
    full_write_guard,
    guarded_iter_unpack_sequence,
    safer_getattr,
)

from app.core.logger import logger

# =============================================================================
# CUSTOM FUNCTION COMPILATION UTILITIES
# =============================================================================
#
# SECURITY: template `python_code` is author-supplied and is compiled+executed
# under RestrictedPython — a transforming compiler that rewrites attribute and
# subscript access to go through guard functions, so the classic CPython
# sandbox escape (walking `().__class__.__base__.__subclasses__()` to reach
# subprocess.Popen / recover real builtins) is rejected at COMPILE time, not by
# a fragile AST name denylist. `safer_getattr` additionally blocks every
# attribute whose name starts with "_". Even so, an in-process interpreter is
# not a hard security boundary: this whole feature is gated OFF by default via
# ENABLE_CUSTOM_PYTHON_FUNCTIONS (enforced at flow-build time), and true
# out-of-process isolation is the tracked defense-in-depth follow-up.

# The datetime MODULE holds module references of its own; handlers only ever
# need its value types, so they get those under the same names.
_SAFE_DATETIME = types.SimpleNamespace(
    date=datetime.date,
    datetime=datetime.datetime,
    time=datetime.time,
    timedelta=datetime.timedelta,
    timezone=datetime.timezone,
    tzinfo=datetime.tzinfo,
    MINYEAR=datetime.MINYEAR,
    MAXYEAR=datetime.MAXYEAR,
    UTC=datetime.timezone.utc,
)

# Value allow-list layered on top of RestrictedPython's own safe_builtins. These
# are safe builtins the legacy sandbox exposed and existing handlers may rely on.
_EXTRA_SAFE_BUILTINS: Dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bin": bin,
    "bool": bool,
    "chr": chr,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "format": format,
    "frozenset": frozenset,
    "hasattr": hasattr,
    "hash": hash,
    "hex": hex,
    "int": int,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "oct": oct,
    "ord": ord,
    "pow": pow,
    "print": print,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "type": type,
    "zip": zip,
    "datetime": _SAFE_DATETIME,
}


def _no_module(value: Any) -> Any:
    """Backstop: a module object in sandboxed hands is a door out of it."""
    if isinstance(value, types.ModuleType):
        raise AttributeError("module objects are not available in custom functions")
    return value


def _guarded_getattr(obj: Any, name: str, default: Any = None) -> Any:
    return _no_module(safer_getattr(obj, name, default))


def _guarded_getitem(obj: Any, index: Any) -> Any:
    return _no_module(default_guarded_getitem(obj, index))


class CompilationError(Exception):
    """Raised when custom function compilation fails."""


# Augmented-assignment operators. RestrictedPython rewrites ``n += v`` into
# ``n = _inplacevar_("+=", n, v)`` but ships no default ``_inplacevar_``; without
# one, any ``+=``/``-=``/... in trusted code raises NameError at runtime.
_INPLACE_OPS: Dict[str, Callable[[Any, Any], Any]] = {
    "+=": operator.iadd,
    "-=": operator.isub,
    "*=": operator.imul,
    "/=": operator.itruediv,
    "//=": operator.ifloordiv,
    "%=": operator.imod,
    "**=": operator.ipow,
    "<<=": operator.ilshift,
    ">>=": operator.irshift,
    "&=": operator.iand,
    "|=": operator.ior,
    "^=": operator.ixor,
    "@=": operator.imatmul,
}


def _guarded_inplacevar(op: str, target: Any, value: Any) -> Any:
    """``_inplacevar_`` hook for augmented assignment on a plain-name target.

    RestrictedPython only routes bareword targets through this hook —
    attribute/subscript augmented targets (``obj.x += 1``) are rejected at
    compile time — so this is semantics-identical to the already-permitted
    ``n = n + v`` and opens no new capability.
    """
    func = _INPLACE_OPS.get(op)
    if func is None:
        raise CompilationError(f"Unsupported augmented assignment operator: {op!r}")
    return func(target, value)


def _build_restricted_namespace() -> Dict[str, Any]:
    """Guarded globals for executing compiled custom-function code.

    Beyond the read guards, RestrictedPython's transformer also emits calls to
    ``_write_`` (attribute/subscript assignment), ``_inplacevar_`` (augmented
    assignment), and ``_print_`` (``print``). Supplying RestrictedPython's own
    safe primitives — ``full_write_guard`` (dict/list mutate freely, everything
    else stays write-guarded), ``_guarded_inplacevar``, and ``PrintCollector``
    (captures output in-memory, never touches stdout/files) — lets ordinary
    trusted code run without NameErrors while preserving the sandbox.
    """
    namespace: Dict[str, Any] = dict(safe_globals)
    builtins_ns = dict(safe_builtins)
    builtins_ns.update(_EXTRA_SAFE_BUILTINS)
    namespace["__builtins__"] = builtins_ns
    namespace["_getattr_"] = _guarded_getattr
    namespace["_getitem_"] = _guarded_getitem
    namespace["_getiter_"] = default_guarded_getiter
    namespace["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
    namespace["_write_"] = full_write_guard
    namespace["_inplacevar_"] = _guarded_inplacevar
    namespace["_print_"] = PrintCollector
    return namespace


def compile_custom_function(name: str, source: str) -> Optional[Callable]:
    """
    Compile python_code source into a callable handler function.

    The source code must define a top-level callable named 'handler' that accepts
    two arguments: (args, context). Compilation and execution happen under
    RestrictedPython (see the module security note above).

    Args:
        name: Function name (for logging/debugging)
        source: Python source code string

    Returns:
        The handler callable, or None if compilation failed (logs warning)
    """
    try:
        result = compile_restricted_exec(source, filename=f"<custom_function:{name}>")
        if result.errors:
            raise CompilationError("; ".join(result.errors))
        code_obj = result.code
        if code_obj is None:
            raise CompilationError("RestrictedPython produced no code object")

        namespace = _build_restricted_namespace()
        exec(code_obj, namespace)  # noqa: S102 - RestrictedPython-compiled code

        # Extract handler function
        handler = namespace.get("handler")
        if not handler:
            logger.warning(
                f"[custom function '{name}'] python_code does not define a top-level 'handler' callable"
            )
            return None

        if not callable(handler):
            logger.warning(
                f"[custom function '{name}'] 'handler' is not callable (found {type(handler).__name__})"
            )
            return None

        # Validate signature accepts at least 2 args
        sig = inspect.signature(handler)
        param_count = len(sig.parameters)
        if param_count < 2:
            logger.warning(
                f"[custom function '{name}'] 'handler' must accept at least 2 arguments (args, context), got {param_count}"
            )
            return None

        return handler

    except SyntaxError as e:
        logger.warning(f"[custom function '{name}'] syntax error: {e}")
        return None
    except CompilationError as e:
        logger.warning(f"[custom function '{name}'] compilation failed: {e}")
        return None
    except Exception as e:
        logger.warning(
            f"[custom function '{name}'] compilation failed: {e}", exc_info=True
        )
        return None
