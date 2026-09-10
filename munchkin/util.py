"""Small shared helpers."""
from __future__ import annotations

import datetime as dt
import decimal
import enum
import re
import uuid
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def now_et() -> dt.datetime:
    return dt.datetime.now(tz=ET)


def to_plain(obj: Any) -> Any:
    """Convert pydantic/alpaca objects into JSON-friendly plain python."""
    if isinstance(obj, enum.Enum):
        return obj.value
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (uuid.UUID,)):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (dt.datetime,)):
        return obj.astimezone(ET).isoformat(timespec="seconds") if obj.tzinfo else obj.isoformat(timespec="seconds")
    if isinstance(obj, (dt.date,)):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        return to_plain(obj.model_dump())
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_plain(v) for v in obj]
    return str(obj)


def fnum(x: Any, nd: int = 2) -> float | None:
    try:
        if x is None:
            return None
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def fmt_ts(ts: dt.datetime | None, with_date: bool = True) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    ts = ts.astimezone(ET)
    return ts.strftime("%m-%d %H:%M" if with_date else "%H:%M")


def parse_occ(symbol: str) -> dict[str, Any] | None:
    m = OCC_RE.match(symbol.upper().replace(" ", ""))
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    exp = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    return {"underlying": root, "expiration": exp, "type": "call" if cp == "C" else "put",
            "strike": int(strike) / 1000.0}


def occ_human(symbol: str) -> str:
    p = parse_occ(symbol)
    if not p:
        return symbol
    s = p["strike"]
    s_txt = f"{s:.0f}" if s == int(s) else f"{s:g}"
    return f"{p['underlying']} {p['expiration'].isoformat()} {s_txt}{'C' if p['type']=='call' else 'P'}"


def is_option(symbol: str) -> bool:
    return parse_occ(symbol) is not None


def business_days_back(n: int, from_date: dt.date | None = None) -> dt.date:
    d = from_date or now_et().date()
    while n > 0:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def md_table(rows: list[dict[str, Any]], cols: list[str] | None = None, max_rows: int = 50) -> str:
    """Compact pipe table (cheap on tokens)."""
    if not rows:
        return "(none)"
    cols = cols or list(rows[0].keys())
    out = [" | ".join(cols)]
    for r in rows[:max_rows]:
        vals = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, float):
                v = f"{v:.4g}" if abs(v) < 1 else f"{v:,.2f}" if abs(v) < 1e5 else f"{v:,.0f}"
            vals.append("" if v is None else str(v))
        out.append(" | ".join(vals))
    if len(rows) > max_rows:
        out.append(f"... ({len(rows) - max_rows} more rows)")
    return "\n".join(out)
