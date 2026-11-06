"""Bounded, untrusted runtime hints derived from the exact submitted source."""

import ast
import re


REPAIR_EXCEPTION_TYPES = frozenset({
    "NameError", "TypeError", "AttributeError", "KeyError", "IndexError",
    "ValueError", "ImportError", "RuntimeError", "ZeroDivisionError",
})


def source_diagnostic(raw, source: str) -> dict | None:
    """Discard arbitrary container strings; derive the function from source AST."""
    if not isinstance(raw, dict) or set(raw) not in (
        {"exception_type", "line"}, {"exception_type", "line", "reason"}
    ):
        return None
    kind, line = raw["exception_type"], raw["line"]
    if (not isinstance(kind, str) or kind not in REPAIR_EXCEPTION_TYPES
            or type(line) is not int or line < 1
            or not isinstance(source, str) or len(source.encode("utf-8")) > 32768
            or line > len(source.splitlines())):
        return None
    if "reason" in raw and (raw["reason"] != "OPEN_STRICT_UNSUPPORTED" or kind != "TypeError"):
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    if "reason" in raw and not any(
        isinstance(node, ast.Call) and node.lineno <= line <= (node.end_lineno or node.lineno)
        and (isinstance(node.func, ast.Name) and node.func.id == "open"
             or isinstance(node.func, ast.Attribute) and node.func.attr == "open")
        and any(keyword.arg == "strict" for keyword in node.keywords)
        for node in ast.walk(tree)
    ):
        return None
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    name = "<module>"
    if functions:
        node = min(functions, key=lambda item: (item.end_lineno - item.lineno, -item.lineno))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,99}", node.name):
            return None
        name = node.name
    result = {"exception_type": kind, "line": line, "function": name}
    if "reason" in raw:
        result["reason"] = "OPEN_STRICT_UNSUPPORTED"
    return result


def verified_diagnostic(value, source: str) -> dict | None:
    """Recheck even a previously sanitized hint before an external model call."""
    if not isinstance(value, dict) or set(value) not in (
        {"exception_type", "line", "function"},
        {"exception_type", "line", "function", "reason"},
    ):
        return None
    raw = {key: value[key] for key in ("exception_type", "line", "reason") if key in value}
    expected = source_diagnostic(raw, source)
    return expected if expected == value else None
