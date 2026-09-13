"""Hard risk guardrails. Everything here runs in code; the LLM cannot override it.

Rules enforced:
  * Cash only. Buying power is settled cash (T+1), never margin, never unsettled proceeds.
  * Virtual account: usable cash/equity is offset so the agent only ever "sees" the
    configured starting capital plus its own P&L, even if the broker account is larger.
  * PDT: at most `max_day_trades_5d` round trips within 5 business days.
  * Position count / size limits, options premium cap, daily-loss circuit breaker.
  * Options: long premium or defined-risk debit spreads only; limit orders; liquidity and DTE filters.
  * HALT file blocks all opening orders.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .broker import Broker
from .config import HALT_FILE, RiskLimits
from .journal import Journal
from .market import Market
from .util import ET, is_option, now_et, occ_human, parse_occ

log = logging.getLogger("munchkin.risk")


@dataclass
class RiskState:
    date: str
    market_open: bool
    halted: bool
    equity: float
    cash: float
    unsettled_proceeds: float
    open_buy_orders_notional: float
    cash_offset: float
    virtual_equity: float
    virtual_cash: float
    virtual_settled_cash: float
    buying_power_now: float
    day_start_equity: float
    daily_pnl: float
    daily_pnl_pct: float
    daily_loss_breached: bool
    day_trades_used_5d: int
    day_trades_remaining: int
    day_trades_list: list[dict] = field(default_factory=list)
    positions_count: int = 0
    options_premium_at_risk: float = 0.0
    max_position_notional: float = 0.0
    max_new_options_premium: float = 0.0
    restrictions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RiskEngine:
    def __init__(self, broker: Broker, market: Market, journal: Journal, limits: RiskLimits) -> None:
        self.b = broker
        self.m = market
        self.j = journal
        self.L = limits

    # ------------------------------------------------------------------ bookkeeping
    def sync_fills(self) -> list[dict[str, Any]]:
        last = self.j.latest_fill_ts()
        after = None
        if last:
            after = dt.datetime.fromisoformat(last.replace("Z", "+00:00")) - dt.timedelta(days=1)
        fills = self.b.fills(after=after)
        self.j.upsert_fills(fills)
        return self.j.rebuild_trades()

    def _ensure_baseline(self, acct: dict[str, Any]) -> float:
        base = self.j.get("baseline")
        if base is None:
            base = {"cash": float(acct["cash"]), "equity": float(acct["equity"]),
                    "date": now_et().isoformat(timespec="seconds"), "starting_capital": self.L.starting_capital}
            self.j.set("baseline", base)
        return max(0.0, float(base["equity"]) - float(base.get("starting_capital", self.L.starting_capital)))

    def reset_baseline(self) -> dict[str, Any]:
        acct = self.b.account()
        self.j.set("baseline", None)
        self._ensure_baseline(acct)
        return self.j.get("baseline")

    def _day_start_equity(self, acct: dict[str, Any]) -> float:
        key = f"day_start_equity:{now_et().date().isoformat()}"
        v = self.j.get(key)
        if v is None:
            v = float(acct.get("last_equity") or acct["equity"])
            self.j.set(key, v)
        return float(v)

    # ------------------------------------------------------------------ state
    def state(self, positions: list[dict[str, Any]] | None = None, open_orders: list[dict[str, Any]] | None = None) -> RiskState:
        acct = self.b.account()
        clock = self.b.clock()
        positions = self.b.positions() if positions is None else positions
        open_orders = self.b.open_orders() if open_orders is None else open_orders
        equity = float(acct["equity"])
        cash = float(acct["cash"])
        offset = self._ensure_baseline(acct)
        unsettled = self.j.unsettled_proceeds(self.L.settlement_days) if self.L.require_settled_cash else 0.0
        pending = 0.0
        for o in open_orders:
            if str(o.get("side")) == "buy":
                if o.get("notional"):
                    pending += float(o["notional"])
                elif o.get("limit_price") and o.get("qty"):
                    mult = self.multiplier(o["symbol"]) if is_option(o["symbol"]) else 1.0
                    pending += float(o["limit_price"]) * float(o["qty"]) * mult
            elif o.get("order_class") == "mleg" and o.get("limit_price") and o.get("qty"):
                pending += abs(float(o["limit_price"])) * float(o["qty"]) * self.multiplier(o["symbol"])
        v_equity = equity - offset
        v_cash = cash - offset
        v_settled = max(0.0, min(v_cash, cash - unsettled - offset) - pending)
        day_start = self._day_start_equity(acct) - offset
        daily_pnl = v_equity - day_start
        daily_pnl_pct = (daily_pnl / day_start * 100) if day_start else 0.0
        breached = daily_pnl_pct <= -self.L.max_daily_loss_pct * 100
        dts = self.j.day_trades(5)
        opt_premium = sum(float(p["cost_basis"]) for p in positions if is_option(p["symbol"]))
        restrictions: list[str] = []
        halted = HALT_FILE.exists()
        if halted:
            restrictions.append("HALT file present: no new entries until the operator removes it")
        if breached:
            restrictions.append(f"daily loss limit hit ({daily_pnl_pct:.1f}% <= -{self.L.max_daily_loss_pct*100:.0f}%): no new entries today, exits only")
        if self.L.enforce_pdt and len(dts) >= self.L.max_day_trades_5d:
            restrictions.append("day-trade budget exhausted: cannot close anything bought today")
        if len(positions) >= self.L.max_positions:
            restrictions.append(f"max positions ({self.L.max_positions}) reached: no new symbols")
        if not clock["is_open"]:
            restrictions.append("market closed: market orders blocked; limit orders queue for the open")
        return RiskState(
            date=now_et().strftime("%Y-%m-%d %H:%M ET"), market_open=bool(clock["is_open"]), halted=halted,
            equity=round(equity, 2), cash=round(cash, 2), unsettled_proceeds=round(unsettled, 2),
            open_buy_orders_notional=round(pending, 2), cash_offset=round(offset, 2),
            virtual_equity=round(v_equity, 2), virtual_cash=round(v_cash, 2), virtual_settled_cash=round(v_settled, 2),
            buying_power_now=round(v_settled, 2), day_start_equity=round(day_start, 2), daily_pnl=round(daily_pnl, 2),
            daily_pnl_pct=round(daily_pnl_pct, 2), daily_loss_breached=breached,
            day_trades_used_5d=len(dts), day_trades_remaining=(999 if not self.L.enforce_pdt else max(0, self.L.max_day_trades_5d - len(dts))),
            day_trades_list=dts, positions_count=len(positions), options_premium_at_risk=round(opt_premium, 2),
            max_position_notional=round(self.L.max_position_pct * v_equity, 2),
            max_new_options_premium=round(max(0.0, self.L.max_options_pct * v_equity - opt_premium), 2),
            restrictions=restrictions,
        )

    # ------------------------------------------------------------------ helpers
    def grade_multiplier(self, grade: str | None) -> float:
        return {"speculative": self.L.size_mult_speculative, "none": self.L.size_mult_no_catalyst}.get(grade or "confirmed", 1.0)

    _mult_cache: dict[str, float] = {}

    def multiplier(self, symbol: str) -> float:
        """Contract multiplier from the contract record (adjusted contracts are not 100). Cached per symbol."""
        if symbol in RiskEngine._mult_cache:
            return RiskEngine._mult_cache[symbol]
        m = 100.0
        try:
            c = self.b.option_contract(symbol)
            m = float(c.get("size") or c.get("multiplier") or 100.0)
        except Exception:
            pass
        RiskEngine._mult_cache[symbol] = m
        return m

    def _entry_common(self, st: RiskState, cost: float, symbol: str, positions: list[dict], grade: str | None = None) -> list[str]:
        v: list[str] = []
        cap = st.max_position_notional * self.grade_multiplier(grade)
        cap_note = "" if self.grade_multiplier(grade) == 1.0 else f" (catalyst grade '{grade}' scales the cap by {self.grade_multiplier(grade):.2f})"
        if st.halted:
            v.append("HALT file present; opening orders disabled")
        if st.daily_loss_breached:
            v.append("daily loss limit breached; no new entries today")
        held = {p["symbol"] for p in positions}
        if symbol not in held and st.positions_count >= self.L.max_positions:
            v.append(f"max positions ({self.L.max_positions}) reached")
        held_cash = 0.0
        try:
            held_cash = self.j.reserved_total()
        except Exception:
            pass
        if cost > st.virtual_settled_cash - held_cash + 0.01:
            v.append(f"cost ${cost:.2f} exceeds settled buying power ${st.virtual_settled_cash:.2f}" + (f" less ${held_cash:.2f} held by orders in flight" if held_cash else "") + " (cash-only, T+1 settlement)")
        root = parse_occ(symbol)["underlying"] if parse_occ(symbol) else symbol
        existing = sum(float(p["cost_basis"]) for p in positions
                       if (parse_occ(p["symbol"])["underlying"] if parse_occ(p["symbol"]) else p["symbol"]) == root)
        if cost + existing > cap + 0.01:
            v.append(f"exposure to {root} would be ${cost + existing:.2f} (stock + options combined) > cap ${cap:.2f}{cap_note}")
        return v

    def avg_dollar_volume(self, symbol: str) -> float | None:
        try:
            df = self.m.bars(symbol, "1D", 25)
            if df.empty:
                return None
            return float((df["close"] * df["volume"]).tail(20).mean())
        except Exception:
            return None

    # ------------------------------------------------------------------ stock checks
    def check_stock_buy(self, symbol: str, notional: float, price: float | None, order_type: str,
                        limit_price: float | None, st: RiskState, positions: list[dict], grade: str | None = None) -> list[str]:
        v = self._entry_common(st, notional, symbol, positions, grade)
        if not st.market_open and order_type == "market":
            v.append("market is closed; market orders are blocked (use a limit order or wait)")
        if price is None:
            v.append("no live price available for symbol")
        else:
            if price < self.L.min_stock_price:
                v.append(f"price ${price:.2f} below minimum ${self.L.min_stock_price:.2f}")
            if order_type == "limit" and limit_price is not None:
                dev = abs(limit_price / price - 1)
                if dev > self.L.max_limit_deviation_pct:
                    v.append(f"limit ${limit_price:.2f} deviates {dev*100:.1f}% from last ${price:.2f} (max {self.L.max_limit_deviation_pct*100:.0f}%)")
        adv = self.avg_dollar_volume(symbol)
        if adv is not None and adv < self.L.min_avg_dollar_volume:
            v.append(f"avg dollar volume ${adv/1e6:.1f}M below minimum ${self.L.min_avg_dollar_volume/1e6:.0f}M (illiquid)")
        try:
            a = self.b.asset(symbol)
            if not a.get("tradable"):
                v.append("asset not tradable on Alpaca")
            if a.get("status") != "active":
                v.append(f"asset status {a.get('status')}")
        except Exception as e:
            v.append(f"unknown asset {symbol}: {str(e)[:80]}")
        return v

    def check_sell(self, symbol: str, qty: float, st: RiskState, positions: list[dict]) -> list[str]:
        v: list[str] = []
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if pos is None:
            v.append(f"no open position in {symbol}")
            return v
        avail = float(pos.get("qty_available") or pos["qty"])
        if qty > avail + 1e-9:
            v.append(f"qty {qty} exceeds available {avail} (some may be tied up in open orders)")
        if self.L.enforce_pdt and self.j.bought_today(symbol):
            if st.day_trades_remaining <= 0:
                v.append(f"selling {symbol} today would be a day trade and the 5-day budget ({self.L.max_day_trades_5d}) is used up")
        return v

    # ------------------------------------------------------------------ option checks
    def _contract_info(self, symbol: str) -> dict[str, Any]:
        info: dict[str, Any] = {"symbol": symbol}
        try:
            c = self.b.option_contract(symbol)
            info["oi"] = int(float(c.get("open_interest") or 0))
            info["tradable"] = bool(c.get("tradable"))
        except Exception as e:
            info["error"] = f"contract lookup failed: {str(e)[:100]}"
        try:
            snap = self.m.option_snapshots([symbol])[0]
            info.update({k: snap.get(k) for k in ("bid", "ask", "mid", "spread_pct", "dte", "delta", "iv")})
        except Exception as e:
            info["snap_error"] = str(e)[:100]
        return info

    def check_option_leg_quality(self, symbol: str, info: dict[str, Any]) -> list[str]:
        v: list[str] = []
        p = parse_occ(symbol)
        if not p:
            return [f"{symbol} is not a valid OCC option symbol"]
        if "error" in info:
            v.append(info["error"])
        if info.get("tradable") is False:
            v.append(f"{occ_human(symbol)} not tradable")
        dte = info.get("dte")
        if dte is not None and (dte < self.L.min_option_dte or dte > self.L.max_option_dte):
            v.append(f"{occ_human(symbol)} DTE {dte} outside [{self.L.min_option_dte}, {self.L.max_option_dte}]")
        if info.get("oi") is not None and info["oi"] < self.L.min_option_open_interest:
            v.append(f"{occ_human(symbol)} open interest {info['oi']} < {self.L.min_option_open_interest} (illiquid)")
        sp = info.get("spread_pct")
        if sp is not None and sp > self.L.max_option_spread_pct * 100:
            v.append(f"{occ_human(symbol)} bid/ask spread {sp:.0f}% of mid > {self.L.max_option_spread_pct*100:.0f}% (too wide)")
        if info.get("bid") is None or info.get("ask") is None:
            v.append(f"{occ_human(symbol)} has no live quote")
        return v

    def check_option_buy(self, symbol: str, qty: int, limit_price: float | None, st: RiskState,
                         positions: list[dict], acct_level: int, grade: str | None = None) -> tuple[list[str], dict[str, Any]]:
        info = self._contract_info(symbol)
        v = self.check_option_leg_quality(symbol, info)
        if not self.L.allow_options or not self.L.allow_singles:
            v.append("the active trading style does not allow buying single options (Defensive = stock only)")
        if acct_level < 2:
            v.append(f"account options level {acct_level} < 2 required for long options")
        if limit_price is None:
            if not self.L.allow_market_orders_options:
                v.append("options require a limit order (market orders disabled)")
            cost = float(info.get("ask") or 0) * self.multiplier(symbol) * qty
        else:
            cost = limit_price * self.multiplier(symbol) * qty
            ask = info.get("ask")
            if ask and limit_price > ask * 1.03 + 0.01:
                v.append(f"limit ${limit_price:.2f} is more than 3% above ask ${ask:.2f}; do not overpay")
        if not st.market_open:
            v.append("market is closed; option orders are day-only and will be rejected or sit until the open")
        v += self._entry_common(st, cost, symbol, positions, grade)
        if cost > st.max_new_options_premium + 0.01:
            v.append(f"premium ${cost:.2f} exceeds remaining options budget ${st.max_new_options_premium:.2f} ({self.L.max_options_pct*100:.0f}% of equity cap)")
        info["cost"] = round(cost, 2)
        return v, info

    def check_mleg(self, legs: list[dict[str, Any]], qty: int, limit_price: float, st: RiskState,
                   positions: list[dict], acct_level: int, grade: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
        v: list[str] = []
        if not self.L.allow_options or not self.L.allow_spreads:
            v.append("the active trading style does not allow spreads")
        if acct_level < 3:
            v.append(f"account options level {acct_level} < 3 required for spreads")
        if limit_price is None or limit_price <= 0:
            v.append("spreads must be net DEBIT (limit_price > 0); credit/naked structures are not allowed in a cash account")
        parsed = []
        for l in legs:
            p = parse_occ(l["symbol"])
            if not p:
                v.append(f"bad option symbol {l['symbol']}")
                continue
            if int(l.get("ratio_qty", 1)) != 1:
                v.append("only 1:1 ratio legs are allowed")
            parsed.append({**p, **l})
        if len({p["underlying"] for p in parsed}) > 1:
            v.append("all legs must share one underlying")
        if len(parsed) < 2 or len(parsed) > 4:
            v.append("spreads need 2-4 legs")
        # every short leg must be covered by a long leg of same type expiring no earlier
        longs = [p for p in parsed if p["side"] == "buy"]
        for s in [p for p in parsed if p["side"] == "sell"]:
            cover = [l for l in longs if l["type"] == s["type"] and l["expiration"] >= s["expiration"]]
            if not cover:
                v.append(f"short leg {occ_human(s['symbol'])} is not covered by a long leg of the same type (undefined risk)")
            if s.get("position_intent") != "sell_to_open":
                v.append("short legs in a new spread must be sell_to_open")
        for l in longs:
            if l.get("position_intent") != "buy_to_open":
                v.append("long legs in a new spread must be buy_to_open")
        infos = []
        for p in parsed:
            info = self._contract_info(p["symbol"])
            v += self.check_option_leg_quality(p["symbol"], info)
            infos.append(info)
        # sanity: net debit vs quotes
        est = 0.0
        for p, info in zip(parsed, infos):
            mid = info.get("mid") or 0
            est += mid if p["side"] == "buy" else -mid
        if limit_price and est and limit_price > est * 1.15 + 0.05:
            v.append(f"net debit ${limit_price:.2f} is well above the mid-based estimate ${est:.2f}; tighten the price")
        cost = (limit_price or 0) * (self.multiplier(legs[0]["symbol"]) if legs else 100.0) * qty
        if not st.market_open:
            v.append("market is closed; option orders are day-only")
        v += self._entry_common(st, cost, parsed[0]["underlying"] if parsed else "?", positions, grade)
        if cost > st.max_new_options_premium + 0.01:
            v.append(f"net debit ${cost:.2f} exceeds remaining options budget ${st.max_new_options_premium:.2f}")
        return v, infos
