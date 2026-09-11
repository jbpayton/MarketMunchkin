"""Market data: Alpaca stock/option/news/screener feeds plus yfinance fundamentals."""
from __future__ import annotations

import datetime as dt
import logging
import re
import warnings
from typing import Any

import pandas as pd
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import NewsClient, OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import (MarketMoversRequest, MostActivesRequest, NewsRequest, OptionChainRequest,
                                  OptionSnapshotRequest, StockBarsRequest, StockSnapshotRequest)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from .config import alpaca_credentials
from .indicators import add_indicators, summarize_latest
from .util import ET, fmt_ts, fnum, now_et, parse_occ

log = logging.getLogger("munchkin.market")
warnings.filterwarnings("ignore", category=UserWarning)

_TF = {
    "1D": (TimeFrame.Day, 1.65, 0),
    "1W": (TimeFrame.Week, 8.0, 0),
    "1H": (TimeFrame.Hour, 1.0 / 6.5 * 1.6, 3),
    "30Min": (TimeFrame(30, TimeFrameUnit.Minute), 1.0 / 13 * 1.6, 3),
    "15Min": (TimeFrame(15, TimeFrameUnit.Minute), 1.0 / 26 * 1.6, 3),
    "5Min": (TimeFrame(5, TimeFrameUnit.Minute), 1.0 / 78 * 1.6, 2),
}


