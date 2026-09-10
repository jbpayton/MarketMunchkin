"""Options as the agility tool: resolve a contract or debit vertical from an intent (expression, DTE, delta),
execute it through the risk engine, and manage spreads by their net value (no resting stop is possible for a spread)."""
from __future__ import annotations

import datetime as dt
import logging
import math
import time
from typing import Any

from .util import now_et, occ_human, parse_occ

log = logging.getLogger("munchkin.optentry")

EXPRESSIONS = ("stock", "call", "put", "call_spread", "put_spread")


def tick_round(px: float) -> float:
    """Alpaca option price increments: $0.01 under $3, $0.05 at/above."""
    if px < 3.0:
        return round(math.floor(px * 100 + 1e-9) / 100, 2)
    return round(math.floor(px * 20 + 1e-9) / 20, 2)


def resolve_contracts(rows: list[dict[str, Any]], expression: str, dte_target: int, min_dte: int,
                      delta_single: float = 0.50, delta_long: float = 0.55, delta_short: float = 0.30) -> tuple[list[tuple[dict, str]], str]:
    """Pick contracts from chain rows (one type). Returns ([(row, 'buy'|'sell')...], note)."""
    usable = [r for r in rows if r.get("bid") and r.get("ask") and r.get("delta") is not None and r["dte"] >= min_dte]
    if not usable:
        return [], "no usable contracts (need bid/ask/delta and DTE >= min)"
    exps = sorted({r["exp"] for r in usable}, key=lambda e: abs(next(x["dte"] for x in usable if x["exp"] == e) - dte_target))
    exp = exps[0]
    cs = [r for r in usable if r["exp"] == exp]
    if expression in ("call", "put"):
        r = min(cs, key=lambda x: abs(abs(x["delta"]) - delta_single))
        return [(r, "buy")], f"{exp} single, delta {r['delta']}"
    is_call = expression == "call_spread"
    long_leg = min(cs, key=lambda x: abs(abs(x["delta"]) - delta_long))
    further = [x for x in cs if (x["strike"] > long_leg["strike"] if is_call else x["strike"] < long_leg["strike"])]
    if not further:
        return [(long_leg, "buy")], f"{exp} no short strike available; single instead"
    short_leg = min(further, key=lambda x: abs(abs(x["delta"]) - delta_short))
    return [(long_leg, "buy"), (short_leg, "sell")], f"{exp} vertical {long_leg['strike']:g}/{short_leg['strike']:g}, deltas {long_leg['delta']}/{short_leg['delta']}"


def resolve_affordable(rows: list[dict[str, Any]], expression: str, notional: float, dte_target: int, min_dte: int) -> tuple[list[tuple[dict, str]], float, str]:
    """Closest structure to the requested spec whose cost fits the notional. Search order: requested deltas at the
    target expiry; then narrower verticals / lower-delta singles; then shorter expiries. Returns (legs, limit, note)."""
    usable = [r for r in rows if r.get("bid") and r.get("ask") and r.get("delta") is not None and r["dte"] >= min_dte]
    if not usable:
        return [], 0.0, "no usable contracts"
    exps = sorted({r["exp"]: next(x["dte"] for x in usable if x["exp"] == r["exp"]) for r in usable}.items(), key=lambda kv: abs(kv[1] - dte_target))
    single = expression in ("call", "put")
    is_call = expression.startswith("call")
    candidates: list[tuple[float, list[tuple[dict, str]], float, str]] = []  # (distance score, legs, limit, note)
    for exp, dte in exps[:3]:
        cs = sorted([r for r in usable if r["exp"] == exp], key=lambda r: r["strike"])
        if single:
            for d in (0.50, 0.45, 0.40, 0.35, 0.30):
                r = min(cs, key=lambda x: abs(abs(x["delta"]) - d))
                lim = limit_for([(r, "buy")])
                if lim * 100 <= notional:
                    candidates.append((abs(dte - dte_target) / 7 + (0.50 - d) * 10, [(r, "buy")], lim, f"{exp} single delta {r['delta']}"))
                    break
        else:
            for dl in (0.55, 0.50, 0.45):
                lg = min(cs, key=lambda x: abs(abs(x["delta"]) - dl))
                further = [x for x in cs if (x["strike"] > lg["strike"] if is_call else x["strike"] < lg["strike"])]
                further.sort(key=lambda x: abs(x["strike"] - lg["strike"]))
                for ds in (0.30, 0.35, 0.40):
                    sh = min(further, key=lambda x: abs(abs(x["delta"]) - ds)) if further else None
                    if sh is None or sh["strike"] == lg["strike"]:
                        continue
                    legs = [(lg, "buy"), (sh, "sell")]
                    lim = limit_for(legs)
                    if lim * 100 <= notional:
                        candidates.append((abs(dte - dte_target) / 7 + (0.55 - dl) * 10 + (ds - 0.30) * 5, legs, lim,
                                           f"{exp} vertical {lg['strike']:g}/{sh['strike']:g}, deltas {lg['delta']}/{sh['delta']}"))
                        break
                if candidates and candidates[-1][3].startswith(exp):
                    break
    if not candidates:
        return [], 0.0, f"nothing affordable under ${notional:.0f} (cheapest structures exceed the intent; raise notional or pick a cheaper underlying)"
    candidates.sort(key=lambda c: c[0])
    _, legs, lim, note = candidates[0]
    return legs, lim, note


