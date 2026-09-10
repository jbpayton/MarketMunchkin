"""Executes agent analysis code against a prepared dataset. Runs with -I -S in a clean env; see __init__.validate."""
import ast
import io
import pickle
import resource
import sys
import traceback

resource.setrlimit(resource.RLIMIT_AS, (3 * 1024 ** 3, 3 * 1024 ** 3))
resource.setrlimit(resource.RLIMIT_CPU, (35, 35))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))

# -S skips site.py (and venv detection); the parent passes the venv's site-packages explicitly
sys.path.insert(0, sys.argv[3])

import math, statistics, json, re, itertools, collections, datetime
import numpy as np
import pandas as pd
try:
    import scipy
    import scipy.stats as stats
except Exception:  # pragma: no cover
    scipy = stats = None
try:
    import pandas_ta as ta
except Exception:
    ta = None

with open(sys.argv[1], "rb") as f:
    data = pickle.load(f)
code = open(sys.argv[2]).read()

ns = {"pd": pd, "np": np, "math": math, "statistics": statistics, "json": json, "re": re, "itertools": itertools,
      "collections": collections, "datetime": datetime, "scipy": scipy, "stats": stats, "ta": ta, **data}
pd.set_option("display.width", 200, "display.max_columns", 40, "display.max_rows", 60, "display.float_format", lambda x: f"{x:.4g}")

buf = io.StringIO()
old = sys.stdout
sys.stdout = buf
try:
    tree = ast.parse(code)
    last_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body[-1].value)
        tree.body = tree.body[:-1]
    exec(compile(tree, "<analysis>", "exec"), ns)
    if last_expr is not None:
        val = eval(compile(last_expr, "<analysis>", "eval"), ns)
        if val is not None:
            print(val.to_string() if hasattr(val, "to_string") else val)
except Exception:
    sys.stdout = old
    print(buf.getvalue())
    print("[error]\n" + "".join(traceback.format_exception(*sys.exc_info()))[-2500:])
    sys.exit(1)
sys.stdout = old
print(buf.getvalue())
