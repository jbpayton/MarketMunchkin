"""Sandboxed pandas/numpy analysis for the agent: data is prepared by the parent (which holds credentials),
the code runs in a separate interpreter with a clean environment, no network helpers, no file access, CPU and
memory limits, and an AST allowlist for imports and names."""
from __future__ import annotations

import ast
import json
import os
import pickle
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

RUNNER = Path(__file__).with_name("runner.py")
ALLOWED_IMPORTS = {"pandas", "numpy", "scipy", "math", "statistics", "datetime", "json", "re", "itertools", "collections",
                   "functools", "operator", "pandas_ta", "typing", "dataclasses", "decimal", "fractions", "random", "time"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr",
                   "input", "breakpoint", "exit", "quit", "help", "memoryview", "bytearray"}


def validate(code: str) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax error: {e}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                if n.split(".")[0] not in ALLOWED_IMPORTS:
                    return f"import of '{n}' is not allowed (allowed: {sorted(ALLOWED_IMPORTS)})"
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            return f"'{node.id}' is not allowed"
        if isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr in ("to_csv", "to_pickle", "to_parquet", "to_excel", "to_sql", "read_csv", "read_pickle", "read_parquet", "read_sql")):
            return f"attribute '{node.attr}' is not allowed"
    return None


def run(code: str, dataset: dict[str, Any], timeout_s: int = 40, max_chars: int = 7000) -> str:
    err = validate(code)
    if err:
        return f"REJECTED: {err}"
    with tempfile.TemporaryDirectory() as td:
        dpath = Path(td) / "data.pkl"
        cpath = Path(td) / "code.py"
        with open(dpath, "wb") as f:
            pickle.dump(dataset, f)
        cpath.write_text(code)
        env = {"PATH": "/usr/bin:/bin", "HOME": td, "PYTHONHASHSEED": "0", "MPLBACKEND": "Agg", "OMP_NUM_THREADS": "2"}
        try:
            import sysconfig
            site = sysconfig.get_paths()["purelib"]  # the venv's site-packages; -S skips site.py so the child gets it explicitly
            p = subprocess.run([sys.executable, "-I", "-S", str(RUNNER), str(dpath), str(cpath), site], cwd=td, env=env,
                               capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return f"ERROR: analysis exceeded {timeout_s}s"
        out = (p.stdout or "")
        if p.returncode != 0:
            out += "\n[stderr]\n" + (p.stderr or "")[-2500:]
        out = out.strip() or "(no output; print() what you want to see, or end with an expression)"
        if len(out) > max_chars:
            out = out[:max_chars] + f"\n...[truncated at {max_chars} chars]"
        return out
