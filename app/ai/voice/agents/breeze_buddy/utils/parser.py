import ast
import inspect
from typing import Any, Callable, Dict, Optional

from app.core.logger import logger

# =============================================================================
# CUSTOM FUNCTION COMPILATION UTILITIES
# =============================================================================


class CompilationError(Exception):
    """Raised when custom function compilation fails."""


def compile_custom_function(name: str, source: str) -> Optional[Callable]:
    """
    Compile python_code source into a callable handler function.

    The source code must define a top-level callable named 'handler' that accepts
    two arguments: (args, context).

    Args:
        name: Function name (for logging/debugging)
        source: Python source code string

    Returns:
        The handler callable, or None if compilation failed (logs warning)
    """
    try:
        # Security: Parse AST to check for dangerous constructs
        tree = ast.parse(source)
        _security_check(tree, name)

        # Compile to code object with descriptive filename for tracebacks
        code_obj = compile(source, f"<custom_function:{name}>", "exec")

        # Execute in isolated namespace with restricted builtins
        safe_builtins = {
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
        }
        namespace: Dict[str, Any] = {"__builtins__": safe_builtins}
        exec(code_obj, namespace)  # noqa: S102

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


def _security_check(tree: ast.AST, name: str) -> None:
    """
    Check AST for potentially dangerous constructs.

    Blocks:
    - Import statements (could import dangerous modules)
    - Exec/eval/compile calls
    - __import__ builtin
    - Open file operations
    """
    blocked_nodes = (
        ast.Import,
        ast.ImportFrom,
    )

    blocked_names = {
        "exec",
        "eval",
        "compile",
        "open",
        "__import__",
        "exit",
        "quit",
        "help",
        "raw_input",
        "input",
    }

    for node in ast.walk(tree):
        # Check for blocked node types
        if isinstance(node, blocked_nodes):
            node_type = type(node).__name__
            raise CompilationError(
                f"Security violation: '{node_type}' statements are not allowed in custom functions"
            )

        # Check for calls to blocked functions
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in blocked_names:
                    raise CompilationError(
                        f"Security violation: '{node.func.id}' function is not allowed"
                    )

            # Check for method calls like os.system, subprocess.run, etc.
            if isinstance(node.func, ast.Attribute):
                # Block obvious dangerous patterns
                dangerous_attrs = ["system", "popen", "call", "run", "execve"]
                if node.func.attr in dangerous_attrs:
                    raise CompilationError(
                        f"Security violation: method '{node.func.attr}' is not allowed"
                    )