def limit_for(legs: list[tuple[dict, str]]) -> float:
    """Marketable-but-not-silly limit: mid plus a quarter of the spread toward the ask (net for verticals)."""
    net = 0.0
    for r, side in legs:
        mid = (r["bid"] + r["ask"]) / 2
        edge = (r["ask"] - r["bid"]) * 0.25
        px = mid + edge if side == "buy" else mid - edge
        net += px if side == "buy" else -px
    return max(0.01, tick_round(net))


class SpreadBook:
    """Open debit verticals tracked by their net value, keyed by the long leg."""
    KEY = "spreads:"

    def __init__(self, journal) -> None:
        self.j = journal

    def all(self) -> dict[str, dict[str, Any]]:
        import json
        rows = self.j.conn.execute("SELECT key, value FROM kv WHERE key LIKE 'spreads:%'").fetchall()
        return {r["key"][len(self.KEY):]: json.loads(r["value"]) for r in rows if r["value"] and r["value"] != "null"}

    def register(self, long_sym: str, short_sym: str, qty: int, entry: float, stop: float, target: float, session_id: int | None) -> dict[str, Any]:
        rec = {"long": long_sym, "short": short_sym, "qty": int(qty), "entry": entry, "stop": stop, "target": target,
               "opened": now_et().isoformat(timespec="seconds"), "session_id": session_id}
        self.j.set(self.KEY + long_sym, rec)
        return rec

    def update(self, long_sym: str, stop: float | None = None, target: float | None = None) -> bool:
        rec = self.j.get(self.KEY + long_sym)
        if not rec:
            return False
        if stop is not None:
            rec["stop"] = stop
        if target is not None:
            rec["target"] = target
        self.j.set(self.KEY + long_sym, rec)
        return True

    def clear(self, long_sym: str) -> None:
        self.j.set(self.KEY + long_sym, None)

    def describe(self, values: dict[str, float] | None = None) -> str:
        recs = self.all()
        if not recs:
            return "(no spreads)"
        out = []
        for k, r in recs.items():
            v = (values or {}).get(k)
            out.append(f"- {occ_human(r['long'])} / {occ_human(r['short'])} x{r['qty']}: entry {r['entry']} stop {r['stop']} target {r['target']}"
                       + (f" | now {v} ({(v / r['entry'] - 1) * 100:+.0f}%)" if v else ""))
        return "\n".join(out)


def spread_value(market, rec: dict[str, Any]) -> float | None:
    snaps = {s["symbol"]: s for s in market.option_snapshots([rec["long"], rec["short"]])}
    lg, sh = snaps.get(rec["long"]), snaps.get(rec["short"])
    if not lg or not sh or lg.get("mid") is None or sh.get("mid") is None:
        return None
    return round(lg["mid"] - sh["mid"], 2)


def close_spread_now(broker, journal, rec: dict[str, Any], reason: str, value: float | None) -> str:
    legs = [{"symbol": rec["long"], "side": "sell", "ratio_qty": 1, "position_intent": "sell_to_close"},
            {"symbol": rec["short"], "side": "buy", "ratio_qty": 1, "position_intent": "buy_to_close"}]
    credit = max(0.01, tick_round((value or rec["stop"]) * 0.9))
    o = broker.submit_mleg_order(legs, int(rec["qty"]), -credit)
    for l in legs:
        journal.add_decision(rec.get("session_id"), "close", l["symbol"], side=l["side"], qty=rec["qty"], price=credit, order_id=o.get("id"),
                             status=o.get("status", "submitted"), meta={"reason": reason, "spread": True, "value": value}, underlying=parse_occ(l["symbol"])["underlying"])
    return f"spread {occ_human(rec['long'])}/{occ_human(rec['short'])} close submitted at net credit {credit} ({reason}); order {o.get('id')} {o.get('status')}"


