"""Book state: what the portfolio looks like as data, and what that state calls for.

Computed in code every run (never guessed by the model): cash versus the style's reserve, each position's sector,
beta, horizon, age and progress, concentration and style-fit flags. The risk engine enforces the portfolio policy
(reserve, sector cap, slow-thesis cap, return-on-time bar); this module makes the same facts visible.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from typing import Any

from .util import is_option, now_et, parse_occ

SESSION_HOURS = 6.5


def parse_horizon_days(text: str | None) -> float | None:
    """'2-5 trading days' -> 5, '3-10d' -> 10, 'hours' -> 0.5, '1-2 weeks' -> 10, 'intraday' -> 0.5, 'same trading day' -> 0.5.
    Uses the upper bound: a thesis is as slow as the longest it allows itself."""
    if not text:
        return None
    t = text.lower()
    if re.search(r"\b(intraday|same[- ]session|hours?|today)\b", t) and not re.search(r"\d+\s*(d|day|week|wk|month)", t):
        return 0.5
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]
    if not nums:
        if "week" in t:
            return 10.0
        if "month" in t:
            return 21.0
        if "day" in t:
            return 3.0
        return None
    n = max(nums)
    if re.search(r"week|wk", t):
        return n * 5
    if re.search(r"month", t):
        return n * 21
    if re.search(r"hour", t):
        return max(0.25, n / SESSION_HOURS)
    return n


def expected_move_pct(atr_pct: float | None, horizon_days: float | None) -> float | None:
    """Rough expected move over the holding period: daily ATR% scaled by the square root of the days."""
    if atr_pct is None or horizon_days is None:
        return None
    return round(float(atr_pct) * math.sqrt(max(0.25, float(horizon_days))), 2)


def trading_days_between(a: dt.datetime, b: dt.datetime) -> float:
    """Approximate Runs elapsed (weekdays), fractional within a day."""
    if b <= a:
        return 0.0
    days = 0.0
    cur = a
    while cur.date() < b.date():
        if cur.weekday() < 5:
            days += 1
        cur += dt.timedelta(days=1)
    if b.weekday() < 5:
        days += min(1.0, max(0.0, (b.hour + b.minute / 60 - 9.5) / SESSION_HOURS))
    return round(days, 2)


def book_state(broker: Any, journal: Any, limits: Any, style: str, table: Any = None, risk_state: Any = None) -> dict[str, Any]:
    """The portfolio as data plus the flags the policy raises. `table` is the screener table (sector, beta, ATR)."""
    st = risk_state
    positions = broker.positions()
    equity = float(getattr(st, "virtual_equity", 0) or 0) or sum(float(p.get("market_value") or 0) for p in positions)
    settled = float(getattr(st, "virtual_settled_cash", 0) or 0)
    reserve_pct = float(getattr(limits, "min_cash_pct", 0.0) or 0.0)
    reserve = equity * reserve_pct
    rows = []
    by_sector: dict[str, float] = {}
    slow_value = 0.0
    now = now_et()
    for p in positions:
        sym = p["symbol"]
        root = parse_occ(sym)["underlying"] if is_option(sym) else sym
        value = abs(float(p.get("market_value") or 0))
        th = journal.thesis_for(sym) or journal.thesis_for(root) or {}
        horizon_days = parse_horizon_days(th.get("horizon"))
        opened = None
        try:
            opened = dt.datetime.fromisoformat(th["ts"]) if th.get("ts") else None
        except Exception:
            opened = None
        age = trading_days_between(opened, now) if opened else None
        sector = beta = atr = None
        if table is not None and root in table.index:
            r = table.loc[root]
            sector = r.get("sector") if hasattr(r, "get") else None
            beta = float(r["beta_spy"]) if "beta_spy" in r and r["beta_spy"] == r["beta_spy"] else None
            atr = float(r["atr14_pct"]) if "atr14_pct" in r and r["atr14_pct"] == r["atr14_pct"] else None
        slow = horizon_days is not None and horizon_days > float(getattr(limits, "slow_horizon_days", 5) or 5)
        if slow:
            slow_value += value
        if sector:
            by_sector[sector] = by_sector.get(sector, 0.0) + value
        pnl_pct = float(p.get("unrealized_plpc") or 0) * 100
        target = th.get("target")
        try:
            tgt = float(re.findall(r"\d+(?:\.\d+)?", str(target))[0]) if target else None
        except Exception:
            tgt = None
        entry = float(p.get("avg_entry_price") or 0)
        cur = float(p.get("current_price") or 0)
        progress = round((cur - entry) / (tgt - entry) * 100, 0) if tgt and entry and tgt != entry else None
        try:
            meta = json.loads(th.get("meta") or "{}") if th else {}
        except Exception:
            meta = {}
        rows.append({"symbol": sym, "value": round(value, 2), "depends_on": meta.get("depends_on") or None, "invalidated_by": meta.get("invalidated_by") or None, "pct": round(value / equity * 100, 1) if equity else None, "sector": sector, "beta": beta, "atr_pct": atr,
                     "horizon_days": horizon_days, "age_days": age, "horizon_elapsed": bool(horizon_days is not None and age is not None and age >= horizon_days),
                     "slow": slow, "pnl_pct": round(pnl_pct, 2), "progress_to_target_pct": progress, "is_option": is_option(sym),
                     "expected_move_pct": expected_move_pct(atr, horizon_days)})
    flags: list[str] = []
    calls: list[str] = []
    if equity and settled < reserve - 0.01:
        flags.append(f"cash ${settled:.2f} is below the {style} reserve of {reserve_pct * 100:.0f}% (${reserve:.2f})")
        calls.append("no new entries until the reserve is restored: trim what is not working, take targets, or wait for settlement")
    max_sector = float(getattr(limits, "max_sector_pct", 1.0) or 1.0)
    for sec, v in sorted(by_sector.items(), key=lambda x: -x[1]):
        if equity and v / equity > max_sector + 1e-9:
            flags.append(f"sector concentration: {sec} {v / equity * 100:.0f}% > {max_sector * 100:.0f}% cap")
            calls.append(f"no adds in {sec}; trim if a name there stops working")
    max_slow = float(getattr(limits, "max_slow_pct", 1.0) or 1.0)
    if equity and slow_value / equity > max_slow + 1e-9:
        flags.append(f"slow theses (horizon > {getattr(limits, 'slow_horizon_days', 5):g}d) hold {slow_value / equity * 100:.0f}% > {max_slow * 100:.0f}% allowed under {style}")
        calls.append("the style wants agility: the next entries are fast setups or option expressions, or a slow position gets cut")
    for r in rows:
        if r["horizon_elapsed"]:
            flags.append(f"{r['symbol']}: thesis horizon ({r['horizon_days']:g}d) elapsed at {r['age_days']}d, {r['pnl_pct']:+.2f}%")
            calls.append(f"{r['symbol']}: exit or re-thesis in writing (set_exit_levels / record_note); a probe that did nothing by its horizon is closed under Aggressive")
        if r["pct"] and r["pct"] > float(getattr(limits, "max_position_pct", 1.0)) * 100 + 1e-9:
            flags.append(f"{r['symbol']} is {r['pct']}% of equity, over the {float(getattr(limits, 'max_position_pct', 1.0)) * 100:.0f}% cap")
    return {"style": style, "equity": round(equity, 2), "settled_cash": round(settled, 2), "cash_pct": round(settled / equity * 100, 1) if equity else None,
            "reserve_pct": reserve_pct * 100, "reserve": round(reserve, 2), "reserve_ok": settled >= reserve - 0.01,
            "positions": rows, "by_sector_pct": {k: round(v / equity * 100, 1) for k, v in by_sector.items()} if equity else {},
            "slow_pct": round(slow_value / equity * 100, 1) if equity else 0.0, "flags": flags, "calls_for": calls,
            "deployable": round(max(0.0, settled - reserve), 2)}


def book_state_text(bs: dict[str, Any]) -> str:
    lines = [f"equity ${bs['equity']:.2f} · settled cash ${bs['settled_cash']:.2f} ({bs['cash_pct']}%) · reserve {bs['reserve_pct']:.0f}% = ${bs['reserve']:.2f} · deployable ${bs['deployable']:.2f}"]
    for r in bs["positions"]:
        lines.append(f"- {r['symbol']}: ${r['value']:.2f} ({r['pct']}%), {r['sector'] or '?'}, beta {round(r['beta'], 2) if r['beta'] is not None else '?'}, ATR {round(r['atr_pct'], 2) if r['atr_pct'] is not None else '?'}%/d, "
                     f"horizon {r['horizon_days'] if r['horizon_days'] is not None else '?'}d, age {r['age_days'] if r['age_days'] is not None else '?'}d, "
                     f"P&L {r['pnl_pct']:+.2f}%" + (f", {r['progress_to_target_pct']:.0f}% of the way to target" if r['progress_to_target_pct'] is not None else "") + (" · SLOW" if r["slow"] else "") + (" · HORIZON ELAPSED" if r["horizon_elapsed"] else "")
                     + (f" · depends on: {r['depends_on']}" if r.get("depends_on") else " · depends on: (not declared)") + (f" · invalidated by: {r['invalidated_by']}" if r.get("invalidated_by") else ""))
    if bs["by_sector_pct"]:
        lines.append("sectors: " + ", ".join(f"{k} {v}%" for k, v in sorted(bs["by_sector_pct"].items(), key=lambda x: -x[1])) + f" · slow theses {bs['slow_pct']}%")
    if bs["flags"]:
        lines.append("FLAGS: " + " | ".join(bs["flags"]))
        lines.append("THIS STATE CALLS FOR: " + " | ".join(bs["calls_for"]))
    else:
        lines.append("no policy flags: the book is within its reserve, sector and horizon limits")
    return "\n".join(lines)
