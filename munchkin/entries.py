"""Armed conditional entries: the agent states intent (trigger, size, stop, target, thesis) and the watcher
executes it the moment price crosses the trigger, through the same risk engine as a manual buy."""
from __future__ import annotations

import datetime as dt
import re
import logging
from typing import Any

from .util import now_et

log = logging.getLogger("munchkin.entries")

KEY = "entries:"


def trigger_hit(rec: dict[str, Any], price: float) -> bool:
    d = rec.get("direction", "above")
    t = float(rec["trigger_price"])
    return price >= t if d == "above" else price <= t


def not_yet(rec: dict[str, Any], now: dt.datetime) -> bool:
    nb = rec.get("not_before")
    if not nb:
        return False
    try:
        t = dt.datetime.fromisoformat(nb)
        if t.tzinfo is None:
            t = t.replace(tzinfo=now.tzinfo)
        return now < t
    except Exception:
        return False


def index_ok(rec: dict[str, Any], spy_chg_pct: float | None) -> bool:
    thr = rec.get("spy_min_chg_pct")
    if thr is None:
        return True
    return spy_chg_pct is not None and spy_chg_pct >= float(thr)


def expired(rec: dict[str, Any], now: dt.datetime) -> bool:
    exp = rec.get("expires")
    if not exp:
        return False
    try:
        return now >= dt.datetime.fromisoformat(exp)
    except Exception:
        return False


class EntryBook:
    def __init__(self, journal) -> None:
        self.j = journal

    def all(self) -> dict[str, dict[str, Any]]:
        rows = self.j.conn.execute("SELECT key, value FROM kv WHERE key LIKE 'entries:%'").fetchall()
        import json
        out = {}
        for r in rows:
            if r["value"] and r["value"] != "null":
                out[r["key"][len(KEY):]] = json.loads(r["value"])
        return out

    def get(self, symbol: str) -> dict[str, Any] | None:
        return self.j.get(KEY + symbol.upper())

    def arm(self, symbol: str, direction: str, trigger_price: float, notional: float, stop_price: float, target_price: float,
            thesis: str, catalyst_grade: str, horizon: str, expires_hours: float | None, session_id: int | None,
            max_chase_pct: float = 1.0, not_before: str | None = None, spy_min_chg_pct: float | None = None,
            expression: str = "stock", dte_target: int = 14, opt_stop_pct: float = 0.5, opt_target_pct: float = 1.0) -> dict[str, Any]:
        rec = {"symbol": symbol.upper(), "direction": "below" if direction == "below" else "above", "trigger_price": float(trigger_price),
               "notional": round(float(notional), 2), "stop_price": float(stop_price), "target_price": float(target_price),
               "thesis": thesis, "catalyst_grade": catalyst_grade, "horizon": horizon, "session_id": session_id,
               "armed_at": now_et().isoformat(timespec="seconds"), "max_chase_pct": max_chase_pct,
               "not_before": not_before, "spy_min_chg_pct": spy_min_chg_pct,
               "expression": expression, "dte_target": int(dte_target), "opt_stop_pct": opt_stop_pct, "opt_target_pct": opt_target_pct,
               "expires": (now_et() + dt.timedelta(hours=expires_hours)).isoformat(timespec="seconds") if expires_hours else None}
        self.j.set(KEY + rec["symbol"], rec)
        return rec

    def disarm(self, symbol: str) -> bool:
        if self.get(symbol) is None:
            return False
        self.j.set(KEY + symbol.upper(), None)
        return True

    def describe(self) -> str:
        recs = self.all()
        if not recs:
            return "(no armed entries)"
        return "\n".join(f"- {r['symbol']}: {r.get('expression', 'stock')} ${r['notional']:.0f} when price {r['direction']} {r['trigger_price']}"
                         + (f" not before {r['not_before'][:16]}" if r.get('not_before') else "")
                         + (f" and SPY today >= {r['spy_min_chg_pct']}%" if r.get('spy_min_chg_pct') is not None else "")
                         + f" | stop {r['stop_price']} target {r['target_price']} | grade {r['catalyst_grade']} | expires {(r.get('expires') or 'never')[:16]} | {r['thesis'][:90]}"
                         for r in recs.values())


def arm_outcome(market: Any, rec: dict[str, Any]) -> dict[str, Any]:
    """How an armed entry actually behaved: did price touch the trigger, how close it came, and where the name went.
    Used when an arm expires or is disarmed so the agent can review whether its triggers were reachable."""
    out = {"symbol": rec["symbol"], "direction": rec["direction"], "trigger": rec["trigger_price"], "armed_at": rec.get("armed_at")}
    try:
        df = market.bars(rec["symbol"], "5Min", 400)
        df.index = df.index.tz_convert("America/New_York")
        t0 = dt.datetime.fromisoformat(rec["armed_at"])
        after = df[df.index >= t0]
        if after.empty:
            out["note"] = "no bars since armed"
            return out
        p0, last = float(after["close"].iloc[0]), float(after["close"].iloc[-1])
        trig = float(rec["trigger_price"])
        if rec["direction"] == "above":
            ext = float(after["high"].max()); closest = (trig / ext - 1) * 100; touched = ext >= trig
        else:
            ext = float(after["low"].min()); closest = (1 - trig / ext) * 100; touched = ext <= trig
        out.update({"price_at_arm": round(p0, 2), "gap_at_arm_pct": round(abs(trig / p0 - 1) * 100, 2), "closest_pct": round(closest, 2),
                    "touched": bool(touched), "extreme": round(ext, 2), "last": round(last, 2), "drift_pct": round((last / p0 - 1) * 100, 2),
                    "bars": int(len(after))})
    except Exception as e:
        out["note"] = f"review failed: {str(e)[:80]}"
    return out