def execute_option_entry(rec: dict[str, Any], price: float, risk, broker, market, journal, exits, limits) -> str:
    """Resolve and buy the option expression in `rec` (armed entry) through the risk engine."""
    u = rec["symbol"]
    expr = rec.get("expression", "stock")
    ctype = "call" if expr.startswith("call") else "put"
    today = now_et().date()
    min_dte = max(limits.min_option_dte + 1, 4)
    contracts = broker.option_contracts(u, today + dt.timedelta(days=min_dte), today + dt.timedelta(days=int(rec.get("dte_target", 14)) + 21), ctype, limit=3000)
    oi = {k["symbol"]: k for k in contracts}
    rows = market.option_chain(u, min_dte, int(rec.get("dte_target", 14)) + 21, 15.0, ctype, oi_map=oi)
    legs, lim, note = resolve_affordable(rows, expr, float(rec["notional"]), int(rec.get("dte_target", 14)), min_dte)
    if not legs:
        return f"{u}: could not resolve an affordable option structure ({note}); nothing bought"
    qty = max(1, int(rec["notional"] // (lim * 100)))
    pos = broker.positions()
    st = risk.state(positions=pos)
    grade = rec.get("catalyst_grade")
    acct_level = int(broker.account().get("options_trading_level") or 0)
    if len(legs) == 1:
        sym = legs[0][0]["symbol"]
        viol, info = risk.check_option_buy(sym, qty, lim, st, pos, acct_level, grade)
        if viol:
            return f"{u}: {occ_human(sym)} blocked by the risk engine: {'; '.join(viol)}"
        o = broker.submit_option_order(sym, "buy", qty, lim, "buy_to_open")
    else:
        norm = [{"symbol": r["symbol"], "side": side, "ratio_qty": 1, "position_intent": "buy_to_open" if side == "buy" else "sell_to_open"} for r, side in legs]
        viol, infos = risk.check_mleg(norm, qty, lim, st, pos, acct_level, grade)
        if viol:
            return f"{u}: vertical blocked by the risk engine: {'; '.join(viol)}"
        o = broker.submit_mleg_order(norm, qty, lim)
    for _ in range(6):
        time.sleep(1.0)
        o = broker.order(o["id"])
        if o.get("status") in ("filled", "canceled", "rejected", "expired"):
            break
    fill = float(o.get("filled_avg_price") or lim)
    stop_pct, tgt_pct = float(rec.get("opt_stop_pct", 0.5)), float(rec.get("opt_target_pct", 1.0))
    stop_prem, tgt_prem = round(fill * (1 - stop_pct), 2), round(fill * (1 + tgt_pct), 2)
    label = " / ".join(f"{side} {occ_human(r['symbol'])}" for r, side in legs)
    for r, side in legs:
        journal.add_decision(rec.get("session_id"), "open", r["symbol"], side=side, qty=qty, price=fill, thesis=rec["thesis"], target=str(tgt_prem),
                             stop=str(stop_prem), horizon=rec.get("horizon"), order_id=o.get("id"), status=o.get("status", "submitted"),
                             meta={"conditional": True, "expression": expr, "catalyst_grade": grade, "legs": label, "stop_price": stop_prem, "target_price": tgt_prem},
                             underlying=u)
    if o.get("status") != "filled":
        return f"{u}: {expr} order {o.get('id')} is {o.get('status')} at limit {lim} ({label}); not filled yet, watcher will report the fill"
    if len(legs) == 1:
        exits.register(legs[0][0]["symbol"], stop_prem, tgt_prem, rec.get("session_id"))
        try:
            exits.ensure()
        except Exception as e:
            log.warning("option stop arming failed: %s", e)
    else:
        SpreadBook(journal).register(legs[0][0]["symbol"], legs[1][0]["symbol"], qty, fill, stop_prem, tgt_prem, rec.get("session_id"))
    return f"{u}: OPTION ENTRY EXECUTED {qty}x {label} at {fill} ({note}); stop {stop_prem} target {tgt_prem} (premium), cost ${fill*100*qty:.0f}"