class Market:
    def __init__(self) -> None:
        key, secret, _ = alpaca_credentials()
        self.stocks = StockHistoricalDataClient(key, secret)
        self.options = OptionHistoricalDataClient(key, secret)
        self.newsc = NewsClient(key, secret)
        self.scr = ScreenerClient(key, secret)
        self._fund_cache: dict[str, tuple[dt.date, dict]] = {}

    # ------------------------------------------------------------------ quotes
    def snapshots(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        symbols = [s.upper().strip() for s in symbols if s.strip()]
        iex: dict = {}
        dl: dict = {}
        try:
            iex = self.stocks.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX))
        except Exception as e:
            log.warning("iex snapshot failed: %s", e)
        try:
            dl = self.stocks.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols, feed=DataFeed.DELAYED_SIP))
        except Exception as e:
            log.warning("delayed sip snapshot failed: %s", e)
        out: dict[str, dict[str, Any]] = {}
        if not iex and not dl and symbols:
            # both Alpaca feeds are down (seen as "backend request timeout" on an otherwise normal day): yfinance keeps
            # prices flowing for the watcher, armed entries and the dashboard, tagged so nobody mistakes it for the tape
            return self._snapshots_yfinance(symbols)
        for s in symbols:
            a, b = iex.get(s), dl.get(s)
            if a is None and b is None:
                out[s] = {"symbol": s, "error": "no data (unknown or untradable symbol?)"}
                continue
            cands = []
            if a is not None and a.latest_trade:
                cands.append((a.latest_trade.timestamp, float(a.latest_trade.price), "iex-realtime"))
            if b is not None and b.latest_trade:
                cands.append((b.latest_trade.timestamp, float(b.latest_trade.price), "sip-15m-delayed"))
            cands.sort(key=lambda x: x[0])
            ts, price, src = cands[-1] if cands else (None, None, None)
            daily = (b.daily_bar if b is not None and b.daily_bar else (a.daily_bar if a is not None else None))
            prev = (b.previous_daily_bar if b is not None and b.previous_daily_bar else (a.previous_daily_bar if a is not None else None))
            # Right after the open the delayed feed's "daily" bar can still be yesterday's: shift it to prev.
            today = now_et().date()
            if daily is not None and daily.timestamp.astimezone(ET).date() < today:
                prev, daily = daily, None
            quote = (b.latest_quote if b is not None and b.latest_quote else (a.latest_quote if a is not None else None))
            prev_close = float(prev.close) if prev else None
            rec: dict[str, Any] = {
                "symbol": s, "price": price, "price_time": fmt_ts(ts), "price_src": src,
                "bid": fnum(quote.bid_price) if quote else None, "ask": fnum(quote.ask_price) if quote else None,
                "quote_time": fmt_ts(quote.timestamp) if quote else None,
                "day_open": fnum(daily.open) if daily else None, "day_high": fnum(daily.high) if daily else None,
                "day_low": fnum(daily.low) if daily else None, "day_vol": int(daily.volume) if daily else None,
                "prev_close": fnum(prev_close),
                "chg_pct": None if not (price and prev_close) else round((price / prev_close - 1) * 100, 2),
            }
            out[s] = rec
        return out

    def _snapshots_yfinance(self, symbols: list[str], budget_s: float = 25.0) -> dict[str, dict[str, Any]]:
        import time as _t
        import yfinance as yf
        out: dict[str, dict[str, Any]] = {}
        t0 = _t.time()
        for s in symbols:
            if _t.time() - t0 > budget_s:
                out[s] = {"symbol": s, "error": "fallback quote budget exhausted"}
                continue
            try:
                fi = yf.Ticker(s.replace(".", "-")).fast_info
                g = lambda k: getattr(fi, k, None)  # noqa: E731  (FastInfo exposes values as attributes; .get() returns None)
                price, prev = fnum(g("last_price")), fnum(g("previous_close"))
                out[s] = {"symbol": s, "price": price, "price_time": now_et().strftime("%H:%M"), "price_src": "yfinance-fallback",
                          "bid": None, "ask": None, "quote_time": None, "day_open": fnum(g("open")), "day_high": fnum(g("day_high")),
                          "day_low": fnum(g("day_low")), "day_vol": int(g("last_volume") or 0) or None, "prev_close": prev,
                          "chg_pct": None if not (price and prev) else round((price / prev - 1) * 100, 2)}
            except Exception as e:
                out[s] = {"symbol": s, "error": f"no data ({str(e)[:60]})"}
        log.warning("snapshots served by yfinance fallback for %d symbols", len(symbols))
        return out

    def price(self, symbol: str) -> float | None:
        return self.snapshots([symbol]).get(symbol.upper(), {}).get("price")

    # ------------------------------------------------------------------ bars
    def bars(self, symbols: list[str] | str, timeframe: str = "1D", limit: int = 250,
             start: dt.datetime | None = None) -> pd.DataFrame:
        tf, days_per_bar, pad = _TF[timeframe]
        if start is None:
            start = now_et() - dt.timedelta(days=limit * days_per_bar + pad + 2)
        end = now_et() - dt.timedelta(minutes=16)  # SIP historical is allowed only >15 min old
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=tf, start=start, end=end, feed=DataFeed.SIP,
                               adjustment="split")
        df = None
        for _ in range(6):
            try:
                df = self.stocks.get_stock_bars(req).df
                break
            except Exception as e:
                msg = str(e)
                m = re.search(r"invalid symbol: ([A-Z0-9\.\-]+)", msg)
                if m and isinstance(req.symbol_or_symbols, list) and m.group(1) in req.symbol_or_symbols:
                    log.warning("dropping invalid symbol %s", m.group(1))
                    req.symbol_or_symbols = [s for s in req.symbol_or_symbols if s != m.group(1)]
                    if not req.symbol_or_symbols:
                        return pd.DataFrame()
                    continue
                if req.feed == DataFeed.SIP:
                    log.warning("sip bars failed (%s); retrying with iex", msg[:120])
                    req.feed = DataFeed.IEX
                    continue
                raise
        if df is None:
            return pd.DataFrame()
        if df.empty:
            return df
        if isinstance(symbols, str):
            df = df.xs(symbols.upper(), level="symbol") if "symbol" in df.index.names else df
            df = df.tail(limit)
        return df

    def bars_with_indicators(self, symbol: str, timeframe: str = "1D", limit: int = 250) -> tuple[pd.DataFrame, dict]:
        df = self.bars(symbol.upper(), timeframe, limit)
        if df.empty:
            return df, {}
        d = add_indicators(df)
        return d, summarize_latest(d)

    # ------------------------------------------------------------------ options
    def option_chain(self, underlying: str, dte_min: int = 5, dte_max: int = 45, moneyness_pct: float = 8.0,
                     contract_type: str | None = None, oi_map: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        underlying = underlying.upper()
        px = self.price(underlying)
        if not px:
            return []
        today = now_et().date()
        lo, hi = px * (1 - moneyness_pct / 100), px * (1 + moneyness_pct / 100)
        req = OptionChainRequest(underlying_symbol=underlying, feed=OptionsFeed.INDICATIVE,
                                 type=contract_type, strike_price_gte=round(lo, 2), strike_price_lte=round(hi, 2),
                                 expiration_date_gte=today + dt.timedelta(days=dte_min),
                                 expiration_date_lte=today + dt.timedelta(days=dte_max))
        chain = self.options.get_option_chain(req)
        rows = [self._snap_row(sym, snap, px, today, oi_map) for sym, snap in chain.items()]
        rows = [r for r in rows if r]
        rows.sort(key=lambda r: (r["exp"], r["type"], r["strike"]))
        return rows

    def option_snapshots(self, symbols: list[str], oi_map: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        symbols = [s.upper() for s in symbols]
        snaps = self.options.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=symbols, feed=OptionsFeed.INDICATIVE))
        today = now_et().date()
        px_cache: dict[str, float | None] = {}
        rows = []
        for sym in symbols:
            snap = snaps.get(sym)
            p = parse_occ(sym)
            if snap is None or p is None:
                rows.append({"symbol": sym, "error": "no snapshot"})
                continue
            u = p["underlying"]
            if u not in px_cache:
                px_cache[u] = self.price(u)
            rows.append(self._snap_row(sym, snap, px_cache[u] or 0.0, today, oi_map) or {"symbol": sym, "error": "parse"})
        return rows

    @staticmethod
    def _snap_row(sym: str, snap: Any, px: float, today: dt.date, oi_map: dict[str, Any] | None) -> dict[str, Any] | None:
        p = parse_occ(sym)
        if not p:
            return None
        q = snap.latest_quote
        bid = float(q.bid_price) if q and q.bid_price is not None else None
        ask = float(q.ask_price) if q and q.ask_price is not None else None
        mid = round((bid + ask) / 2, 3) if bid is not None and ask is not None else None
        spread_pct = round((ask - bid) / mid * 100, 1) if mid and bid is not None and ask is not None else None
        g = snap.greeks
        oi = None
        if oi_map and sym in oi_map:
            try:
                oi = int(float(oi_map[sym].get("open_interest") or 0))
            except (TypeError, ValueError):
                oi = None
        return {
            "symbol": sym, "type": "C" if p["type"] == "call" else "P", "strike": p["strike"],
            "exp": p["expiration"].isoformat(), "dte": (p["expiration"] - today).days,
            "moneyness_pct": round((p["strike"] / px - 1) * 100, 1) if px else None,
            "bid": bid, "ask": ask, "mid": mid, "spread_pct": spread_pct,
            "last": float(snap.latest_trade.price) if snap.latest_trade else None,
            "quote_time": fmt_ts(q.timestamp) if q else None,
            "iv": fnum(snap.implied_volatility, 3),
            "delta": fnum(g.delta, 3) if g else None, "gamma": fnum(g.gamma, 4) if g else None,
            "theta": fnum(g.theta, 3) if g else None, "vega": fnum(g.vega, 3) if g else None,
            "oi": oi,
        }

    # ------------------------------------------------------------------ news / movers
    def news(self, symbols: list[str] | None = None, limit: int = 10, hours: int = 48) -> list[dict[str, Any]]:
        req = NewsRequest(symbols=",".join(s.upper() for s in symbols) if symbols else None, limit=min(limit, 50),
                          start=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours), include_content=False,
                          exclude_contentless=False)
        res = self.newsc.get_news(req)
        items = res.data.get("news", []) if hasattr(res, "data") else []
        out = []
        for a in items[:limit]:
            out.append({"time": fmt_ts(a.created_at), "headline": a.headline[:160], "source": a.source,
                        "symbols": ",".join(a.symbols[:6]) if a.symbols else "", "url": a.url,
                        "summary": (a.summary or "")[:240], "engine": "benzinga"})
        try:  # second source: Finnhub company news (free tier), merged and de-duplicated by headline
            from . import providers as P
            if symbols and len(symbols) <= 3 and P.enabled("finnhub"):
                seen = {o["headline"][:60].lower() for o in out}
                for s_ in symbols:
                    for a in P.finnhub_company_news(s_, days=max(1, hours // 24 + 1), limit=limit):
                        if a["headline"][:60].lower() not in seen:
                            out.append(a)
                            seen.add(a["headline"][:60].lower())
            elif not symbols and P.enabled("finnhub"):
                seen = {o["headline"][:60].lower() for o in out}
                for a in P.finnhub_market_news(limit=8):
                    if a["headline"][:60].lower() not in seen:
                        out.append(a)
        except Exception as e:
            log.warning("finnhub news merge failed: %s", e)
        out.sort(key=lambda x: x.get("time") or "", reverse=True)
        return out[: max(limit, 12)]

    def movers(self, top: int = 10) -> dict[str, list[dict[str, Any]]]:
        mv = self.scr.get_market_movers(MarketMoversRequest(top=min(top, 50)))
        f = lambda xs: [{"symbol": x.symbol, "price": fnum(x.price), "chg_pct": fnum(x.percent_change, 1), "chg": fnum(x.change)} for x in xs]
        return {"gainers": f(mv.gainers), "losers": f(mv.losers)}

    def most_actives(self, top: int = 10) -> list[dict[str, Any]]:
        ma = self.scr.get_most_actives(MostActivesRequest(top=min(top, 100)))
        return [{"symbol": x.symbol, "volume": int(x.volume), "trades": int(x.trade_count)} for x in ma.most_actives]

    # ------------------------------------------------------------------ vix / news attention
    _vix_cache: tuple[float, dict] | None = None

    def vix(self) -> dict[str, Any]:
        import time as _t
        if self._vix_cache and _t.time() - self._vix_cache[0] < 1800:
            return self._vix_cache[1]
        out: dict[str, Any] = {}
        try:
            import yfinance as yf
            v = yf.Ticker("^VIX").history(period="3mo")["Close"].dropna()
            out["vix"] = round(float(v.iloc[-1]), 2)
            out["vix_chg_1d_pct"] = round(float(v.iloc[-1] / v.iloc[-2] - 1) * 100, 1) if len(v) > 1 else None
            out["vix_pct_rank_3m"] = round(float((v <= v.iloc[-1]).mean() * 100), 0)
            v3 = yf.Ticker("^VIX3M").history(period="1mo")["Close"].dropna()
            if len(v3):
                out["vix3m"] = round(float(v3.iloc[-1]), 2)
        except Exception as e:
            out["error"] = str(e)[:100]
        self._vix_cache = (_t.time(), out)
        return out

    _news_cache: tuple[float, dict] | None = None

    def news_counts(self, hours: int = 24, max_pages: int = 8) -> dict[str, int]:
        """Headline count per symbol over the window (attention/crowding signal)."""
        import time as _t
        if self._news_cache and _t.time() - self._news_cache[0] < 1800:
            return self._news_cache[1]
        counts: dict[str, int] = {}
        token = None
        for _ in range(max_pages):
            req = NewsRequest(limit=50, start=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours), include_content=False, page_token=token)
            try:
                res = self.newsc.get_news(req)
            except Exception:
                break
            for a in res.data.get("news", []):
                for sym in (a.symbols or [])[:6]:
                    counts[sym] = counts.get(sym, 0) + 1
            token = getattr(res, "next_page_token", None) or res.data.get("next_page_token")
            if not token:
                break
        self._news_cache = (_t.time(), counts)
        return counts

    # ------------------------------------------------------------------ option analytics
    def option_analytics(self, underlying: str, oi_map: dict[str, Any] | None = None, rv20_pct: float | None = None) -> dict[str, Any]:
        """ATM IV by expiry, IV vs realized, expected move, 25-delta skew, OI walls / put-call ratio."""
        underlying = underlying.upper()
        px = self.price(underlying)
        if not px:
            return {"error": f"no price for {underlying}"}
        rows = self.option_chain(underlying, 5, 60, 12.0, None, oi_map=oi_map)
        if not rows:
            return {"error": "no option chain"}
        out: dict[str, Any] = {"underlying": underlying, "price": px, "rv20_pct": rv20_pct}
        by_exp: dict[str, list[dict]] = {}
        for r in rows:
            by_exp.setdefault(r["exp"], []).append(r)
        exps = sorted(by_exp)
        near = next((e for e in exps if by_exp[e][0]["dte"] >= 7), exps[0])
        month = min(exps, key=lambda e: abs(by_exp[e][0]["dte"] - 30))
        out["expiries"] = {}
        for label, e in (("near", near), ("month", month)):
            cs = by_exp[e]
            calls = [r for r in cs if r["type"] == "C" and r["mid"]]
            puts = [r for r in cs if r["type"] == "P" and r["mid"]]
            if not calls or not puts:
                continue
            atm_c = min(calls, key=lambda r: abs(r["strike"] - px))
            atm_p = min(puts, key=lambda r: abs(r["strike"] - px))
            ivs = [x["iv"] for x in (atm_c, atm_p) if x.get("iv")]
            atm_iv = round(sum(ivs) / len(ivs), 3) if ivs else None
            straddle = round(atm_c["mid"] + atm_p["mid"], 2)
            dte = cs[0]["dte"]
            info = {"exp": e, "dte": dte, "atm_strike": atm_c["strike"], "atm_iv": atm_iv,
                    "straddle_mid": straddle, "expected_move_pct": round(straddle / px * 100, 2),
                    "expected_move_iv_pct": round(atm_iv * (dte / 365) ** 0.5 * 100, 2) if atm_iv else None,
                    "atm_call": {"symbol": atm_c["symbol"], "bid": atm_c["bid"], "ask": atm_c["ask"], "delta": atm_c["delta"], "oi": atm_c.get("oi")},
                    "atm_put": {"symbol": atm_p["symbol"], "bid": atm_p["bid"], "ask": atm_p["ask"], "delta": atm_p["delta"], "oi": atm_p.get("oi")}}
            c25 = min((r for r in calls if r.get("delta") is not None), key=lambda r: abs(r["delta"] - 0.25), default=None)
            p25 = min((r for r in puts if r.get("delta") is not None), key=lambda r: abs(r["delta"] + 0.25), default=None)
            if c25 and p25 and c25.get("iv") and p25.get("iv"):
                info["skew_25d_put_minus_call"] = round(p25["iv"] - c25["iv"], 3)
            call_oi = sum(int(r.get("oi") or 0) for r in calls)
            put_oi = sum(int(r.get("oi") or 0) for r in puts)
            info["put_call_oi_ratio"] = round(put_oi / call_oi, 2) if call_oi else None
            walls = sorted(cs, key=lambda r: -(int(r.get("oi") or 0)))[:3]
            info["oi_walls"] = [f"{w['type']}{w['strike']:g}:{w.get('oi')}" for w in walls if w.get("oi")]
            out["expiries"][label] = info
        m = out["expiries"].get("month") or out["expiries"].get("near")
        if m and m.get("atm_iv") and rv20_pct:
            out["iv30_pct"] = round(m["atm_iv"] * 100, 1)
            out["iv_over_rv"] = round(m["atm_iv"] * 100 / rv20_pct, 2)
            out["premium_read"] = "rich" if out["iv_over_rv"] > 1.3 else "cheap" if out["iv_over_rv"] < 0.9 else "fair"
        return out

    # ------------------------------------------------------------------ fundamentals (yfinance)
    def fundamentals(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        today = now_et().date()
        if symbol in self._fund_cache and self._fund_cache[symbol][0] == today:
            return self._fund_cache[symbol][1]
        try:
            import yfinance as yf
            t = yf.Ticker(symbol)
            info = t.info or {}
            keys = ["shortName", "sector", "industry", "marketCap", "trailingPE", "forwardPE", "priceToSalesTrailing12Months",
                    "shortPercentOfFloat", "floatShares", "sharesOutstanding", "averageVolume", "beta",
                    "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "revenueGrowth", "earningsGrowth", "profitMargins",
                    "totalCash", "totalDebt", "targetMeanPrice", "recommendationKey", "numberOfAnalystOpinions"]
            out: dict[str, Any] = {k: info.get(k) for k in keys if info.get(k) is not None}
            for k in ("marketCap", "floatShares", "sharesOutstanding", "totalCash", "totalDebt"):
                if k in out:
                    out[k] = f"{out[k] / 1e9:.2f}B" if out[k] >= 1e9 else f"{out[k] / 1e6:.1f}M"
            for k in ("shortPercentOfFloat", "revenueGrowth", "earningsGrowth", "profitMargins"):
                if k in out:
                    out[k] = f"{out[k] * 100:.1f}%"
            try:
                cal = t.calendar or {}
                ed = cal.get("Earnings Date")
                if ed:
                    out["next_earnings"] = ", ".join(str(x) for x in (ed if isinstance(ed, list) else [ed]))
                if cal.get("Ex-Dividend Date"):
                    out["ex_dividend"] = str(cal["Ex-Dividend Date"])
            except Exception:
                pass
        except Exception as e:
            out = {"error": f"fundamentals unavailable: {type(e).__name__}: {str(e)[:120]}"}
        try:
            from . import providers as P
            fm = P.finnhub_metrics(symbol)
            if fm:
                out["finnhub"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in fm.items()}
        except Exception as e:
            log.warning("finnhub metrics failed: %s", e)
        self._fund_cache[symbol] = (today, out)
        return out