def arm_outcome_text(o: dict[str, Any]) -> str:
    if "touched" not in o:
        return f"ARM REVIEW {o['symbol']}: {o.get('note', '')}"
    verdict = "touched" if o["touched"] else f"never reached ({abs(o['closest_pct']):.2f}% short at the closest)"
    return (f"ARM REVIEW {o['symbol']} {o['direction']} {o['trigger']}: armed at {o['price_at_arm']} ({o['gap_at_arm_pct']}% away), {verdict}; "
            f"name drifted {o['drift_pct']:+.2f}% to {o['last']} while armed")


_CAP_RE = re.compile(r"would be \$([\d.]+).*?> cap \$([\d.]+)")


def clamp_to_cap(notional: float, violations: list[str], floor: float = 25.0) -> tuple[float, str | None]:
    """If the only problem is that the notional overshoots the per-underlying cap, shrink it to fit (never below `floor`)."""
    caps = [m for v in violations for m in [_CAP_RE.search(v)] if m]
    if len(violations) != 1 or not caps:
        return notional, None
    exposure, cap = float(caps[0].group(1)), float(caps[0].group(2))
    room = cap - (exposure - notional) - 0.05
    if room < floor:
        return notional, None
    new = float(int(room * 100) / 100)
    return new, f"notional trimmed from ${notional:.2f} to ${new:.2f} to fit the cap"


def execute_entry(rec: dict[str, Any], price: float, risk, broker, market, journal, exits, limits=None) -> str:
    """Run the same checks as buy_stock, then buy at market (notional) and arm the stop. Option expressions are resolved at fire time."""
    sym = rec["symbol"]
    chase = (price / rec["trigger_price"] - 1) * 100 if rec["direction"] == "above" else (rec["trigger_price"] / price - 1) * 100
    if chase > rec.get("max_chase_pct", 1.0):
        return f"{sym}: trigger hit but price {price} is {chase:.1f}% past the trigger (max chase {rec.get('max_chase_pct', 1.0)}%); not chasing, entry left armed"
    if rec.get("expression", "stock") != "stock":
        from .optentry import execute_option_entry
        from .config import SETTINGS
        return execute_option_entry(rec, price, risk, broker, market, journal, exits, limits or SETTINGS.risk)
    pos = broker.positions()
    st = risk.state(positions=pos)
    viol = risk.check_stock_buy(sym, rec["notional"], price, "market", None, st, pos, rec.get("catalyst_grade"))
    trimmed = None
    if viol:
        new_notional, trimmed = clamp_to_cap(float(rec["notional"]), viol)
        if trimmed:
            rec = dict(rec, notional=new_notional)
            viol = risk.check_stock_buy(sym, rec["notional"], price, "market", None, st, pos, rec.get("catalyst_grade"))
    if not st.market_open:
        viol.append("market closed")
    if viol:
        journal.add_decision(rec.get("session_id"), "open", sym, side="buy", qty=rec["notional"], price=price, thesis=rec["thesis"],
                             target=str(rec["target_price"]), stop=str(rec["stop_price"]), horizon=rec.get("horizon"), status="blocked",
                             meta={"conditional": True, "violations": viol, "catalyst_grade": rec.get("catalyst_grade")}, underlying=sym)
        return f"{sym}: trigger hit but the risk engine blocked the entry: {'; '.join(viol)}"
    o = broker.submit_stock_order(sym, "buy", notional=rec["notional"], order_type="market")
    import time
    for _ in range(4):
        time.sleep(1.0)
        o = broker.order(o["id"])
        if o.get("status") in ("filled", "canceled", "rejected", "expired"):
            break
    journal.add_decision(rec.get("session_id"), "open", sym, side="buy", qty=rec["notional"], price=o.get("filled_avg_price") or price,
                         thesis=rec["thesis"], target=str(rec["target_price"]), stop=str(rec["stop_price"]), horizon=rec.get("horizon"),
                         order_id=o.get("id"), status=o.get("status", "submitted"),
                         meta={"conditional": True, "catalyst_grade": rec.get("catalyst_grade"), "stop_price": rec["stop_price"], "target_price": rec["target_price"]},
                         underlying=sym)
    exits.register(sym, rec["stop_price"], rec["target_price"], rec.get("session_id"))
    try:
        exits.ensure()
    except Exception as e:
        log.warning("stop arming after conditional entry failed: %s", e)
    return f"{sym}: CONDITIONAL ENTRY EXECUTED at {o.get('filled_avg_price') or price} (order {o.get('id')}, {o.get('status')}); stop {rec['stop_price']} armed, target {rec['target_price']}"
