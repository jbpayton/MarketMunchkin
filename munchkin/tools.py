"""Tools exposed to the LLM. Every tool returns a compact string.

Order tools route through RiskEngine before touching the broker and journal every
decision (including rejected ones). Secrets never pass through here: the tool
layer has no file, shell, or config access.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from . import analytics as A
from . import macro as M
from . import screener as scr
from .broker import Broker
from .config import SETTINGS, Settings, redact
from .entries import EntryBook
from .optentry import EXPRESSIONS, SpreadBook, close_spread_now, spread_value
from . import sandbox as SB
from .exits import ExitManager
from .journal import Journal
from .market import Market
from .research import CATALYST_GRADES, ResearchTracker, normalize_grade
from .risk import RiskEngine, RiskState
from .search import fetch_page, web_search
from .util import fnum, is_option, md_table, now_et, occ_human, parse_occ

log = logging.getLogger("munchkin.tools")


@dataclass
class Context:
    broker: Broker
    market: Market
    journal: Journal
    risk: RiskEngine
    settings: Settings
    session_id: int | None = None
    dry_run: bool = False
    allow_trading: bool = True
    memory_writes: bool = True     # plan / brief / lessons / playbook / tasks (false for read-only chat)
    phase: str = "adhoc"
    plan_set: bool = False
    research: ResearchTracker = field(default_factory=ResearchTracker)
    exits: ExitManager | None = None
    style: str = "balanced"


def _p(name: str, typ: str, desc: str, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"type": typ, "description": desc}
    d.update(extra)
    return d


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


class ToolRegistry:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._tools: dict[str, tuple[Callable[..., str], dict[str, Any]]] = {}
        self._register_all()

    def add(self, name: str, desc: str, params: dict[str, Any], fn: Callable[..., str]) -> None:
        self._tools[name] = (fn, {"type": "function", "function": {"name": name, "description": desc, "parameters": params}})

    # tools whose outputs are worth more context than the default cap
    LIMITS = {"get_market_context": 14000, "get_setups": 12000, "get_intraday_setups": 9000, "get_setup_stats": 6000,
              "screen_stocks": 7000, "screen_intraday": 7000, "get_option_chain": 7000, "get_journal": 6000, "fetch_page": 9000,
              "research_symbol": 15000, "get_macro_release": 6000, "get_economic_calendar": 6000, "get_macro_data": 5000,
              "run_analysis": 8000}

    def limit(self, name: str) -> int | None:
        return self.LIMITS.get(name)

    def openai_specs(self) -> list[dict[str, Any]]:
        return [spec for _, spec in self._tools.values()]

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name not in self._tools:
            return f"ERROR: unknown tool {name}. Available: {', '.join(self._tools)}"
        fn, _ = self._tools[name]
        try:
            out = fn(**args)
            return redact(out if isinstance(out, str) else json.dumps(out, default=str))
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:  # never crash the loop on a tool error
            log.warning("tool %s failed: %s\n%s", name, e, traceback.format_exc())
            return redact(f"ERROR in {name}: {type(e).__name__}: {str(e)[:300]}")

    # ------------------------------------------------------------------ helpers
    def _positions(self) -> list[dict[str, Any]]:
        return self.ctx.broker.positions()

    def _acct_level(self) -> int:
        try:
            return int(self.ctx.broker.account().get("options_trading_level") or 0)
        except Exception:
            return 0

    def _await_order(self, order_id: str, tries: int = 4) -> dict[str, Any]:
        o: dict[str, Any] = {}
        for _ in range(tries):
            time.sleep(1.0)
            o = self.ctx.broker.order(order_id)
            if o.get("status") in ("filled", "canceled", "rejected", "expired"):
                break
        return o

    @staticmethod
    def _fmt_order(o: dict[str, Any]) -> str:
        legs = o.get("legs") or []
        base = (f"order {o.get('id')} | {occ_human(o.get('symbol') or '') if o.get('symbol') else 'MLEG'} | {o.get('side')} "
                f"{o.get('qty') or ''}{('$' + str(o.get('notional'))) if o.get('notional') else ''} | {o.get('type')} "
                f"{('@' + str(o.get('limit_price'))) if o.get('limit_price') else ''} | status={o.get('status')} "
                f"filled={o.get('filled_qty')}@{o.get('filled_avg_price')}")
        if legs:
            base += " | legs: " + "; ".join(f"{occ_human(l.get('symbol',''))} {l.get('side')} {l.get('position_intent')} filled={l.get('filled_qty')}@{l.get('filled_avg_price')}" for l in legs)
        return base

    def _journal_order(self, kind: str, symbol: str, side: str, qty: Any, price: Any, order: dict[str, Any] | None,
                       status: str, meta: dict[str, Any] | None = None, **fields: Any) -> None:
        p = parse_occ(symbol)
        self.ctx.journal.add_decision(self.ctx.session_id, kind, symbol, side=side, qty=qty, price=price,
                                      order_id=(order or {}).get("id"), status=status, meta=meta,
                                      underlying=p["underlying"] if p else symbol, **fields)

    def _trading_gate(self) -> str | None:
        if not self.ctx.allow_trading:
            return "ERROR: trading is disabled in this mode (read-only session)."
        return None

    # ------------------------------------------------------------------ registration
    def _register_all(self) -> None:
        c = self.ctx
        L = c.settings.risk
        c.research.min_charts = L.research_min_charts
        if c.exits is None:
            c.exits = ExitManager(c.broker, c.market, c.journal)
        X = c.exits

        def _arm_stop(symbol: str, stop_price: float | None, target_price: float | None) -> str:
            """Register levels and rest a protective stop right after an entry fills."""
            X.register(symbol, stop_price, target_price, c.session_id)
            if stop_price is None or not c.settings.watch.auto_stops:
                return ""
            try:
                acts = X.ensure()
            except Exception as e:
                return f" | stop placement error: {str(e)[:100]}"
            mine = [a for a in acts if a.startswith(occ_human(symbol)) or a.startswith(symbol)]
            return (" | " + "; ".join(mine)) if mine else " | stop registered (armed by the watcher when the fill lands)"

        # ---------------- account / positions
        def get_account() -> str:
            st = c.risk.state()
            stats = c.journal.trade_stats()
            d = st.as_dict()
            lines = [
                f"time: {d['date']} | market_open: {d['market_open']} | halted: {d['halted']}",
                f"virtual_equity: ${d['virtual_equity']:.2f} | settled buying power now: ${d['buying_power_now']:.2f} | cash: ${d['virtual_cash']:.2f} (unsettled ${d['unsettled_proceeds']:.2f} = today's sale proceeds, usable next business day; pending buy orders ${d['open_buy_orders_notional']:.2f})",
                f"day P&L: ${d['daily_pnl']:.2f} ({d['daily_pnl_pct']:+.2f}%) from day-start ${d['day_start_equity']:.2f} | daily loss breaker: {'TRIPPED' if d['daily_loss_breached'] else 'ok'}",
                (f"day trades used (5d): {d['day_trades_used_5d']}/{L.max_day_trades_5d} remaining {d['day_trades_remaining']} {d['day_trades_list'] or ''}" if L.enforce_pdt
                 else f"day trades (5d): {d['day_trades_used_5d']} (no cap; cash account, settled-funds rule applies) {d['day_trades_list'] or ''}"),
                f"positions: {d['positions_count']}/{L.max_positions} | max per-position notional: ${d['max_position_notional']:.2f} | options premium at risk: ${d['options_premium_at_risk']:.2f}, remaining options budget: ${d['max_new_options_premium']:.2f}",
                f"restrictions: {'; '.join(d['restrictions']) or 'none'}",
                f"closed-trade stats: {stats}",
            ]
            return "\n".join(lines)

        self.add("get_account", "Account, buying power, risk limits status, day-trade budget, P&L today, closed-trade statistics.", _schema({}), get_account)

        def get_positions() -> str:
            pos = self._positions()
            if not pos:
                return "No open positions."
            rows = []
            oo = c.broker.open_orders()
            for p in pos:
                sym = p["symbol"]
                th = c.journal.thesis_for(sym)
                ex = X.describe(sym, oo)
                rows.append({
                    "symbol": occ_human(sym) if is_option(sym) else sym, "occ": sym if is_option(sym) else "",
                    "stop_level": ex["stop_level"], "target_level": ex["target_level"], "resting_stop": ex["resting_stop"],
                    "qty": fnum(p["qty"], 4), "avg_entry": fnum(p["avg_entry_price"], 3), "last": fnum(p["current_price"], 3),
                    "mkt_value": fnum(p["market_value"]), "unreal_pnl": fnum(p["unrealized_pl"]),
                    "unreal_pct": fnum(float(p["unrealized_plpc"]) * 100, 1), "today_pct": fnum(float(p["change_today"]) * 100, 1),
                    "bought_today": c.journal.bought_today(sym),
                    "target": (th or {}).get("target"), "stop": (th or {}).get("stop"), "horizon": (th or {}).get("horizon"),
                    "thesis": ((th or {}).get("thesis") or "")[:140],
                })
            out = md_table(rows)
            sb = SpreadBook(c.journal)
            if sb.all():
                vals = {}
                for k, r in sb.all().items():
                    try:
                        vals[k] = spread_value(c.market, r)
                    except Exception:
                        pass
                out += "\n\nspreads (managed on net value by the watcher):\n" + sb.describe(vals)
            return out

        self.add("get_positions", "Open positions with P&L, the registered stop/target levels, whether a protective stop order is resting at the broker, spread net-value tracking, and the journaled thesis.", _schema({}), get_positions)

        def set_exit_levels(symbol: str, stop_price: float | None = None, target_price: float | None = None, reason: str = "") -> str:
            gate = self._trading_gate()
            if gate:
                return gate
            sym = symbol.upper().replace(" ", "")
            pos = next((p for p in self._positions() if p["symbol"] == sym), None)
            if pos is None:
                return f"ERROR: no position in {occ_human(sym)}"
            sb = SpreadBook(c.journal)
            if sym in sb.all():
                sb.update(sym, stop_price, target_price)
                c.journal.add_decision(c.session_id, "adjust", sym, price=stop_price, target=str(target_price), stop=str(stop_price), status="ok", meta={"reason": reason, "spread": True}, underlying=(parse_occ(sym) or {}).get("underlying", sym))
                return f"spread levels updated (net value): {sb.describe()}"
            ok, msg = X.update(sym, stop_price, target_price)
            if not ok:
                return "REJECTED: " + msg
            c.journal.add_decision(c.session_id, "adjust", sym, price=stop_price, target=str(target_price) if target_price is not None else None,
                                   stop=str(stop_price) if stop_price is not None else None, status="ok", meta={"reason": reason},
                                   underlying=(parse_occ(sym) or {}).get("underlying", sym))
            if c.dry_run:
                return f"DRY RUN: levels updated for {occ_human(sym)} stop={stop_price} target={target_price} (no order changes)"
            acts = []
            if stop_price is not None and c.settings.watch.auto_stops:
                X.cancel_exit_orders(sym)
                acts = X.ensure()
            return f"levels updated for {occ_human(sym)}: {X.describe(sym, c.broker.open_orders())}" + (f" | {'; '.join(acts)}" if acts else "")

        self.add("set_exit_levels", "Move the stop (only upward for longs) and/or target on an open position. The resting stop order is replaced automatically; the daemon watches the target and wakes you when it trades.",
                 _schema({"symbol": _p("symbol", "string", "ticker or OCC option symbol"), "stop_price": _p("stop_price", "number", "new stop (stock price, or option premium per share)"),
                          "target_price": _p("target_price", "number", "new target (stock price, or option premium per share)"), "reason": _p("reason", "string", "why")}, ["symbol"]), set_exit_levels)

        def get_orders() -> str:
            oo = c.broker.open_orders()
            if not oo:
                return "No open orders."
            return "\n".join(self._fmt_order(o) for o in oo)

        self.add("get_orders", "List open (working) orders.", _schema({}), get_orders)

        def cancel_order(order_id: str) -> str:
            gate = self._trading_gate()
            if gate:
                return gate
            if c.dry_run:
                return f"DRY RUN: would cancel {order_id}"
            c.broker.cancel_order(order_id)
            c.journal.update_decision_status(order_id, "canceled")
            return f"cancel requested for {order_id}"

        self.add("cancel_order", "Cancel a working order by id.", _schema({"order_id": _p("order_id", "string", "Alpaca order id")}, ["order_id"]), cancel_order)

        # ---------------- market data
        def get_quotes(symbols: list[str]) -> str:
            snaps = c.market.snapshots(symbols[:20])
            rows = list(snaps.values())
            return md_table(rows, ["symbol", "price", "price_time", "price_src", "bid", "ask", "chg_pct", "day_open", "day_high", "day_low", "day_vol", "prev_close", "error"])

        self.add("get_quotes", "Latest prices/quotes for up to 20 stock/ETF symbols (IEX real-time merged with 15-min delayed SIP; each row shows the timestamp and source).",
                 _schema({"symbols": _p("symbols", "array", "ticker symbols", items={"type": "string"})}, ["symbols"]), get_quotes)

        def get_chart(symbol: str, timeframe: str = "1D", bars: int = 10) -> str:
            symbol = symbol.upper()
            c.research.note_chart(symbol)
            tf = timeframe if timeframe in ("1D", "1W", "1H", "30Min", "15Min", "5Min") else "1D"
            df, s = c.market.bars_with_indicators(symbol, tf, 260 if tf in ("1D", "1W") else 200)
            if df.empty:
                return f"no bars for {symbol}"
            tail = df.tail(max(1, min(bars, 40)))
            rows = [{"t": (idx.tz_convert("America/New_York").strftime("%m-%d %H:%M") if tf not in ("1D", "1W") else idx.strftime("%Y-%m-%d")),
                     "o": fnum(r["open"], 3), "h": fnum(r["high"], 3), "l": fnum(r["low"], 3), "c": fnum(r["close"], 3),
                     "v": int(r["volume"])} for idx, r in tail.iterrows()]
            if tf not in ("1D", "1W"):
                for k in ("hi52_dist_pct", "lo52_dist_pct", "rvol20_pct", "avg_dollar_vol20_m", "sma200_dist_pct"):
                    s.pop(k, None)
                s["note"] = "intraday bars include pre/post-market; indicators are per-bar (not daily)"
            ind = ", ".join(f"{k}={v}" for k, v in s.items())
            live = c.market.snapshots([symbol]).get(symbol, {})
            partial = tf in ("1D", "1W") and rows[-1]["t"] == now_et().date().isoformat() and now_et().time() < dt.time(16, 0)
            through = f"through today's PARTIAL bar {rows[-1]['t']} (session in progress; vol_ratio understated)" if partial else f"through last completed bar {rows[-1]['t']}"
            return (f"{symbol} {tf} indicators ({through}): {ind}\n"
                    f"live: price={live.get('price')} @{live.get('price_time')} ({live.get('price_src')}) chg_today={live.get('chg_pct')}%\n"
                    f"last {len(rows)} bars:\n" + md_table(rows))

        self.add("get_chart", "OHLCV bars plus technical indicators (SMA/EMA distances, RSI, ATR%, MACD, Bollinger position, 20d/52w highs-lows, volume ratio) for one symbol.",
                 _schema({"symbol": _p("symbol", "string", "ticker"),
                          "timeframe": _p("timeframe", "string", "1D (default), 1W, 1H, 30Min, 15Min, 5Min"),
                          "bars": _p("bars", "integer", "how many recent bars to list (default 10, max 40)")}, ["symbol"]), get_chart)

        def screen_stocks(query: str = "", sort_by: str = "", ascending: bool = False, limit: int = 20) -> str:
            c.research.note_screen()
            return scr.query(query, sort_by or None, ascending, limit, market=c.market)

        def get_setups(per_list: int = 5) -> str:
            c.research.note_screen()
            return scr.setups(c.market, per=max(3, min(per_list, 10)))

        self.add("get_setups", "Preset daily scans in one call (breakouts on volume, momentum leaders, pullbacks in uptrend, oversold quality, unusual volume, big losers, squeeze setups, ADX trend pullbacks), each with its BACKTESTED forward-return stats. Use for candidate breadth before picking names.",
                 _schema({"per_list": _p("per_list", "integer", "rows per list (default 4)")}, []), get_setups)

        def screen_intraday(query: str = "", sort_by: str = "", ascending: bool = False, limit: int = 20) -> str:
            c.research.note_screen()
            return scr.intraday_query(query, sort_by or None, ascending, limit, c.market)

        self.add("screen_intraday", "LIVE intraday screener over the universe (today's gap, move from open, range position, VWAP distance, time-of-day relative volume, RS vs SPY today, news count) joined with daily context. pandas query expression. " + scr.INTRADAY_DOC,
                 _schema({"query": _p("query", "string", "e.g. \"rel_vol > 2 and from_hod_pct > -0.5 and avg_dollar_vol20_m > 30\""),
                          "sort_by": _p("sort_by", "string", "column"), "ascending": _p("ascending", "boolean", "default false"),
                          "limit": _p("limit", "integer", "default 20")}, []), screen_intraday)

        def get_intraday_setups(per_list: int = 6) -> str:
            c.research.note_screen()
            return scr.intraday_setups(c.market, per=max(3, min(per_list, 12)))

        self.add("get_intraday_setups", "Preset LIVE scans: gap-and-go, high-of-day breakouts, VWAP-hold strength, gap fades, volume leaders, washout reversals. Market hours only.",
                 _schema({"per_list": _p("per_list", "integer", "rows per list (default 6)")}, []), get_intraday_setups)

        def get_setup_stats() -> str:
            st = scr.load_stats()
            if not st:
                return "no backtest yet (run `munchkin screener backtest`)."
            base = st.get("baseline_all_days")
            lines = [f"setup backtest over {st.get('years')}y, {st.get('symbols')} symbols, generated {st.get('generated')}; baseline all days: {base}"]
            for name, s_ in st.get("setups", {}).items():
                lines.append(A.stats_line(name, s_, base))
                if s_.get("by_year"):
                    lines.append("   by year: " + ", ".join(f"{y}: n={v['n']} 10d {v['fwd10_mean']:+.1f}% win {v['win']}%" for y, v in s_["by_year"].items()))
            return "\n".join(lines)

        self.add("get_setup_stats", "Backtested statistics for each preset daily setup (forward 5/10/20-day returns, win rate, worst decile, by year) vs the all-days baseline. Use to weight setups by evidence.", _schema({}), get_setup_stats)

        def size_position(symbol: str, entry_price: float, stop_price: float | None = None, risk_pct: float | None = None) -> str:
            st = c.risk.state()
            table, _ = scr.load()
            atr = None
            if table is not None and symbol.upper() in table.index:
                atr = table.loc[symbol.upper()].get("atr14_pct")
                atr = None if atr is None or (isinstance(atr, float) and atr != atr) else float(atr)
            rp = risk_pct if risk_pct is not None else 0.05
            rp = max(0.005, min(rp, 0.15))
            out = A.position_size(st.virtual_equity, rp, float(entry_price), stop_price, atr, st.max_position_notional, st.virtual_settled_cash)
            return json.dumps(out)

        self.add("size_position", "Position sizing math: shares/notional so that the loss at the stop equals risk_pct of equity (default 5%), capped by the per-position limit and settled cash; suggests a 1.5-ATR stop if none given and warns when a stop sits inside daily noise.",
                 _schema({"symbol": _p("symbol", "string", "ticker"), "entry_price": _p("entry_price", "number", "planned entry"),
                          "stop_price": _p("stop_price", "number", "planned stop (optional)"), "risk_pct": _p("risk_pct", "number", "fraction of equity to risk, e.g. 0.05")}, ["symbol", "entry_price"]), size_position)

        def get_option_analytics(underlying: str) -> str:
            u = underlying.upper()
            today = now_et().date()
            contracts = c.broker.option_contracts(u, today + dt.timedelta(days=5), today + dt.timedelta(days=60), None, limit=3000)
            oi = {k["symbol"]: k for k in contracts}
            table, _ = scr.load()
            rv = None
            if table is not None and u in table.index:
                v = table.loc[u].get("rvol20_pct")
                rv = None if v is None or (isinstance(v, float) and v != v) else float(v)
            a = c.market.option_analytics(u, oi_map=oi, rv20_pct=rv)
            if "error" in a:
                return a["error"]
            hist = c.journal.record_iv(u, a.get("iv30_pct"), rv)
            a["iv_history"] = hist
            return json.dumps(a)

        self.add("get_option_analytics", "Options read for one underlying: ATM IV (near and ~30 DTE), IV vs 20d realized (rich/fair/cheap), expected move from the ATM straddle, 25-delta skew, put/call OI ratio, OI walls, IV rank over recorded history. Use before any option trade and to judge whether a target is realistic.",
                 _schema({"underlying": _p("underlying", "string", "ticker")}, ["underlying"]), get_option_analytics)

        def _liquidity(symbols: list[str]) -> dict[str, float]:
            """20d avg dollar volume per symbol from one multi-symbol bars request."""
            out: dict[str, float] = {}
            if not symbols:
                return out
            try:
                df = c.market.bars(symbols, "1D", 25)
                if df.empty:
                    return out
                for sym, g in df.groupby(level="symbol"):
                    gg = g.droplevel("symbol").tail(21).iloc[:-1] if len(g) > 1 else g.droplevel("symbol")
                    out[sym] = float((gg["close"] * gg["volume"]).mean()) if len(gg) else 0.0
            except Exception as e:
                log.warning("liquidity lookup failed: %s", e)
            return out

        self.add("screen_stocks", "Filter ~600 liquid US stocks/ETFs on DAILY indicators, cross-sectional momentum ranks, relative strength vs SPY and sector, beta, trend strength, squeeze, earnings distance, using a pandas query expression, e.g. \"mom_score > 80 and rs_sector_20d > 0 and days_to_earnings > 10\" or \"rsi14 < 35 and sma200_dist_pct > 0\". " + scr.COLUMNS_DOC,
                 _schema({"query": _p("query", "string", "pandas query expression over the columns (empty = all)"),
                          "sort_by": _p("sort_by", "string", "column to sort by"),
                          "ascending": _p("ascending", "boolean", "sort ascending (default false)"),
                          "limit": _p("limit", "integer", "max rows (default 20, max 60)")}, []), screen_stocks)

        def get_market_movers(kind: str = "gainers", top: int = 15) -> str:
            top = max(1, min(top, 40))
            min_adv = L.min_avg_dollar_volume
            if kind == "most_active":
                acts = c.market.most_actives(100)
                syms = [a["symbol"] for a in acts][:60]
                snaps = c.market.snapshots(syms)
                cand = [a for a in acts if (snaps.get(a["symbol"], {}).get("price") or 0) >= L.min_stock_price][:60]
                adv = _liquidity([a["symbol"] for a in cand])
                rows = []
                for a in cand:
                    sn = snaps.get(a["symbol"], {})
                    if adv.get(a["symbol"], 0) < min_adv:
                        continue
                    rows.append({"symbol": a["symbol"], "price": sn.get("price"), "chg_pct": sn.get("chg_pct"), "volume": a["volume"],
                                 "avg_dvol_m": round(adv[a["symbol"]] / 1e6, 1)})
                    if len(rows) >= top:
                        break
                return f"most active, tradeable only (price >= ${L.min_stock_price}, 20d avg $vol >= ${min_adv/1e6:.0f}M):\n" + md_table(rows)
            mv = c.market.movers(50)
            cand = [r for r in mv.get(kind, []) if (r.get("price") or 0) >= L.min_stock_price]
            adv = _liquidity([r["symbol"] for r in cand])
            rows = []
            for r in cand:
                if adv.get(r["symbol"], 0) < min_adv:
                    continue
                r = dict(r)
                r["avg_dvol_m"] = round(adv[r["symbol"]] / 1e6, 1)
                rows.append(r)
                if len(rows) >= top:
                    break
            return f"top {kind}, tradeable only (price >= ${L.min_stock_price}, 20d avg $vol >= ${min_adv/1e6:.0f}M):\n" + md_table(rows)

        self.add("get_market_movers", "Today's top gainers, losers, or most-active stocks (penny stocks filtered out).",
                 _schema({"kind": _p("kind", "string", "gainers | losers | most_active"), "top": _p("top", "integer", "rows (default 15)")}, []), get_market_movers)

        def get_news(symbols: list[str] | None = None, query: str = "", limit: int = 10, hours: int = 48) -> str:
            out = []
            symbols = [s.upper() for s in (symbols or []) if s.strip()]
            c.research.note_news(symbols)
            if query:
                c.research.note_search(query)
            try:
                items = c.market.news(symbols or None, min(limit, 25), hours)
                label = "general market" if not symbols else ",".join(symbols)
                out.append(f"Benzinga feed ({label}, last {hours}h):\n" +
                           (md_table(items, ["time", "symbols", "headline", "source", "url"]) if items else "(none)"))
                if symbols and not items:
                    out.append(f"NO feed headlines for {','.join(symbols)} in the last {hours}h. If the name is moving, the move is UNEXPLAINED by covered news: "
                               f"treat it as catalyst grade 'none' unless the web search below (auto-run) finds a primary source.")
                    for s_ in symbols[:3]:
                        res = web_search(f"{s_} stock", "news", max_results=6, time_range="week")
                        out.append(f"web news for {s_}:\n" + (md_table(res, ["date", "title", "snippet", "url"]) if res else "(nothing found)"))
                elif not symbols and not query:
                    res = web_search("stock market today", "news", max_results=8, time_range="day")
                    out.append("web news 'stock market today' (last day):\n" + (md_table(res, ["date", "title", "snippet", "url"]) if res else "(none)"))
            except Exception as e:
                out.append(f"alpaca news error: {str(e)[:120]}")
            if query:
                res = web_search(query, "news", max_results=min(limit, 12), time_range="week")
                out.append(f"web news for '{query}':\n" + (md_table(res, ["date", "title", "snippet", "url"]) if res else "(none)"))
            return "\n\n".join(out)

        self.add("get_news", "News as a first source. No args = general market headlines + web 'stock market today'. With symbols = that name's feed (auto web-search fallback when the feed is empty, flagging an unexplained move). With query = web news search on any topic (macro, geopolitics, sector, rumor check).",
                 _schema({"symbols": _p("symbols", "array", "tickers (omit for general market news)", items={"type": "string"}),
                          "query": _p("query", "string", "free-text news search, e.g. 'Fed decision', 'NVDA earnings reaction'"),
                          "limit": _p("limit", "integer", "max items (default 10)"),
                          "hours": _p("hours", "integer", "lookback for the symbol feed (default 48)")}, []), get_news)

        def web_search_tool(query: str, category: str = "general", time_range: str = "", max_results: int = 8) -> str:
            c.research.note_search(query)
            res = web_search(query, category if category in ("general", "news") else "general", max_results=min(max_results, 15), time_range=time_range or None)
            return md_table(res, ["date", "title", "snippet", "url"]) if res else "(no results)"

        self.add("web_search", "Search the web (SearXNG). Use for catalysts, earnings dates, macro calendar, analyst moves, anything not in the other tools.",
                 _schema({"query": _p("query", "string", "search query"), "category": _p("category", "string", "general | news"),
                          "time_range": _p("time_range", "string", "day | week | month | year (optional)"),
                          "max_results": _p("max_results", "integer", "default 8")}, ["query"]), web_search_tool)

        def fetch_page_tool(url: str, max_chars: int = 5000) -> str:
            return fetch_page(url, max_chars=min(max_chars, 9000))

        self.add("fetch_page", "Fetch a web page and return its readable text. Blocked or thin pages are retried through Tavily extract automatically; if both fail, rely on search snippets.",
                 _schema({"url": _p("url", "string", "http(s) URL"), "max_chars": _p("max_chars", "integer", "default 5000")}, ["url"]), fetch_page_tool)

        def get_fundamentals(symbol: str) -> str:
            f = c.market.fundamentals(symbol)
            return f"{symbol.upper()}: " + ", ".join(f"{k}={v}" for k, v in f.items())

        self.add("get_fundamentals", "Company snapshot: sector, market cap, P/E, short % float, growth, analyst target, NEXT EARNINGS DATE, ex-dividend date.",
                 _schema({"symbol": _p("symbol", "string", "ticker")}, ["symbol"]), get_fundamentals)

        def get_earnings_dates(symbols: list[str]) -> str:
            rows = []
            for s in symbols[:12]:
                f = c.market.fundamentals(s)
                rows.append({"symbol": s.upper(), "next_earnings": f.get("next_earnings", "?"), "mkt_cap": f.get("marketCap"), "short_float": f.get("shortPercentOfFloat")})
            return md_table(rows)

        self.add("get_earnings_dates", "Next earnings date for up to 12 symbols (avoid unintended earnings gaps; or target them deliberately).",
                 _schema({"symbols": _p("symbols", "array", "tickers", items={"type": "string"})}, ["symbols"]), get_earnings_dates)

        # ---------------- primary-source macro
        def get_macro_data(items: list[str] | None = None) -> str:
            c.research.note_context()
            parts = [M.format_macro_data(M.bls_series(items))]
            try:
                parts.append(M.format_dashboard(M.market_dashboard()))
            except Exception as e:
                parts.append(f"dashboard error: {str(e)[:100]}")
            return "\n".join(parts)

        self.add("get_macro_data", "PRIMARY-SOURCE macro numbers: latest BLS prints (CPI, core CPI, PPI, core PPI, unemployment, payrolls, hourly earnings) with m/m and y/y, plus live yields (3m/5y/10y), WTI, gold, copper, dollar index, VIX, ES/NQ futures, bitcoin. Cite these; never infer macro numbers from headlines.",
                 _schema({"items": _p("items", "array", "subset of: cpi, core_cpi, ppi, core_ppi, unemployment, payrolls, hourly_earnings (omit for all)", items={"type": "string"})}, []), get_macro_data)

        def get_macro_release(name: str) -> str:
            n = name.lower().strip()
            if n in ("fomc", "fed", "fed_statement"):
                return M.fed_statement()
            return M.bls_release(n)

        self.add("get_macro_release", "Read the official release text: cpi | ppi | jobs | jolts | eci (BLS) or fomc (latest Fed statement). Use to confirm what a print actually said before writing it into the brief.",
                 _schema({"name": _p("name", "string", "cpi | ppi | jobs | jolts | eci | fomc")}, ["name"]), get_macro_release)

        def get_economic_calendar() -> str:
            parts = []
            for fn in (lambda: M.bls_schedule(21), M.fomc_calendar):
                try:
                    parts.append(fn())
                except Exception as e:
                    parts.append(f"calendar error: {str(e)[:100]}")
            try:
                parts.append("Recent Fed monetary-policy releases: " + "; ".join(f"{i['date']}: {i['title']}" for i in M.fed_monetary_rss(4)))
            except Exception as e:
                parts.append(f"fed rss error: {str(e)[:80]}")
            return "\n\n".join(parts)

        self.add("get_economic_calendar", "Upcoming scheduled macro events from primary sources: BLS release schedule (next 3 weeks), FOMC meeting dates, recent Fed monetary-policy releases.", _schema({}), get_economic_calendar)

        # ---------------- social attention (StockTwits, free)
        def get_social_buzz(symbol: str) -> str:
            from . import providers as P
            d = P.stocktwits_stream(symbol)
            if "error" in d:
                return d["error"]
            lines = [f"StockTwits {d['symbol']}: {d['watchers']} watchers | last 30 msgs span {d['span_hours_for_last_30']}h (~{d['msgs_per_hour']}/h) | tagged bullish {d['bullish']} / bearish {d['bearish']} (bull ratio {d['bull_ratio']})",
                     "top by likes: " + " || ".join(f"[{t['time']} {t['sentiment']} +{t['likes']}] {t['text']}" for t in d["top_by_likes"])]
            lines.append("Read: msgs/hour = attention (crowding when it spikes); bull ratio near 1.0 on a big up day = crowded long. Social is SPECULATIVE grade at best, never a catalyst by itself.")
            return "\n".join(lines)

        self.add("get_social_buzz", "Crowd attention and sentiment for a ticker from StockTwits (free): message rate, bullish/bearish tags, most-liked posts. Use as a crowding/attention signal, not as a catalyst.",
                 _schema({"symbol": _p("symbol", "string", "ticker")}, ["symbol"]), get_social_buzz)

        # ---------------- research dossier
        def research_symbol(symbol: str) -> str:
            u = symbol.upper().strip()
            c.research.note_dossier(u)
            out: list[str] = [f"# DOSSIER {u} ({now_et().strftime('%Y-%m-%d %H:%M')} ET)"]
            snap = c.market.snapshots([u]).get(u, {})
            out.append(f"quote: {snap.get('price')} @{snap.get('price_time')} ({snap.get('price_src')}), bid/ask {snap.get('bid')}/{snap.get('ask')}, today {snap.get('chg_pct')}%, prev close {snap.get('prev_close')}")
            table, _ = scr.load()
            if table is not None and u in table.index:
                r = table.loc[u]
                keys = ["chg_1d_pct", "chg_5d_pct", "chg_20d_pct", "chg_60d_pct", "rsi14", "sma20_dist_pct", "sma50_dist_pct", "sma200_dist_pct",
                        "hi52_dist_pct", "lo52_dist_pct", "atr14_pct", "adx14", "bb_squeeze", "vol_ratio", "avg_dollar_vol20_m", "mom_score",
                        "mom_1m_rank", "rs_spy_20d", "rs_sector_20d", "beta_spy", "sector", "days_to_earnings", "news_24h"]
                def _v(x):
                    return round(float(x), 2) if isinstance(x, (int, float)) and not isinstance(x, bool) else x
                out.append("daily context: " + ", ".join(f"{k}={_v(r[k])}" for k in keys if k in r.index and r[k] is not None and r[k] == r[k]))
            try:
                df = c.market.bars(u, "1D", 6)
                if not df.empty:
                    out.append("last 5 daily bars: " + "; ".join(f"{i.strftime('%m-%d')} o{float(x['open']):.2f} h{float(x['high']):.2f} l{float(x['low']):.2f} c{float(x['close']):.2f} v{int(x['volume'])}" for i, x in df.tail(5).iterrows()))
            except Exception:
                pass
            if snap.get("price") and c.broker.clock().get("is_open"):
                try:
                    it = scr.intraday(c.market)
                    if not it.empty and u in it.index:
                        r = it.loc[u]
                        out.append("intraday: " + ", ".join(f"{k}={r[k]}" for k in ("gap_pct", "from_open_pct", "range_pos", "from_hod_pct", "vwap_dist_pct", "rel_vol", "rs_today") if k in r.index))
                except Exception:
                    pass
            f = c.market.fundamentals(u)
            out.append("fundamentals: " + ", ".join(f"{k}={v}" for k, v in f.items()))
            urls: list[tuple[str, str]] = []
            try:
                items = c.market.news([u], 8, 72)
                out.append("news feed (72h): " + ("; ".join(f"[{i['time']}] {i['headline']} ({i['source']})" for i in items) if items else "NONE - no covered headlines; treat moves as unexplained unless the web finds a primary source"))
                urls += [(i["headline"], i["url"]) for i in items[:3] if i.get("url")]
            except Exception as e:
                out.append(f"news feed error: {str(e)[:80]}")
            try:
                web = web_search(f"{u} stock", "news", max_results=6, time_range="week")
                out.append("web news (week): " + ("; ".join(f"[{w['date'] or '?'}] {w['title']}" for w in web) if web else "none"))
                urls += [(w["title"], w["url"]) for w in web[:3] if w.get("url")]
                c.research.note_search(f"{u} stock")
            except Exception as e:
                out.append(f"web news error: {str(e)[:80]}")
            got = 0
            for title, url in urls:
                if got >= 2:
                    break
                txt = fetch_page(url, max_chars=1500)
                if txt.startswith("ERROR") or "headline only article" in txt.lower() or len(txt) < 350:
                    continue  # blocked, paywalled, or a stub
                out.append(f"ARTICLE ({title[:80]}) {url}\n{txt}")
                got += 1
            if got == 0 and urls:
                out.append("(article bodies unavailable: sites blocked the fetch; rely on headlines and cite them as such)")
            try:
                _today = now_et().date()
                _oi = {k["symbol"]: k for k in c.broker.option_contracts(u, _today + dt.timedelta(days=5), _today + dt.timedelta(days=60), None, limit=3000)}
                a = c.market.option_analytics(u, oi_map=_oi or None, rv20_pct=(float(table.loc[u]["rvol20_pct"]) if table is not None and u in table.index and table.loc[u]["rvol20_pct"] == table.loc[u]["rvol20_pct"] else None))
                if "error" not in a:
                    m_ = a["expiries"].get("month") or a["expiries"].get("near") or {}
                    out.append(f"options: iv30 {a.get('iv30_pct')}% vs rv20 {a.get('rv20_pct')} -> {a.get('premium_read')} (iv/rv {a.get('iv_over_rv')}); "
                               f"expected move by {m_.get('exp')}: +/-{m_.get('expected_move_pct')}% (straddle {m_.get('straddle_mid')}); skew25 {m_.get('skew_25d_put_minus_call')}; "
                               f"put/call OI {m_.get('put_call_oi_ratio')}; walls {m_.get('oi_walls')}; ATM call {m_.get('atm_call', {}).get('symbol')} {m_.get('atm_call', {}).get('bid')}/{m_.get('atm_call', {}).get('ask')}")
                    c.journal.record_iv(u, a.get("iv30_pct"), a.get("rv20_pct"))
                else:
                    out.append("options: none listed / no chain")
            except Exception as e:
                out.append(f"options: unavailable ({str(e)[:60]})")
            try:
                from . import providers as P
                sb_ = P.stocktwits_stream(u, 30)
                if "error" not in sb_:
                    out.append(f"social (StockTwits): {sb_['watchers']} watchers, ~{sb_['msgs_per_hour']} msgs/h, bull ratio {sb_['bull_ratio']} ({sb_['bullish']}B/{sb_['bearish']}b) — attention signal only")
            except Exception:
                pass
            hist = c.journal.decisions(6, u)
            if hist:
                out.append("journal history: " + "; ".join(f"{d['ts'][5:16]} {d['kind']} {d['side'] or ''} {d['qty'] or ''} @{d['price'] or ''} [{d['status']}]" for d in hist))
            th = [l["text"] for l in c.journal.lessons(40) if u in l["text"]]
            if th:
                out.append("lessons mentioning it: " + " | ".join(t[:160] for t in th[:3]))
            out.append("Cite: quote/bars = Alpaca; fundamentals = Yahoo Finance; headlines = Benzinga/web with dates above.")
            return "\n".join(out)

        self.add("research_symbol", "Full DOSSIER for one name in a single call: live quote, daily context and ranks, last bars, intraday stats (market hours), fundamentals and earnings date, 72h news feed + web news with the top 2 articles actually fetched and excerpted, options read (IV vs realized, expected move, skew, OI walls), your journal history and lessons for the name. Required before any entry; use it to go deep on every shortlisted candidate.",
                 _schema({"symbol": _p("symbol", "string", "ticker")}, ["symbol"]), research_symbol)

        # ---------------- state of the world
        _CONTEXT_ETFS = ["SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "USO", "UVXY", "XLK", "SMH", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "XBI", "KRE", "ARKK", "IBIT"]

        def list_knowledge() -> str:
            from .knowledge import KnowledgeBase
            return KnowledgeBase().index_text(60)

        self.add("list_knowledge", "List the library: durable notes written by study sessions (slug, title, summary, date).", _schema({}, []), list_knowledge)

        def get_knowledge(topic: str) -> str:
            from .knowledge import KnowledgeBase
            k = KnowledgeBase().get(topic)
            if not k:
                return f"no note for '{topic}' (list_knowledge shows what exists)"
            return f"# {k['title']} (updated {k['updated'][:16]})\n{k['body']}\n\nSources: " + "; ".join(k["sources"][:8])

        self.add("get_knowledge", "Read one library note in full by slug or title words.", _schema({"topic": _p("topic", "string", "slug or title words")}, ["topic"]), get_knowledge)

        def save_knowledge(topic: str, title: str, body: str, sources: list[str], summary: str = "") -> str:
            from .knowledge import KnowledgeBase, slugify
            if c.dry_run:
                return "dry run: not saved"
            if len((body or "").strip()) < 300:
                return "REJECTED: body too short — a note needs the mechanism, how it moves markets, what to watch, and how this book should use it"
            if not sources or not any(str(x).startswith("http") for x in sources):
                return "REJECTED: cite at least one URL you actually read (fetch_page/web_search results)"
            p = KnowledgeBase().save(topic, title, body, [str(x) for x in sources], summary or None)
            c.journal.add_event("knowledge", f"library note saved: {slugify(topic)} ({len(body)} chars)")
            return f"saved {p.name} ({len(body)} chars); it now appears in the Library index of every session"

        self.add("save_knowledge", "Save or update a library note (study sessions). Body <= 6000 chars, structured: what it is / how it moves markets / what to watch / how MarketMunchkin should use it. Cite URLs.",
                 _schema({"topic": _p("topic", "string", "slug, e.g. credit-spreads"), "title": _p("title", "string", "human title"), "body": _p("body", "string", "markdown"),
                          "sources": _p("sources", "array", "URLs read", items={"type": "string"}), "summary": _p("summary", "string", "one line (optional)")}, ["topic", "title", "body", "sources"]), save_knowledge)

        def get_market_context() -> str:
            c.research.note_context()
            snaps = c.market.snapshots(_CONTEXT_ETFS)
            table, _ = scr.load()
            rows = []
            for s_ in _CONTEXT_ETFS:
                sn = snaps.get(s_, {})
                r = {"symbol": s_, "price": sn.get("price"), "today_pct": sn.get("chg_pct")}
                if table is not None and s_ in table.index:
                    t = table.loc[s_]
                    r.update({"chg_5d": t.get("chg_5d_pct"), "chg_20d": t.get("chg_20d_pct"), "rsi14": t.get("rsi14"), "sma50_dist": t.get("sma50_dist_pct")})
                rows.append(r)
            parts = ["Market internals (UVXY ~ volatility proxy):\n" + md_table(rows)]
            try:
                parts.append(scr.regime_report(c.market, c.journal))
            except Exception as e:
                parts.append(f"regime error: {str(e)[:120]}")
            try:
                dials = M.world_dials(M.market_dashboard(), None, None)
                if dials:
                    parts.append("CROSS-ASSET DIALS (20-day, -1 headwind .. +1 tailwind; let these direct where you look and how hard you press): "
                                 + " | ".join(f"{d['label']} {d['score']:+.2f} ({d['value']})" for d in dials))
            except Exception as e:
                parts.append(f"dials unavailable: {str(e)[:80]}")
            try:
                items = c.market.news(None, 12, 18)
                parts.append("Benzinga general headlines (18h):\n" + (md_table(items, ["time", "symbols", "headline"]) if items else "(none)"))
            except Exception as e:
                parts.append(f"news error: {str(e)[:100]}")
            try:
                res = web_search("stock market today", "news", max_results=8, time_range="day")
                parts.append("Web: 'stock market today':\n" + (md_table(res, ["date", "title", "snippet"]) if res else "(none)"))
                res = web_search("world news markets geopolitics economy", "news", max_results=6, time_range="day")
                parts.append("Web: world/geopolitics/economy:\n" + (md_table(res, ["date", "title", "snippet"]) if res else "(none)"))
            except Exception as e:
                parts.append(f"web error: {str(e)[:100]}")
            wb = c.journal.get("world_brief")
            if wb:
                parts.append(f"Current world brief (updated {(c.journal.get('world_brief_ts') or '')[:16]}):\n{wb[:2500]}")
            return "\n\n".join(parts)

        self.add("get_market_context", "State of the world in one call: index/sector/rates/commodity/vol ETF moves, a computed REGIME score (trend, breadth, VIX level and term structure), breadth stats and trend, sector performance table, general market headlines, web news on markets and geopolitics, and the saved world brief. Call this first in every session.",
                 _schema({}), get_market_context)

        def set_world_brief(text: str) -> str:
            low = text.lower()
            if "source" not in low and "bls" not in low and "alpaca" not in low:
                return "ERROR: the brief must cite sources per fact, e.g. 'PPI final demand +0.4% m/m Aug (BLS API, 2026-09-10)'. Unsourced numbers are not accepted."
            if c.dry_run or not c.memory_writes:
                c.journal.set("world_brief_dryrun", text.strip())
                return "world brief saved (dry-run copy; the live brief is untouched)"
            c.journal.set("world_brief", text.strip())
            c.journal.set("world_brief_ts", now_et().isoformat(timespec="seconds"))
            return "world brief saved; it is shown at the start of every session"

        self.add("set_world_brief", "Save/replace the 'state of the world' brief: macro regime and risk appetite, key events today/this week (data, Fed, earnings, geopolitics), sector leadership/laggards, live themes with tickers, and known risks. Refresh it pre-market and whenever something material changes.",
                 _schema({"text": _p("text", "string", "the brief (10-25 lines, markdown ok)")}, ["text"]), set_world_brief)

        # ---------------- options data
        def get_option_chain(underlying: str, dte_min: int = 7, dte_max: int = 45, moneyness_pct: float = 8.0,
                             type: str = "", max_rows: int = 30) -> str:
            underlying = underlying.upper()
            today = now_et().date()
            ctype = type.lower() if type.lower() in ("call", "put") else None
            contracts = c.broker.option_contracts(underlying, today + dt.timedelta(days=dte_min), today + dt.timedelta(days=dte_max), ctype, limit=2000)
            oi = {k["symbol"]: k for k in contracts}
            rows = c.market.option_chain(underlying, dte_min, dte_max, moneyness_pct, ctype, oi_map=oi)
            if not rows:
                return f"no option snapshots for {underlying} in that window (check symbol, DTE window, moneyness)"
            px = c.market.price(underlying)
            # thin to the strikes nearest the money per expiration if too many
            max_rows = max(5, min(max_rows, 60))
            if len(rows) > max_rows:
                by_exp: dict[str, list] = {}
                for r in rows:
                    by_exp.setdefault(r["exp"] + r["type"], []).append(r)
                per = max(2, max_rows // len(by_exp))
                thinned = []
                for k, lst in by_exp.items():
                    lst.sort(key=lambda r: abs(r["moneyness_pct"] or 0))
                    thinned += sorted(lst[:per], key=lambda r: r["strike"])
                rows = sorted(thinned, key=lambda r: (r["exp"], r["type"], r["strike"]))
            return (f"{underlying} last={px} | {len(rows)} contracts (indicative quotes; prices per share, x100 per contract):\n" +
                    md_table(rows, ["symbol", "type", "strike", "exp", "dte", "bid", "ask", "mid", "spread_pct", "iv", "delta", "theta", "oi"], max_rows=60))

        self.add("get_option_chain", "Option chain with bid/ask, IV, greeks, open interest for an underlying, filtered by days-to-expiration and % moneyness around the current price.",
                 _schema({"underlying": _p("underlying", "string", "stock/ETF ticker"),
                          "dte_min": _p("dte_min", "integer", "min days to expiration (default 7)"),
                          "dte_max": _p("dte_max", "integer", "max days to expiration (default 45)"),
                          "moneyness_pct": _p("moneyness_pct", "number", "strikes within +/- this % of spot (default 8)"),
                          "type": _p("type", "string", "call | put (omit for both)"),
                          "max_rows": _p("max_rows", "integer", "default 30")}, ["underlying"]), get_option_chain)

        def get_option_quotes(symbols: list[str]) -> str:
            rows = c.market.option_snapshots(symbols[:15])
            return md_table(rows, ["symbol", "type", "strike", "exp", "dte", "bid", "ask", "mid", "spread_pct", "last", "iv", "delta", "theta", "quote_time", "error"])

        self.add("get_option_quotes", "Quotes and greeks for specific OCC option symbols (e.g. SOFI260918C00018000).",
                 _schema({"symbols": _p("symbols", "array", "OCC option symbols", items={"type": "string"})}, ["symbols"]), get_option_quotes)

        # ---------------- orders: stocks
        def buy_stock(symbol: str, thesis: str, target: str, stop: str, horizon: str, catalyst_grade: str,
                      stop_price: float, target_price: float, notional: float | None = None, qty: float | None = None,
                      order_type: str = "market", limit_price: float | None = None) -> str:
            gate = self._trading_gate()
            if gate:
                return gate
            symbol = symbol.upper()
            grade = normalize_grade(catalyst_grade)
            if grade is None:
                return f"ERROR: catalyst_grade must be one of {CATALYST_GRADES}"
            order_type = "limit" if order_type == "limit" else "market"
            if (notional is None) == (qty is None):
                return "ERROR: give exactly one of notional (dollars) or qty (shares)"
            snaps = c.market.snapshots([symbol]).get(symbol, {})
            price = snaps.get("price")
            if order_type == "limit" and limit_price is None:
                return "ERROR: limit orders need limit_price"
            est_px = limit_price if order_type == "limit" else price
            cost = float(notional) if notional is not None else float(qty) * float(est_px or 0)
            if qty is not None and order_type == "market" and price:
                cost *= 1.01  # slippage cushion
            pos = self._positions()
            st = c.risk.state(positions=pos)
            viol = c.research.gate(symbol) + c.risk.check_stock_buy(symbol, cost, price, order_type, limit_price, st, pos, grade)
            if qty is not None and float(qty) != int(float(qty)) and order_type == "limit":
                viol.append("fractional quantities require market orders (use notional or whole shares for limits)")
            ref = est_px or price
            if ref and not (float(stop_price) < ref):
                viol.append(f"stop_price {stop_price} must be below the entry price {ref}")
            if ref and not (float(target_price) > ref):
                viol.append(f"target_price {target_price} must be above the entry price {ref}")
            if ref and float(stop_price) < ref * 0.80:
                viol.append(f"stop_price {stop_price} is more than 20% below entry; too loose for this account")
            meta = {"catalyst_grade": grade, "stop_price": stop_price, "target_price": target_price}
            if viol:
                self._journal_order("open", symbol, "buy", qty or notional, est_px, None, "blocked", {**meta, "violations": viol}, thesis=thesis, target=target, stop=stop, horizon=horizon)
                return "REJECTED:\n- " + "\n- ".join(viol)
            if c.dry_run:
                self._journal_order("open", symbol, "buy", qty or notional, est_px, None, "dry_run", meta, thesis=thesis, target=target, stop=stop, horizon=horizon)
                return f"DRY RUN: would BUY {symbol} {'$%.2f' % notional if notional else str(qty) + ' sh'} {order_type} {limit_price or ''} (est cost ${cost:.2f}, grade {grade}); thesis journaled."
            o = c.broker.submit_stock_order(symbol, "buy", qty=qty, notional=notional, order_type=order_type, limit_price=limit_price)
            o = self._await_order(o["id"])
            self._journal_order("open", symbol, "buy", qty or notional, o.get("filled_avg_price") or est_px, o, o.get("status", "submitted"), meta, thesis=thesis, target=target, stop=stop, horizon=horizon)
            armed = _arm_stop(symbol, float(stop_price), float(target_price))
            return "SUBMITTED " + self._fmt_order(o) + armed

        self.add("buy_stock", "Buy a stock/ETF (fractional allowed via notional dollars). Requires thesis, target, stop, horizon which are journaled and later reviewed. Passes through the risk engine (settled cash, position limits, liquidity).",
                 _schema({"symbol": _p("symbol", "string", "ticker"),
                          "notional": _p("notional", "number", "dollar amount to buy (market orders; fractional shares)"),
                          "qty": _p("qty", "number", "share count (whole shares for limit orders)"),
                          "order_type": _p("order_type", "string", "market (default) | limit"),
                          "limit_price": _p("limit_price", "number", "required for limit orders"),
                          "thesis": _p("thesis", "string", "why this trade, including the catalyst and timing"),
                          "target": _p("target", "string", "price/condition to take profit"),
                          "stop": _p("stop", "string", "price/condition that invalidates the thesis -> exit"),
                          "horizon": _p("horizon", "string", "expected holding period, e.g. '3-7 trading days'"),
                          "catalyst_grade": _p("catalyst_grade", "string", "confirmed (primary source / major outlet, dated today-yesterday) | speculative (rumor, unnamed sources, social, single small outlet) | none (unexplained move, pure technical). Scales the size cap: 1.0 / 0.5 / 0.35"),
                          "stop_price": _p("stop_price", "number", "numeric protective stop; a resting stop order is placed at the broker automatically"),
                          "target_price": _p("target_price", "number", "numeric first target; the daemon wakes you when it trades")},
                         ["symbol", "thesis", "target", "stop", "horizon", "catalyst_grade", "stop_price", "target_price"]), buy_stock)

        def sell_stock(symbol: str, reason: str, qty: float | None = None, order_type: str = "market", limit_price: float | None = None) -> str:
            gate = self._trading_gate()
            if gate:
                return gate
            symbol = symbol.upper()
            pos = self._positions()
            p = next((x for x in pos if x["symbol"] == symbol), None)
            if p is None:
                return f"ERROR: no position in {symbol}"
            held = float(p.get("qty_available") or p["qty"])
            q = float(qty) if qty is not None else held
            st = c.risk.state(positions=pos)
            viol = c.risk.check_sell(symbol, q, st, pos)
            if order_type == "limit" and limit_price is None:
                viol.append("limit orders need limit_price")
            if viol:
                self._journal_order("close", symbol, "sell", q, limit_price, None, "blocked", {"violations": viol, "reason": reason})
                return "REJECTED by risk engine:\n- " + "\n- ".join(viol)
            if c.dry_run:
                self._journal_order("close", symbol, "sell", q, limit_price, None, "dry_run", {"reason": reason})
                return f"DRY RUN: would SELL {q} {symbol} {order_type} {limit_price or ''} (reason: {reason})"
            X.cancel_exit_orders(symbol)
            if qty is None and order_type == "market":
                o = c.broker.close_position(symbol)
            else:
                o = c.broker.submit_stock_order(symbol, "sell", qty=q, order_type=order_type, limit_price=limit_price)
            o = self._await_order(o["id"])
            remaining = 0.0 if (qty is None or q >= held - 1e-9) else round(held - q, 6)
            self._journal_order("close", symbol, "sell", q, o.get("filled_avg_price") or limit_price, o, o.get("status", "submitted"),
                                {"reason": reason, "partial": remaining > 0, "remaining_qty": remaining})
            if remaining <= 0:
                c.journal.clear_exits(symbol)
            else:
                X.ensure()  # re-arm the stop for the remaining shares
            return "SUBMITTED " + self._fmt_order(o) + (f" | PARTIAL close: {remaining:g} shares remain open" if remaining > 0 else " | position fully closed")

        self.add("sell_stock", "Sell (close) some or all of a stock position.",
                 _schema({"symbol": _p("symbol", "string", "ticker"), "qty": _p("qty", "number", "shares (omit = entire position)"),
                          "order_type": _p("order_type", "string", "market (default) | limit"), "limit_price": _p("limit_price", "number", "for limit orders"),
                          "reason": _p("reason", "string", "why: target hit / stop hit / thesis broken / rebalancing")}, ["symbol", "reason"]), sell_stock)

        # ---------------- orders: options
        def buy_option(option_symbol: str, qty: int, limit_price: float, thesis: str, target: str, stop: str, horizon: str, catalyst_grade: str,
                       stop_premium: float | None = None, target_premium: float | None = None) -> str:
            gate = self._trading_gate()
            if gate:
                return gate
            sym = option_symbol.upper().replace(" ", "")
            grade = normalize_grade(catalyst_grade)
            if grade is None:
                return f"ERROR: catalyst_grade must be one of {CATALYST_GRADES}"
            pp = parse_occ(sym)
            pos = self._positions()
            st = c.risk.state(positions=pos)
            viol, info = c.risk.check_option_buy(sym, int(qty), float(limit_price), st, pos, self._acct_level(), grade)
            viol = (c.research.gate(pp["underlying"]) if pp else []) + viol
            if stop_premium is not None and not (0 < float(stop_premium) < float(limit_price)):
                viol.append(f"stop_premium {stop_premium} must be below the limit price {limit_price}")
            meta = {"catalyst_grade": grade, "info": info, "stop_price": stop_premium, "target_price": target_premium}
            if viol:
                self._journal_order("open", sym, "buy", qty, limit_price, None, "blocked", {**meta, "violations": viol}, thesis=thesis, target=target, stop=stop, horizon=horizon)
                return "REJECTED:\n- " + "\n- ".join(viol) + f"\n(quote: bid {info.get('bid')} ask {info.get('ask')} oi {info.get('oi')} dte {info.get('dte')})"
            if c.dry_run:
                self._journal_order("open", sym, "buy", qty, limit_price, None, "dry_run", meta, thesis=thesis, target=target, stop=stop, horizon=horizon)
                return f"DRY RUN: would BUY {qty}x {occ_human(sym)} limit {limit_price} (cost ${info['cost']:.2f}, grade {grade}); quote bid {info.get('bid')} ask {info.get('ask')}"
            o = c.broker.submit_option_order(sym, "buy", int(qty), float(limit_price), "buy_to_open")
            o = self._await_order(o["id"])
            self._journal_order("open", sym, "buy", qty, o.get("filled_avg_price") or limit_price, o, o.get("status", "submitted"), meta, thesis=thesis, target=target, stop=stop, horizon=horizon)
            armed = _arm_stop(sym, float(stop_premium) if stop_premium is not None else None, float(target_premium) if target_premium is not None else None)
            return "SUBMITTED " + self._fmt_order(o) + armed

        self.add("buy_option", "Buy to open a call or put (limit order, per-share premium; cost = limit x 100 x qty). Journals thesis/target/stop/horizon. Risk engine checks DTE, liquidity, spread, budget.",
                 _schema({"option_symbol": _p("option_symbol", "string", "OCC symbol from get_option_chain"), "qty": _p("qty", "integer", "contracts"),
                          "limit_price": _p("limit_price", "number", "per-share limit, at or inside the ask"),
                          "thesis": _p("thesis", "string", "why + catalyst inside the holding window"), "target": _p("target", "string", "exit target (premium or underlying level)"),
                          "stop": _p("stop", "string", "exit condition on the downside"), "horizon": _p("horizon", "string", "planned holding period"),
                          "catalyst_grade": _p("catalyst_grade", "string", "confirmed | speculative | none (scales the size cap 1.0 / 0.5 / 0.35)"),
                          "stop_premium": _p("stop_premium", "number", "per-share premium at which to stop out; a DAY stop order rests at the broker (re-armed each morning)"),
                          "target_premium": _p("target_premium", "number", "per-share premium target; the daemon wakes you when the mark reaches it")},
                         ["option_symbol", "qty", "limit_price", "thesis", "target", "stop", "horizon", "catalyst_grade"]), buy_option)

        def sell_option(option_symbol: str, reason: str, qty: int | None = None, limit_price: float | None = None) -> str:
            gate = self._trading_gate()
            if gate:
                return gate
            sym = option_symbol.upper().replace(" ", "")
            pos = self._positions()
            p = next((x for x in pos if x["symbol"] == sym), None)
            if p is None:
                return f"ERROR: no position in {occ_human(sym)}"
            held = int(float(p.get("qty_available") or p["qty"]))
            q = int(qty) if qty is not None else held
            st = c.risk.state(positions=pos)
            viol = c.risk.check_sell(sym, q, st, pos)
            if limit_price is None:
                snap = c.market.option_snapshots([sym])[0]
                limit_price = snap.get("bid")
                if not limit_price:
                    viol.append("no bid available; supply limit_price explicitly")
            if viol:
                self._journal_order("close", sym, "sell", q, limit_price, None, "blocked", {"violations": viol, "reason": reason})
                return "REJECTED by risk engine:\n- " + "\n- ".join(viol)
            if c.dry_run:
                self._journal_order("close", sym, "sell", q, limit_price, None, "dry_run", {"reason": reason})
                return f"DRY RUN: would SELL {q}x {occ_human(sym)} limit {limit_price} (reason: {reason})"
            X.cancel_exit_orders(sym)
            o = c.broker.submit_option_order(sym, "sell", q, float(limit_price), "sell_to_close")
            o = self._await_order(o["id"])
            self._journal_order("close", sym, "sell", q, o.get("filled_avg_price") or limit_price, o, o.get("status", "submitted"), {"reason": reason})
            if q >= held:
                c.journal.clear_exits(sym)
            return "SUBMITTED " + self._fmt_order(o)

        self.add("sell_option", "Sell to close a long option position (limit; defaults to the current bid if limit_price omitted).",
                 _schema({"option_symbol": _p("option_symbol", "string", "OCC symbol"), "qty": _p("qty", "integer", "contracts (omit = all)"),
                          "limit_price": _p("limit_price", "number", "per-share limit"), "reason": _p("reason", "string", "why")}, ["option_symbol", "reason"]), sell_option)

        def open_spread(legs: list[dict[str, Any]], qty: int, net_debit: float, thesis: str, target: str, stop: str, horizon: str, catalyst_grade: str) -> str:
            gate = self._trading_gate()
            if gate:
                return gate
            grade = normalize_grade(catalyst_grade)
            if grade is None:
                return f"ERROR: catalyst_grade must be one of {CATALYST_GRADES}"
            norm = []
            for l in legs:
                side = str(l.get("side", "")).lower()
                norm.append({"symbol": str(l.get("symbol", "")).upper().replace(" ", ""), "side": side, "ratio_qty": 1,
                             "position_intent": "buy_to_open" if side == "buy" else "sell_to_open"})
            pos = self._positions()
            st = c.risk.state(positions=pos)
            viol, infos = c.risk.check_mleg(norm, int(qty), float(net_debit), st, pos, self._acct_level(), grade)
            label = " / ".join(f"{l['side']} {occ_human(l['symbol'])}" for l in norm)
            u = parse_occ(norm[0]["symbol"])["underlying"] if norm and parse_occ(norm[0]["symbol"]) else "?"
            viol = (c.research.gate(u) if u != "?" else []) + viol
            if viol:
                self._journal_order("open", norm[0]["symbol"] if norm else "?", "spread", qty, net_debit, None, "blocked", {"catalyst_grade": grade, "violations": viol, "legs": norm}, thesis=thesis, target=target, stop=stop, horizon=horizon, underlying=u)
                return "REJECTED:\n- " + "\n- ".join(viol)
            if c.dry_run:
                self._journal_order("open", norm[0]["symbol"], "spread", qty, net_debit, None, "dry_run", {"catalyst_grade": grade, "legs": norm}, thesis=thesis, target=target, stop=stop, horizon=horizon, underlying=u)
                return f"DRY RUN: would open {qty}x spread [{label}] net debit {net_debit} (cost ${net_debit*100*qty:.2f})"
            o = c.broker.submit_mleg_order(norm, int(qty), float(net_debit))
            o = self._await_order(o["id"])
            for l in norm:
                self._journal_order("open", l["symbol"], l["side"], qty, net_debit, o, o.get("status", "submitted"), {"legs": norm, "spread": label, "catalyst_grade": grade}, thesis=thesis, target=target, stop=stop, horizon=horizon, underlying=u)
            extra = ""
            if o.get("status") == "filled" and len(norm) == 2:
                fill = float(o.get("filled_avg_price") or net_debit)
                sp_, tp_ = round(fill * 0.5, 2), round(fill * 2.0, 2)
                SpreadBook(c.journal).register(norm[0]["symbol"], norm[1]["symbol"], int(qty), fill, sp_, tp_, c.session_id)
                extra = f" | spread tracked on net value: stop {sp_} target {tp_} (adjust with set_exit_levels on the long leg)"
            return "SUBMITTED " + self._fmt_order(o) + extra

        self.add("open_spread", "Open a defined-risk DEBIT spread (vertical/calendar/diagonal, 2-4 legs, 1:1). legs: [{symbol, side:'buy'|'sell'}]. net_debit is the per-share price to pay (max loss = net_debit x 100 x qty).",
                 _schema({"legs": _p("legs", "array", "option legs", items={"type": "object", "properties": {"symbol": {"type": "string"}, "side": {"type": "string"}}, "required": ["symbol", "side"]}),
                          "qty": _p("qty", "integer", "number of spreads"), "net_debit": _p("net_debit", "number", "limit net debit per share"),
                          "thesis": _p("thesis", "string", "why"), "target": _p("target", "string", "exit target"), "stop": _p("stop", "string", "exit condition"),
                          "horizon": _p("horizon", "string", "holding period"),
                          "catalyst_grade": _p("catalyst_grade", "string", "confirmed | speculative | none")},
                         ["legs", "qty", "net_debit", "thesis", "target", "stop", "horizon", "catalyst_grade"]), open_spread)

        def close_spread(legs: list[dict[str, Any]], qty: int, net_credit: float, reason: str) -> str:
            gate = self._trading_gate()
            if gate:
                return gate
            pos = self._positions()
            held = {p["symbol"]: p for p in pos}
            norm = []
            viol: list[str] = []
            for l in legs:
                sym = str(l.get("symbol", "")).upper().replace(" ", "")
                p = held.get(sym)
                if p is None:
                    viol.append(f"no position in {occ_human(sym)}")
                    continue
                long_leg = float(p["qty"]) > 0
                norm.append({"symbol": sym, "side": "sell" if long_leg else "buy", "ratio_qty": 1,
                             "position_intent": "sell_to_close" if long_leg else "buy_to_close"})
                if c.journal.bought_today(sym):
                    st = c.risk.state(positions=pos)
                    if st.day_trades_remaining <= 0:
                        viol.append(f"closing {occ_human(sym)} today would be a day trade; budget exhausted")
            if viol:
                return "REJECTED:\n- " + "\n- ".join(viol)
            label = " / ".join(f"{l['position_intent']} {occ_human(l['symbol'])}" for l in norm)
            if c.dry_run:
                return f"DRY RUN: would close {qty}x [{label}] for net credit {net_credit}"
            for l in norm:
                X.cancel_exit_orders(l["symbol"])
            o = c.broker.submit_mleg_order(norm, int(qty), -abs(float(net_credit)))
            o = self._await_order(o["id"])
            for l in norm:
                self._journal_order("close", l["symbol"], l["side"], qty, net_credit, o, o.get("status", "submitted"), {"reason": reason, "legs": norm})
                SpreadBook(c.journal).clear(l["symbol"])
            return "SUBMITTED " + self._fmt_order(o)

        self.add("close_spread", "Close an existing spread in one multi-leg order for a net credit (legs: [{symbol}], sides inferred from positions).",
                 _schema({"legs": _p("legs", "array", "option legs held", items={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}),
                          "qty": _p("qty", "integer", "spreads to close"), "net_credit": _p("net_credit", "number", "minimum net credit per share to accept"),
                          "reason": _p("reason", "string", "why")}, ["legs", "qty", "net_credit", "reason"]), close_spread)

        # ---------------- memory
        def record_lesson(text: str, tags: str = "") -> str:
            if len(text) > 400:
                return "ERROR: a lesson is a rule in one or two sentences (max 400 chars), not a diary entry. Put the narrative in record_note and state the rule here."
            if c.dry_run or not c.memory_writes:
                c.journal.add_note(f"[dry-run lesson, not saved] {text}", c.session_id)
                return "noted (dry-run/read-only sessions do not write lessons)"
            c.journal.add_lesson(text, source=f"session:{c.session_id}", tags=tags)
            return "lesson recorded"

        self.add("record_lesson", "Save a durable lesson learned (shown in future sessions). Be specific and actionable.",
                 _schema({"text": _p("text", "string", "the lesson"), "tags": _p("tags", "string", "comma tags, e.g. options,sizing")}, ["text"]), record_lesson)

        def record_note(text: str) -> str:
            c.journal.add_note(text, c.session_id)
            return "note recorded"

        self.add("record_note", "Save a working note/observation for this session (not a lesson).", _schema({"text": _p("text", "string", "note")}, ["text"]), record_note)

        def set_plan(text: str) -> str:
            if c.dry_run or not c.memory_writes:
                c.journal.set("plan_dryrun", text.strip())
                return "plan saved (dry-run/read-only copy; the live plan is untouched)"
            c.journal.set_plan(text)
            c.plan_set = True
            return "plan saved; it will be shown at the start of the next session"

        self.add("set_plan", "Save the plan for the next session: per-position stops/targets/actions, watchlist with entry triggers, what to check first. Always call this before finishing.",
                 _schema({"text": _p("text", "string", "plan text (markdown ok)")}, ["text"]), set_plan)

        def get_journal(what: str = "decisions", limit: int = 15, symbol: str = "") -> str:
            j = c.journal
            limit = max(1, min(limit, 40))
            if what == "trades":
                rows = j.trades(limit)
                return md_table([{**{k: r[k] for k in ("id", "symbol", "qty", "entry", "exit", "pnl", "pnl_pct")}, "opened": r["opened_at"][:16], "closed": r["closed_at"][:16], "review": (r["review"] or "")[:100]} for r in rows]) + f"\nstats: {j.trade_stats()}"
            if what == "lessons":
                return "\n".join(f"- [{r['ts'][:10]}] {r['text']}" for r in j.lessons(limit)) or "(none)"
            if what == "notes":
                return "\n".join(f"- [{r['ts'][:16]}] {r['text'][:300]}" for r in j.notes(limit)) or "(none)"
            if what == "sessions":
                return "\n\n".join(f"[{r['started_at'][:16]} {r['phase']}] {(r['summary'] or '')[:700]}" for r in j.sessions(limit)) or "(none)"
            rows = j.decisions(limit, symbol.upper() or None)
            def _kind(r):
                m = json.loads(r["meta"] or "{}")
                return f"close (partial, {m.get('remaining_qty')} left)" if r["kind"] == "close" and m.get("partial") else r["kind"]
            return md_table([{"ts": r["ts"][5:16], "kind": _kind(r), "symbol": occ_human(r["symbol"]) if is_option(r["symbol"]) else r["symbol"], "side": r["side"], "qty": r["qty"], "price": r["price"], "status": r["status"], "grade": (json.loads(r["meta"] or "{}").get("catalyst_grade")), "thesis": (r["thesis"] or "")[:90], "target": r["target"], "stop": r["stop"]} for r in rows])

        self.add("get_journal", "Read your memory: decisions (with theses), trades (closed round-trips with P&L), lessons, notes, or past session summaries.",
                 _schema({"what": _p("what", "string", "decisions | trades | lessons | notes | sessions"), "limit": _p("limit", "integer", "default 15"),
                          "symbol": _p("symbol", "string", "filter decisions by symbol")}, []), get_journal)

        def update_playbook(new_markdown: str) -> str:
            if not c.memory_writes or c.dry_run:
                return "playbook edits are disabled in this mode"
            if len(new_markdown) < 200:
                return "ERROR: playbook replacement too short; supply the full revised document."
            if len(new_markdown) > 7000:
                return f"ERROR: playbook is {len(new_markdown)} chars; keep it under 7000 (it is injected into every prompt). Consolidate."
            c.journal.update_playbook(new_markdown)
            return "playbook updated (previous version archived)"

        self.add("update_playbook", "Replace the playbook (your standing strategy rules) with a full revised markdown document. Use sparingly, when a rule genuinely changes.",
                 _schema({"new_markdown": _p("new_markdown", "string", "complete new playbook")}, ["new_markdown"]), update_playbook)

        # ---------------- armed conditional entries (intent the watcher executes)
        EB = EntryBook(c.journal)

        def arm_entry(symbol: str, direction: str, trigger_price: float, notional: float, stop_price: float, target_price: float,
                      thesis: str, catalyst_grade: str, horizon: str, expires_hours: float = 30.0, max_chase_pct: float = 1.0,
                      not_before: str | None = None, spy_min_chg_pct: float | None = None, expression: str = "stock",
                      dte_target: int = 14, opt_stop_pct: float = 0.5, opt_target_pct: float = 1.0) -> str:
            gate = self._trading_gate()
            if gate:
                return gate
            u = symbol.upper()
            grade = normalize_grade(catalyst_grade)
            if grade is None:
                return f"ERROR: catalyst_grade must be one of {CATALYST_GRADES}"
            expression = (expression or "stock").lower()
            if expression not in EXPRESSIONS:
                return f"ERROR: expression must be one of {EXPRESSIONS}"
            if expression != "stock" and not (7 <= int(dte_target) <= 60):
                return "ERROR: dte_target must be 7-60 for option expressions"
            viol = c.research.gate(u)
            tp, sp, trg = float(target_price), float(stop_price), float(trigger_price)
            if expression in ("stock", "call", "call_spread") and not (sp < trg < tp):
                viol.append("need stop_price < trigger_price < target_price (underlying levels)")
            if expression in ("put", "put_spread") and not (tp < trg < sp):
                viol.append("for puts: need target_price < trigger_price < stop_price (underlying levels; the position's own stop is opt_stop_pct of premium)")
            if sp < trg * 0.80:
                viol.append("stop more than 20% below the trigger; too loose")
            pos = self._positions()
            st = c.risk.state(positions=pos)
            cap = st.max_position_notional * c.risk.grade_multiplier(grade)
            if float(notional) > cap + 0.01:
                viol.append(f"notional ${float(notional):.2f} exceeds the cap ${cap:.2f} for grade '{grade}'")
            if float(notional) > st.virtual_settled_cash + 0.01:
                viol.append(f"notional exceeds settled cash ${st.virtual_settled_cash:.2f}")
            nb = None
            if not_before:
                try:
                    nb = dt.datetime.fromisoformat(not_before.replace("Z", "+00:00"))
                    if nb.tzinfo is None:
                        from .util import ET as _ET
                        nb = nb.replace(tzinfo=_ET)
                    nb = nb.isoformat(timespec="seconds")
                except Exception:
                    viol.append("not_before must be an ISO datetime like 2026-09-11T08:35 (ET)")
            if viol:
                return "REJECTED:\n- " + "\n- ".join(viol)
            if c.dry_run:
                return f"DRY RUN: would arm {u} buy ${float(notional):.0f} when price {direction} {trg} (stop {sp}, target {tp}, not before {nb}, spy>= {spy_min_chg_pct})"
            rec = EB.arm(u, direction, trg, float(notional), sp, tp, thesis, grade, horizon, expires_hours, c.session_id, max_chase_pct, nb, spy_min_chg_pct,
                         expression, int(dte_target), float(opt_stop_pct), float(opt_target_pct))
            c.journal.add_decision(c.session_id, "arm", u, side="buy", qty=float(notional), price=trg, thesis=thesis, target=str(tp), stop=str(sp),
                                   horizon=horizon, status="armed", meta={"catalyst_grade": grade, "direction": direction, "expires": rec["expires"]}, underlying=u)
            cond = (f", not before {nb[:16]}" if nb else "") + (f", only if SPY today >= {spy_min_chg_pct}%" if spy_min_chg_pct is not None else "")
            ex = ""
            if expression != "stock":
                ex = f" as {expression} (~{dte_target} DTE, resolved at fire time; position stop {int(opt_stop_pct*100)}% of premium, target +{int(opt_target_pct*100)}%)"
                try:
                    from .optentry import resolve_affordable
                    ctype = "call" if expression.startswith("call") else "put"
                    today = now_et().date()
                    min_dte = max(L.min_option_dte + 1, 4)
                    _oi = {k["symbol"]: k for k in c.broker.option_contracts(u, today + dt.timedelta(days=min_dte), today + dt.timedelta(days=int(dte_target) + 21), ctype, limit=3000)}
                    rows = c.market.option_chain(u, min_dte, int(dte_target) + 21, 15.0, ctype, oi_map=_oi)
                    legs, lim, note = resolve_affordable(rows, expression, float(notional), int(dte_target), min_dte)
                    ex += (f". Right now that resolves to {note} at ~{lim} = ${lim*100:.0f}/contract, {max(1, int(float(notional) // (lim*100)))} contract(s)" if legs
                           else f". WARNING: {note}")
                except Exception as e:
                    ex += f" (preview unavailable: {str(e)[:60]})"
            return f"ARMED: {u} {expression} ${float(notional):.0f} when price {rec['direction']} {trg} (max chase {max_chase_pct}%){cond}{ex}, underlying stop {sp} target {tp}, expires {(rec['expires'] or 'never')[:16]}. The watcher executes it within a minute of the trigger and wakes you."

        self.add("arm_entry", "State trading intent the watcher will execute for you when the UNDERLYING crosses trigger_price ('above' for breakouts, 'below' for pullbacks): expression = stock | call | put | call_spread | put_spread (options are resolved at fire time: expiry nearest dte_target, delta ~0.5 single or 0.55/0.30 vertical, limit inside the market, qty = notional / premium). Optional not_before (e.g. '2026-09-11T08:35' for after CPI) and spy_min_chg_pct tape filter. Runs the same risk checks at fire time, arms the stop (spreads are managed on net value), and wakes you. Use instead of 'no trigger met' or 'wait for the event'.",
                 _schema({"symbol": _p("symbol", "string", "ticker"), "direction": _p("direction", "string", "above | below"),
                          "trigger_price": _p("trigger_price", "number", "price that fires the entry"), "notional": _p("notional", "number", "dollars to buy"),
                          "stop_price": _p("stop_price", "number", "protective stop after fill"), "target_price": _p("target_price", "number", "first target"),
                          "thesis": _p("thesis", "string", "why, with the catalyst and its source"), "catalyst_grade": _p("catalyst_grade", "string", "confirmed | speculative | none"),
                          "horizon": _p("horizon", "string", "holding period"), "expires_hours": _p("expires_hours", "number", "auto-disarm after this many hours (default 30)"),
                          "max_chase_pct": _p("max_chase_pct", "number", "do not fill if price is more than this % past the trigger (default 1.0)"),
                          "not_before": _p("not_before", "string", "ISO datetime in ET; the entry is inactive before it (use for post-event triggers)"),
                          "spy_min_chg_pct": _p("spy_min_chg_pct", "number", "require SPY's change today to be >= this % when the trigger fires (tape filter)"),
                          "expression": _p("expression", "string", "stock (default) | call | put | call_spread | put_spread"),
                          "dte_target": _p("dte_target", "integer", "target days to expiry for option expressions (7-60, default 14)"),
                          "opt_stop_pct": _p("opt_stop_pct", "number", "option position stop as a fraction of premium paid (default 0.5)"),
                          "opt_target_pct": _p("opt_target_pct", "number", "option first target as a fraction gain on premium (default 1.0 = +100%)")},
                         ["symbol", "direction", "trigger_price", "notional", "stop_price", "target_price", "thesis", "catalyst_grade", "horizon"]), arm_entry)

        def list_entries() -> str:
            return EB.describe()

        self.add("list_entries", "Show the armed conditional entries the watcher is holding.", _schema({}), list_entries)

        def disarm_entry(symbol: str, reason: str) -> str:
            if not c.allow_trading:
                return "trading disabled in this mode"
            ok = EB.disarm(symbol)
            if ok:
                c.journal.add_decision(c.session_id, "disarm", symbol.upper(), status="disarmed", meta={"reason": reason}, underlying=symbol.upper())
            return f"{symbol.upper()} disarmed" if ok else f"no armed entry for {symbol.upper()}"

        self.add("disarm_entry", "Cancel an armed conditional entry.", _schema({"symbol": _p("symbol", "string", "ticker"), "reason": _p("reason", "string", "why")}, ["symbol", "reason"]), disarm_entry)

        # ---------------- free-form quantitative analysis (sandboxed pandas)
        def run_analysis(code: str, symbols: list[str] | None = None, timeframe: str = "1D", bars: int = 260,
                         include_screener: bool = True, option_underlyings: list[str] | None = None) -> str:
            import pandas as pd
            from .analytics import screen_frame
            dataset: dict[str, Any] = {"bars": {}, "screener": None, "chains": {}, "fills": None, "trades": None, "equity": None}
            syms = [x.upper() for x in (symbols or [])][:25]
            tf = timeframe if timeframe in ("1D", "1W", "1H", "30Min", "15Min", "5Min") else "1D"
            for sym in syms:
                try:
                    df = c.market.bars(sym, tf, max(30, min(int(bars), 1500)))
                    if not df.empty:
                        df = df.copy()
                        if tf in ("1D", "1W"):
                            f = screen_frame(df)
                            df = df.join(f.drop(columns=[x for x in f.columns if x in df.columns], errors="ignore"))
                        df.index = df.index.tz_convert("America/New_York")
                        dataset["bars"][sym] = df
                        c.research.note_chart(sym)
                except Exception as e:
                    dataset["bars"][sym] = pd.DataFrame({"error": [str(e)[:120]]})
            if include_screener:
                table, _ = scr.load()
                if table is not None:
                    dataset["screener"] = table
                    c.research.note_screen()
            for u in [x.upper() for x in (option_underlyings or [])][:5]:
                try:
                    today = now_et().date()
                    oi = {k["symbol"]: k for k in c.broker.option_contracts(u, today + dt.timedelta(days=3), today + dt.timedelta(days=75), None, limit=3000)}
                    dataset["chains"][u] = pd.DataFrame(c.market.option_chain(u, 3, 75, 20.0, None, oi_map=oi))
                except Exception as e:
                    dataset["chains"][u] = pd.DataFrame({"error": [str(e)[:120]]})
            try:
                dataset["fills"] = pd.DataFrame(c.journal.fills())
                dataset["trades"] = pd.DataFrame(c.journal.trades(500))
                dataset["equity"] = pd.DataFrame(c.journal.equity_series(limit=20000))
            except Exception:
                pass
            return SB.run(code, dataset, timeout_s=40, max_chars=7500)

        self.add("run_analysis", "Run your own pandas/numpy/scipy/pandas_ta code (sandboxed: no network, no files, no credentials). Preloaded variables: bars (dict symbol -> OHLCV DataFrame with vwap/trade_count and, for daily, the screener indicator columns), screener (the full daily table, index=symbol), chains (dict underlying -> option chain DataFrame with bid/ask/iv/greeks/oi), fills, trades, equity (your own history), plus pd, np, stats, ta (pandas_ta). print() results or end with an expression. Use for anything the fixed tools do not compute: custom indicators, correlations/beta, seasonality, event studies, expected-move vs realized, sizing simulations, quick backtests of a rule.",
                 _schema({"code": _p("code", "string", "python code"), "symbols": _p("symbols", "array", "symbols to load into `bars` (max 25)", items={"type": "string"}),
                          "timeframe": _p("timeframe", "string", "1D (default) | 1W | 1H | 30Min | 15Min | 5Min"), "bars": _p("bars", "integer", "bars per symbol (default 260, max 1500)"),
                          "include_screener": _p("include_screener", "boolean", "load the screener table (default true)"),
                          "option_underlyings": _p("option_underlyings", "array", "underlyings whose option chains to load into `chains` (max 5)", items={"type": "string"})}, ["code"]), run_analysis)

        # ---------------- self-assigned work
        def add_task(text: str, priority: int = 2, kind: str = "task") -> str:
            if not c.memory_writes:
                return "task queue is read-only in this mode"
            kind = kind if kind in ("task", "reflect", "research", "experiment") else "task"
            tid = c.journal.add_task(text, max(1, min(int(priority), 5)), c.session_id, kind)
            return f"task #{tid} queued (priority {priority}, kind {kind}); the daemon will hand it to a future session"

        self.add("add_task", "Give yourself work for a future session: research to do, a hypothesis to test, a name to watch for a trigger, a rule to evaluate, or 'reflect' to request a free-thinking session. Priority 1 (urgent) to 5 (whenever). The daemon runs open tasks in order between events.",
                 _schema({"text": _p("text", "string", "what to do and what 'done' looks like"), "priority": _p("priority", "integer", "1-5, default 2"),
                          "kind": _p("kind", "string", "task | research | experiment | reflect")}, ["text"]), add_task)

        def list_tasks() -> str:
            rows = c.journal.open_tasks(25)
            hist = c.journal.tasks_history(8)
            out = ["open:"] + [f"#{r['id']} p{r['priority']} [{r['kind']}] {r['text'][:200]}" for r in rows] if rows else ["open: (none)"]
            done = [f"#{r['id']} {r['status']} {(r['result'] or '')[:100]}" for r in hist if r["status"] != "open"]
            if done:
                out.append("recently closed: " + " | ".join(done[:6]))
            return "\n".join(out)

        self.add("list_tasks", "Show your open task queue and recently completed tasks.", _schema({}), list_tasks)

        def complete_task(task_id: int, result: str, status: str = "done") -> str:
            if not c.memory_writes:
                return "task queue is read-only in this mode"
            ok = c.journal.complete_task(int(task_id), result, "done" if status not in ("done", "dropped") else status)
            return f"task #{task_id} marked {status}" if ok else f"task #{task_id} not found or already closed"

        self.add("complete_task", "Close a task with a short result (what was learned / decided), or drop it.",
                 _schema({"task_id": _p("task_id", "integer", "id"), "result": _p("result", "string", "outcome"), "status": _p("status", "string", "done | dropped")}, ["task_id", "result"]), complete_task)

        def get_calendar() -> str:
            clk = c.broker.clock()
            today = now_et().date()
            cal = c.broker.calendar(today, today + dt.timedelta(days=10))
            days = ", ".join(x["date"] for x in cal)
            return f"now {now_et().strftime('%Y-%m-%d %H:%M ET')} | market_open={clk['is_open']} next_open={clk['next_open']} next_close={clk['next_close']}\nupcoming trading days: {days}"

        self.add("get_market_clock", "Current time, whether the market is open, next open/close, upcoming trading days.", _schema({}), get_calendar)
