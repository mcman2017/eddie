"""$0 static checks on an R1.worker script, before the sandbox spends any time on it.

This gives the model precise feedback for its next attempt ("line 4 imports os"). It is NOT a
security boundary: Python can reach the operating system in ways no allowlist can see. The
boundary is the container sandbox (`SandboxRunner(isolation="docker")`: no network, no home,
no kubeconfig, no ledger), which every live witness runs in.
"""

from __future__ import annotations

import ast

MAX_SCRIPT_BYTES = 12_000
ALLOWED_ROOTS = frozenset({
    "numpy", "scipy", "mpmath", "matplotlib", "math", "cmath", "json", "itertools", "functools", "fractions",
    "statistics", "decimal", "collections", "dataclasses", "typing", "random", "__future__",
})
BANNED_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "breakpoint", "input", "help", "memoryview", "exit", "quit",
})
BANNED_ATTRS = frozenset({
    "ctypeslib", "ctypes", "subprocess", "system", "popen", "spawn", "_fork_exec", "fork", "os", "sys",
    "importlib", "builtins", "loader", "socket", "load_library", "CDLL",
})
WRITABLE = frozenset({"witness.png", "witness.json"})


def check_script(source: str) -> list[str]:
    """Problems found, each with its line number; empty when the script may run."""
    problems: list[str] = []
    if len(source.encode("utf-8")) > MAX_SCRIPT_BYTES:
        problems.append(f"the script is longer than {MAX_SCRIPT_BYTES:,} bytes")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return problems + [f"line {exc.lineno}: syntax error: {exc.msg}"]
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_ROOTS:
                    problems.append(f"line {line}: import {alias.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or root not in ALLOWED_ROOTS:
                problems.append(f"line {line}: from {node.module} import ... is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in BANNED_ATTRS:
                problems.append(f"line {line}: attribute .{node.attr} is not allowed")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") and node.id not in ("__name__", "__file__"):
                problems.append(f"line {line}: name {node.id} is not allowed")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else None
            if name in BANNED_CALLS:
                problems.append(f"line {line}: {name}() is not allowed")
            if name == "open":
                target = node.args[0] if node.args else None
                if not (isinstance(target, ast.Constant) and target.value in WRITABLE):
                    problems.append(f"line {line}: open() may only write witness.json")
    if "witness.png" not in source:
        problems.append('the script never saves "witness.png"')
    # one message per problem, in source order
    seen: set[str] = set()
    return [p for p in problems if not (p in seen or seen.add(p))]
