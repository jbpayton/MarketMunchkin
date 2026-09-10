"""Contingent exits: a resting protective stop on every position, plus registered targets.

Alpaca constraints (verified on this account): fractional quantities allow only simple DAY
orders (stop / stop-limit OK), whole-share quantities allow GTC stops; options are DAY only.
So stops are GTC where possible and re-armed each morning otherwise. Targets are not
resting orders (a second sell order would tie up the same shares); the watcher wakes the
agent when a target trades.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .broker import Broker
from .journal import Journal
from .market import Market
import datetime as dt

from .util import ET, is_option, now_et, occ_human, parse_occ

log = logging.getLogger("munchkin.exits")

STOP_TYPES = ("stop", "stop_limit")


def _is_whole(qty: float) -> bool:
    return abs(qty - round(qty)) < 1e-9


class ExitManager:
    def __init__(self, broker: Broker, market: Market, journal: Journal) -> None:
        self.b, self.m, self.j = broker, market, journal

    # ------------------------------------------------------------------ registry
    def register(self, symbol: str, stop_price: float | None, target_price: float | None,
                 session_id: int | None = None) -> dict[str, Any]:
        return self.j.set_exits(symbol, stop_price=stop_price, target_price=target_price,
                                kind="option" if is_option(symbol) else "stock", session_id=session_id)

    def update(self, symbol: str, stop_price: float | None = None, target_price: float | None = None,
               allow_lower_stop: bool = False) -> tuple[bool, str]:
        cur = self.j.exits(symbol) or {}
        old_stop = cur.get("stop_price")
        if stop_price is not None and old_stop is not None and stop_price < float(old_stop) - 1e-9 and not allow_lower_stop:
            return False, f"refusing to LOWER the stop on {occ_human(symbol)} from {old_stop} to {stop_price}; stops only move up"
        self.j.set_exits(symbol, stop_price=stop_price, target_price=target_price, stop_order_id=None if stop_price is not None else cur.get("stop_order_id"))
        return True, "levels updated"

    # ------------------------------------------------------------------ orders
    def stop_orders_for(self, symbol: str, open_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [o for o in open_orders if o.get("symbol") == symbol.upper() and str(o.get("side")) == "sell"
                and str(o.get("type")) in STOP_TYPES]

    def cancel_exit_orders(self, symbol: str, open_orders: list[dict[str, Any]] | None = None, wait: float = 1.5) -> int:
        oo = open_orders if open_orders is not None else self.b.open_orders()
        n = 0
        for o in [x for x in oo if x.get("symbol") == symbol.upper() and str(x.get("side")) == "sell"]:
            try:
                self.b.cancel_order(o["id"])
                n += 1
            except Exception as e:
                log.warning("cancel exit %s: %s", o["id"], e)
        if n:
            time.sleep(wait)
        return n

    def place_stop(self, symbol: str, qty: float, stop_price: float) -> dict[str, Any] | None:
        opt = is_option(symbol)
        tif = "gtc" if (not opt and _is_whole(qty)) else "day"
        try:
            o = self.b.submit_stop_order(symbol, qty if not opt else int(qty), stop_price, tif=tif, is_option=opt,
                                         client_order_id=f"mmstop-{symbol[:12]}-{int(time.time())}")
        except Exception as e:
            log.warning("stop placement failed for %s: %s", symbol, e)
            self.j.set_exits(symbol, stop_error=str(e)[:160])
            return None
        self.j.set_exits(symbol, stop_order_id=o.get("id"), stop_tif=tif, stop_error=None)
        self.j.add_decision(None, "exit_order", symbol, side="sell", qty=qty, price=stop_price, order_id=o.get("id"),
                            status=o.get("status", "submitted"), meta={"tif": tif, "stop_price": stop_price},
                            underlying=(parse_occ(symbol) or {}).get("underlying", symbol))
        return o

    def ensure(self, positions: list[dict[str, Any]] | None = None, open_orders: list[dict[str, Any]] | None = None) -> list[str]:
        """Make sure every long position with a registered stop has a live stop order of the right size."""
        positions = self.b.positions() if positions is None else positions
        open_orders = self.b.open_orders() if open_orders is None else open_orders
        actions: list[str] = []
        held = {p["symbol"] for p in positions}
        # drop registry entries for closed positions
        for sym in list(self.j.all_exits().keys()):
            if sym not in held:
                self.j.clear_exits(sym)
        for p in positions:
            sym = p["symbol"]
            qty = float(p["qty"])
            if qty <= 0:
                continue
            lv = self.j.exits(sym)
            if not lv or lv.get("stop_price") is None:
                actions.append(f"{occ_human(sym)}: no stop registered (agent must set one via set_exit_levels)")
                continue
            stop = float(lv["stop_price"])
            existing = self.stop_orders_for(sym, open_orders)
            ok = [o for o in existing if abs(float(o.get("qty") or 0) - qty) < 1e-6 and abs(float(o.get("stop_price") or 0) - stop) < 0.005]
            if ok:
                continue
            if existing:
                for o in existing:
                    try:
                        self.b.cancel_order(o["id"])
                    except Exception as e:
                        log.warning("cancel stale stop %s: %s", o["id"], e)
                time.sleep(1.5)
            # avoid firing a stop that is already through: leave it to the agent if price is at/below the stop
            try:
                px = self.m.price(parse_occ(sym)["underlying"]) if is_option(sym) else self.m.price(sym)
            except Exception:
                px = None
            if not is_option(sym) and px is not None and px <= stop:
                actions.append(f"{sym}: price {px} already at/below stop {stop}; not placing a stop order (agent must act)")
                continue
            o = self.place_stop(sym, qty, stop)
            if o:
                actions.append(f"{occ_human(sym)}: placed {o.get('time_in_force')} stop {qty:g} @ {stop} (order {o.get('id')})")
            else:
                actions.append(f"{occ_human(sym)}: stop placement FAILED ({(self.j.exits(sym) or {}).get('stop_error')})")
        return actions

    def describe(self, symbol: str, open_orders: list[dict[str, Any]]) -> dict[str, Any]:
        lv = self.j.exits(symbol) or {}
        stops = self.stop_orders_for(symbol, open_orders)
        return {"stop_level": lv.get("stop_price"), "target_level": lv.get("target_price"),
                "resting_stop": ", ".join(f"{o.get('type')} {o.get('qty')}@{o.get('stop_price')} {o.get('time_in_force')} {o.get('status')}" for o in stops) or "NONE",
                "stop_error": lv.get("stop_error")}


# ------------------------------------------------------------------ expiration guard
def expiry_plan(positions: list[dict[str, Any]], now: dt.datetime, wake_dte: int = 1, force_close_time: str = "14:30",
                dne_time: str = "15:45", assignment_delta: float = 0.85, deltas: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Pure decision logic. Returns actions: wake | force_close | dne | assignment_risk."""
    out: list[dict[str, Any]] = []
    today = now.date()
    fh, fm = (int(x) for x in force_close_time.split(":"))
    dh, dm = (int(x) for x in dne_time.split(":"))
    t = now.time()
    by_exp_short: dict[tuple[str, dt.date], bool] = {}
    parsed = []
    for p in positions:
        pp = parse_occ(p["symbol"])
        if not pp:
            continue
        qty = float(p["qty"])
        parsed.append((p, pp, qty))
        if qty < 0:
            by_exp_short[(pp["underlying"], pp["expiration"])] = True
    for p, pp, qty in parsed:
        sym = p["symbol"]
        dte = (pp["expiration"] - today).days
        if dte < 0:
            continue
        if dte == 0:
            if t >= dt.time(fh, fm):
                out.append({"action": "force_close", "symbol": sym, "qty": qty, "dte": 0,
                            "reason": f"{occ_human(sym)} expires today; forced close after {force_close_time} ET"})
            elif t >= dt.time(9, 30):
                out.append({"action": "wake", "symbol": sym, "dte": 0, "reason": f"{occ_human(sym)} EXPIRES TODAY: close it before {force_close_time} ET or the watcher will"})
            if qty > 0 and t >= dt.time(dh, dm) and not by_exp_short.get((pp["underlying"], pp["expiration"])):
                out.append({"action": "dne", "symbol": sym, "reason": f"{occ_human(sym)} still held near the close on expiration day; filing do-not-exercise"})
        elif dte <= wake_dte:
            out.append({"action": "wake", "symbol": sym, "dte": dte, "reason": f"{occ_human(sym)} has {dte} day(s) to expiry: exit or roll today (no positions are held into expiration day)"})
        if qty < 0 and deltas and abs(deltas.get(sym, 0.0)) >= assignment_delta:
            out.append({"action": "assignment_risk", "symbol": sym, "reason": f"short leg {occ_human(sym)} is deep ITM (|delta| {abs(deltas[sym]):.2f}): early assignment risk; close or roll the spread"})
    return out


def force_close_option(x: "ExitManager", symbol: str, qty: float) -> str:
    """Close a single option position at a marketable limit (bid for longs, ask for shorts)."""
    snap = x.m.option_snapshots([symbol])[0]
    bid, ask = snap.get("bid"), snap.get("ask")
    x.cancel_exit_orders(symbol)
    if qty > 0:
        px = bid if bid else (snap.get("mid") or 0.01)
        o = x.b.submit_option_order(symbol, "sell", int(qty), max(0.01, float(px)), "sell_to_close")
    else:
        px = ask if ask else (snap.get("mid") or 0.05)
        o = x.b.submit_option_order(symbol, "buy", int(abs(qty)), float(px), "buy_to_close")
    x.j.add_decision(None, "close", symbol, side="sell" if qty > 0 else "buy", qty=abs(qty), price=px, order_id=o.get("id"),
                     status=o.get("status", "submitted"), meta={"reason": "expiration guard: forced close", "forced": True},
                     underlying=parse_occ(symbol)["underlying"])
    if qty > 0:
        x.j.clear_exits(symbol)
    return f"{occ_human(symbol)}: forced close {abs(qty):g} @ {px} (order {o.get('id')}, {o.get('status')})"
