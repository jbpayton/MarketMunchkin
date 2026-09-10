"""Quant helpers for screening: time-series screen columns, cross-sectional ranks, relative strength,
sector tables, breadth/regime, setup backtests, intraday table, position sizing. Pure pandas/numpy."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

import numpy as np
import pandas as pd

from .indicators import add_indicators

SECTOR_ETF = {
    "Information Technology": "XLK", "Financials": "XLF", "Energy": "XLE", "Health Care": "XLV", "Industrials": "XLI",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Utilities": "XLU", "Materials": "XLB",
    "Real Estate": "XLRE", "Communication Services": "XLC",
}


# ------------------------------------------------------------------ per-symbol time series
def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    up = h.diff()
    dn = -l.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / n, min_periods=n, adjust=False).mean() / atr_.replace(0, np.nan)
    mdi = 100 * minus_dm.ewm(alpha=1 / n, min_periods=n, adjust=False).mean() / atr_.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()


def screen_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Indicator columns as full time series, named exactly like the screener table columns."""
    d = add_indicators(df)
    c = d["close"].astype(float)
    f = pd.DataFrame(index=d.index)
    f["price"] = c
    for n, col in ((1, "chg_1d_pct"), (5, "chg_5d_pct"), (20, "chg_20d_pct"), (60, "chg_60d_pct")):
        f[col] = (c / c.shift(n) - 1) * 100
    f["rsi14"] = d["rsi14"]
    for col in ("sma20", "sma50", "sma200", "ema9", "ema21", "hi20", "lo20", "hi52", "lo52"):
        f[f"{col}_dist_pct"] = (c / d[col] - 1) * 100
    f["atr14_pct"] = d["atr14"] / c * 100
    f["rvol20_pct"] = d["rvol20"]
    f["macd_hist"] = d["macd_hist"]
    bw = d["bb_upper"] - d["bb_lower"]
    f["bb_pos"] = (c - d["bb_lower"]) / bw.replace(0, np.nan)
    f["bb_width_pct"] = bw / d["sma20"] * 100
    f["bb_squeeze"] = f["bb_width_pct"].rolling(120, min_periods=40).rank(pct=True) * 100  # low = tight
    f["vol_ratio"] = d["volume"] / d["avg_vol20"].replace(0, np.nan)
    f["avg_dollar_vol20_m"] = d["avg_vol20"] * c / 1e6
    f["gap_pct"] = (d["open"] / c.shift(1) - 1) * 100
    rng = (d["high"] - d["low"]).replace(0, np.nan)
    f["range_pos"] = (c - d["low"]) / rng
    f["adx14"] = adx(d, 14)
    f["z20_atr"] = (c - d["sma20"]) / d["atr14"].replace(0, np.nan)
    f["up_days_5"] = (c.diff() > 0).rolling(5).sum()
    f["ret1"] = d["ret1"]
    return f


# ------------------------------------------------------------------ cross-sectional
def _pct_rank(s: pd.Series) -> pd.Series:
    return (s.rank(pct=True) * 100).round(0)


def add_cross_sectional(table: pd.DataFrame, spy: dict[str, float] | None, sectors: dict[str, str],
                        etf_rows: pd.DataFrame | None) -> pd.DataFrame:
    t = table.copy()
    t["mom_3m_rank"] = _pct_rank(t["chg_60d_pct"])
    t["mom_1m_rank"] = _pct_rank(t["chg_20d_pct"])
    t["mom_1w_rank"] = _pct_rank(t["chg_5d_pct"])
    t["volsurge_rank"] = _pct_rank(t["vol_ratio"])
    t["rvol_rank"] = _pct_rank(t["rvol20_pct"])
    # composite momentum: 3m strength that is not short-term overbought
    t["mom_score"] = (0.5 * t["mom_3m_rank"] + 0.3 * t["mom_1m_rank"] + 0.2 * (100 - _pct_rank((t["sma20_dist_pct"] / t["atr14_pct"]).abs()))).round(0)
    if spy:
        t["rs_spy_20d"] = (t["chg_20d_pct"] - spy.get("chg_20d_pct", 0.0)).round(2)
        t["rs_spy_60d"] = (t["chg_60d_pct"] - spy.get("chg_60d_pct", 0.0)).round(2)
    t["sector"] = [sectors.get(s, "ETF" if bool(t.loc[s, "is_etf"]) else "Other") for s in t.index]
    if etf_rows is not None and len(etf_rows):
        def rs_sec(sym):
            etf = SECTOR_ETF.get(sectors.get(sym, ""))
            if etf and etf in etf_rows.index:
                return round(float(t.loc[sym, "chg_20d_pct"] - etf_rows.loc[etf, "chg_20d_pct"]), 2)
            return None
        t["rs_sector_20d"] = [rs_sec(s) for s in t.index]
    return t


def beta_corr(returns: pd.DataFrame, bench: str = "SPY", window: int = 60) -> pd.DataFrame:
    """returns: wide DataFrame of daily returns (columns = symbols). Returns beta & corr vs bench."""
    if bench not in returns.columns:
        return pd.DataFrame(columns=["beta_spy", "corr_spy"])
    r = returns.tail(window)
    b = r[bench]
    cov = r.apply(lambda col: col.cov(b))
    beta = cov / b.var()
    corr = r.corrwith(b)
    return pd.DataFrame({"beta_spy": beta.round(2), "corr_spy": corr.round(2)})


def sector_table(table: pd.DataFrame, etf_rows: pd.DataFrame | None) -> pd.DataFrame:
    t = table[table["sector"].notna() & (table["sector"] != "ETF") & (table["sector"] != "Other")]
    g = t.groupby("sector")
    out = pd.DataFrame({
        "n": g.size(),
        "chg_1d": g["chg_1d_pct"].mean().round(2),
        "chg_5d": g["chg_5d_pct"].mean().round(2),
        "chg_20d": g["chg_20d_pct"].mean().round(2),
        "chg_60d": g["chg_60d_pct"].mean().round(2),
        "pct_above_50d": (g["sma50_dist_pct"].apply(lambda s: (s > 0).mean()) * 100).round(0),
        "pct_above_200d": (g["sma200_dist_pct"].apply(lambda s: (s > 0).mean()) * 100).round(0),
        "avg_rsi": g["rsi14"].mean().round(0),
    })
    out["etf"] = [SECTOR_ETF.get(s) for s in out.index]
    if etf_rows is not None:
        out["etf_chg_20d"] = [round(float(etf_rows.loc[e, "chg_20d_pct"]), 2) if e in etf_rows.index else None for e in out["etf"]]
    return out.sort_values("chg_20d", ascending=False)


def breadth(table: pd.DataFrame) -> dict[str, Any]:
    t = table[~table["is_etf"].astype(bool)]
    n = max(1, len(t))
    return {
        "n": len(t),
        "pct_above_sma50": round(float((t["sma50_dist_pct"] > 0).mean() * 100), 1),
        "pct_above_sma200": round(float((t["sma200_dist_pct"] > 0).mean() * 100), 1),
        "pct_above_sma20": round(float((t["sma20_dist_pct"] > 0).mean() * 100), 1),
        "pct_rsi_gt_50": round(float((t["rsi14"] > 50).mean() * 100), 1),
        "new_20d_highs": int((t["hi20_dist_pct"] >= -0.1).sum()),
        "new_20d_lows": int((t["lo20_dist_pct"] <= 0.1).sum()),
        "advancers": int((t["chg_1d_pct"] > 0).sum()), "decliners": int((t["chg_1d_pct"] < 0).sum()),
        "avg_chg_1d": round(float(t["chg_1d_pct"].mean()), 2),
        "median_chg_20d": round(float(t["chg_20d_pct"].median()), 2),
    }


def regime(table: pd.DataFrame, vix: dict[str, Any] | None) -> dict[str, Any]:
    """Transparent heuristic: trend (SPY vs MAs), breadth, volatility (VIX level + term), momentum."""
    b = breadth(table)
    spy = table.loc["SPY"] if "SPY" in table.index else None
    score = 50.0
    parts: dict[str, Any] = {}
    if spy is not None:
        trend = 0
        trend += 1 if spy["sma50_dist_pct"] > 0 else -1
        trend += 1 if spy["sma200_dist_pct"] > 0 else -1
        parts["spy_trend"] = "up" if trend > 0 else "down" if trend < 0 else "mixed"
        score += 6 * trend                                    # -12..+12
        mom = float(np.clip(spy["chg_20d_pct"] * 2, -10, 10))  # -10..+10
        parts["spy_chg_20d"] = round(float(spy["chg_20d_pct"]), 2)
        score += mom
        parts["spy_dist_20d_high_pct"] = round(float(spy["hi20_dist_pct"]), 2)
    score += (b["pct_above_sma50"] - 50) * 0.5                # -25..+25
    score += (b["pct_rsi_gt_50"] - 50) * 0.3                  # -15..+15
    parts["breadth_above_50d"] = b["pct_above_sma50"]
    parts["breadth_rsi_gt_50"] = b["pct_rsi_gt_50"]
    if vix and vix.get("vix"):
        v = float(vix["vix"])
        parts["vix"] = v
        if v < 15:
            score += 5
            parts["vol_regime"] = "low"
        elif v > 25:
            score -= 15
            parts["vol_regime"] = "high"
        else:
            parts["vol_regime"] = "normal"
        if vix.get("vix3m"):
            term = v / float(vix["vix3m"])
            parts["vix_term_ratio"] = round(term, 3)
            if term > 1.0:
                score -= 10
                parts["vol_term"] = "backwardation (stress)"
            else:
                parts["vol_term"] = "contango"
        if vix.get("vix_chg_1d_pct") is not None and abs(vix["vix_chg_1d_pct"]) > 15:
            parts["vix_spike_1d_pct"] = vix["vix_chg_1d_pct"]
            score -= 5 if vix["vix_chg_1d_pct"] > 0 else 0
    score = float(np.clip(score, 0, 100))
    label = "risk-on" if score >= 60 else "risk-off" if score <= 40 else "neutral"
    return {"score": round(score), "label": label, "components": parts, "breadth": b}


# ------------------------------------------------------------------ setup backtest
def backtest_setups(frames: dict[str, pd.DataFrame], setups: dict[str, tuple[str, str, bool]], is_etf: set[str],
                    horizons: tuple[int, ...] = (5, 10, 20), min_gap_days: int = 5) -> dict[str, Any]:
    """Evaluate each setup expression on every historical day and measure forward returns.
    Signals within `min_gap_days` of a prior signal on the same symbol are skipped (overlap control)."""
    per_setup: dict[str, list[pd.DataFrame]] = {k: [] for k in setups}
    base_rows: list[pd.DataFrame] = []
    for sym, f in frames.items():
        if len(f) < 80:
            continue
        g = f.copy()
        g["is_etf"] = sym in is_etf
        c = g["price"]
        for h in horizons:
            g[f"fwd{h}"] = (c.shift(-h) / c - 1) * 100
        lo = g["_low"] if "_low" in g else None
        if lo is not None:
            g["mae10"] = (lo.rolling(10).min().shift(-10) / c - 1) * 100
        sub = g[[f"fwd{h}" for h in horizons]].dropna()
        if len(sub):
            base_rows.append(sub.sample(min(200, len(sub)), random_state=1))
        for name, (expr, _, _) in setups.items():
            try:
                hit = g.query(expr, engine="python")
            except Exception:
                continue
            if hit.empty:
                continue
            keep_idx = []
            last = None
            for ts in hit.index:
                if last is None or (ts - last).days >= min_gap_days:
                    keep_idx.append(ts)
                    last = ts
            h = hit.loc[keep_idx].copy()
            h["symbol"] = sym
            per_setup[name].append(h)
    base = pd.concat(base_rows) if base_rows else pd.DataFrame()
    out: dict[str, Any] = {"generated": dt.datetime.now().isoformat(timespec="minutes"), "horizons": list(horizons), "setups": {}}
    if not base.empty:
        out["baseline_all_days"] = {f"fwd{h}_mean": round(float(base[f"fwd{h}"].mean()), 2) for h in horizons} | {
            "fwd10_win_rate": round(float((base["fwd10"] > 0).mean() * 100), 1)}
    for name, lst in per_setup.items():
        if not lst:
            out["setups"][name] = {"n": 0}
            continue
        h = pd.concat(lst)
        h = h.dropna(subset=[f"fwd{horizons[-1]}"])
        if h.empty:
            out["setups"][name] = {"n": 0}
            continue
        stats: dict[str, Any] = {"n": int(len(h)), "symbols": int(h["symbol"].nunique())}
        for hz in horizons:
            col = f"fwd{hz}"
            stats[f"fwd{hz}_mean"] = round(float(h[col].mean()), 2)
            stats[f"fwd{hz}_median"] = round(float(h[col].median()), 2)
            stats[f"fwd{hz}_win"] = round(float((h[col] > 0).mean() * 100), 1)
        if "mae10" in h:
            stats["mae10_mean"] = round(float(h["mae10"].mean()), 2)
            stats["mae10_p10"] = round(float(h["mae10"].quantile(0.10)), 2)
        stats["fwd10_p10"] = round(float(h["fwd10"].quantile(0.10)), 2)
        stats["fwd10_p90"] = round(float(h["fwd10"].quantile(0.90)), 2)
        by_year = h.groupby(h.index.year)["fwd10"].agg(["count", "mean", lambda s: (s > 0).mean() * 100])
        stats["by_year"] = {str(y): {"n": int(r.iloc[0]), "fwd10_mean": round(float(r.iloc[1]), 2), "win": round(float(r.iloc[2]), 1)} for y, r in by_year.iterrows()}
        out["setups"][name] = stats
    return out


def stats_line(name: str, s: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    if not s or not s.get("n"):
        return f"{name}: no historical signals"
    edge = ""
    if baseline and "fwd10_mean" in baseline:
        edge = f", edge vs all-days {s['fwd10_mean'] - baseline['fwd10_mean']:+.2f}pp"
    return (f"{name}: n={s['n']} ({s['symbols']} names) | 5d {s['fwd5_mean']:+.2f}% | 10d {s['fwd10_mean']:+.2f}% win {s['fwd10_win']}%"
            f" | 20d {s['fwd20_mean']:+.2f}% | 10d worst-decile {s['fwd10_p10']:+.1f}%" + (f" | avg MAE10 {s['mae10_mean']:+.1f}%" if "mae10_mean" in s else "") + edge)


# ------------------------------------------------------------------ intraday
def volume_profile(bars_5m: pd.DataFrame) -> list[float]:
    """Average cumulative volume fraction per 5-minute slot of the regular session (78 slots)."""
    if bars_5m.empty:
        return [min(1.0, (i + 1) / 78) for i in range(78)]
    df = bars_5m.copy()
    idx = df.index.get_level_values("timestamp") if "timestamp" in df.index.names else df.index
    ts = pd.DatetimeIndex(idx).tz_convert("America/New_York")
    df = df.reset_index(drop=True)
    df["date"] = ts.date
    minutes = ts.hour * 60 + ts.minute - (9 * 60 + 30)
    df["slot"] = (minutes // 5).astype(int)
    df = df[(df["slot"] >= 0) & (df["slot"] < 78)]
    if "symbol" in bars_5m.index.names:
        df["symbol"] = bars_5m.index.get_level_values("symbol")[df.index] if len(df) == len(bars_5m) else "X"
    key = ["date"] + (["symbol"] if "symbol" in df else [])
    tot = df.groupby(key)["volume"].transform("sum")
    df["frac"] = df["volume"] / tot.replace(0, np.nan)
    prof = df.groupby("slot")["frac"].mean().reindex(range(78)).fillna(1 / 78)
    cum = prof.cumsum() / prof.sum()
    return [round(float(x), 4) for x in cum]


def session_fraction(now: dt.datetime, profile: list[float]) -> float | None:
    m = now.hour * 60 + now.minute - (9 * 60 + 30)
    if m < 0:
        return None
    slot = min(77, m // 5)
    return max(0.02, profile[slot])


def intraday_table(snaps: dict[str, Any], daily: pd.DataFrame, profile: list[float], now: dt.datetime,
                   news_counts: dict[str, int] | None = None) -> pd.DataFrame:
    """Live table from a universe snapshot merged with the daily context table."""
    frac = session_fraction(now, profile)
    rows = []
    spy = snaps.get("SPY")
    spy_chg = None
    if spy is not None and spy.daily_bar and spy.previous_daily_bar:
        spy_chg = (float(spy.daily_bar.close) / float(spy.previous_daily_bar.close) - 1) * 100
    for sym, s in snaps.items():
        db, pb = s.daily_bar, s.previous_daily_bar
        if db is None or pb is None:
            continue
        if db.timestamp.astimezone(dt.timezone.utc).date() != now.astimezone(dt.timezone.utc).date() and now.hour >= 9:
            continue  # stale bar (not today's)
        price = float(s.latest_trade.price) if s.latest_trade else float(db.close)
        o, h, l, v = float(db.open), float(db.high), float(db.low), float(db.volume)
        pc = float(pb.close)
        rng = h - l
        r: dict[str, Any] = {
            "symbol": sym, "price": round(price, 3), "chg_today_pct": round((price / pc - 1) * 100, 2),
            "gap_pct": round((o / pc - 1) * 100, 2), "from_open_pct": round((price / o - 1) * 100, 2),
            "range_pos": round((price - l) / rng, 2) if rng > 0 else None,
            "from_hod_pct": round((price / h - 1) * 100, 2), "from_lod_pct": round((price / l - 1) * 100, 2),
            "day_range_pct": round(rng / pc * 100, 2),
            "vwap_dist_pct": round((price / float(db.vwap) - 1) * 100, 2) if getattr(db, "vwap", None) else None,
            "day_vol": int(v),
        }
        if spy_chg is not None:
            r["rs_today"] = round(r["chg_today_pct"] - spy_chg, 2)
        if sym in daily.index:
            d = daily.loc[sym]
            avg_vol = float(d["avg_dollar_vol20_m"]) * 1e6 / float(d["price"]) if d["price"] else None
            r["rel_vol"] = round(v / (avg_vol * frac), 2) if (avg_vol and frac) else None
            for col in ("rsi14", "sma20_dist_pct", "sma50_dist_pct", "sma200_dist_pct", "hi52_dist_pct", "hi20_dist_pct", "atr14_pct",
                        "avg_dollar_vol20_m", "mom_1m_rank", "mom_3m_rank", "mom_score", "rs_spy_20d", "sector", "days_to_earnings", "is_etf", "adx14", "bb_squeeze"):
                if col in daily.columns:
                    r[col] = d[col]
        if news_counts:
            r["news_24h"] = int(news_counts.get(sym, 0))
        rows.append(r)
    t = pd.DataFrame(rows)
    if not t.empty:
        t = t.set_index("symbol", drop=False)
    return t


INTRADAY_SETUPS = {
    "gap_and_go": ("gap_pct > 3 and from_open_pct > 0 and rel_vol > 2 and avg_dollar_vol20_m > 20 and price > 5", "rel_vol", False),
    "hod_breakout": ("from_hod_pct > -0.3 and rel_vol > 1.5 and chg_today_pct > 1 and avg_dollar_vol20_m > 20", "rel_vol", False),
    "vwap_hold_strength": ("vwap_dist_pct > 0 and vwap_dist_pct < 2 and range_pos > 0.6 and rel_vol > 1.2 and rs_today > 1 and avg_dollar_vol20_m > 20", "rs_today", False),
    "gap_fade": ("gap_pct > 5 and from_open_pct < -1 and avg_dollar_vol20_m > 20", "from_open_pct", True),
    "volume_leaders": ("rel_vol > 3 and avg_dollar_vol20_m > 20 and price > 5", "rel_vol", False),
    "washout_reversal": ("chg_today_pct < -4 and range_pos > 0.7 and rel_vol > 1.5 and sma200_dist_pct > -10 and avg_dollar_vol20_m > 30", "chg_today_pct", True),
}


# ------------------------------------------------------------------ sizing
def position_size(equity: float, risk_pct: float, entry: float, stop: float | None, atr_pct: float | None,
                  cap_notional: float, settled_cash: float) -> dict[str, Any]:
    out: dict[str, Any] = {"equity": round(equity, 2), "risk_pct": risk_pct, "entry": entry}
    if stop is None and atr_pct:
        stop = round(entry * (1 - 1.5 * atr_pct / 100), 2)
        out["stop_suggested_1_5_atr"] = stop
    if stop is None or stop >= entry:
        return out | {"error": "need a stop below entry (or an ATR to suggest one)"}
    dist = entry - stop
    out["stop_distance_pct"] = round(dist / entry * 100, 2)
    risk_dollars = equity * risk_pct
    shares_by_risk = risk_dollars / dist
    notional_by_risk = shares_by_risk * entry
    cap = min(cap_notional, settled_cash)
    notional = min(notional_by_risk, cap)
    out.update({
        "risk_dollars": round(risk_dollars, 2), "shares_by_risk": round(shares_by_risk, 4), "notional_by_risk": round(notional_by_risk, 2),
        "cap_notional": round(cap_notional, 2), "settled_cash": round(settled_cash, 2), "notional": round(notional, 2),
        "shares": round(notional / entry, 4), "loss_at_stop": round(notional / entry * dist, 2),
        "binding": "risk" if notional_by_risk <= cap else ("settled_cash" if settled_cash < cap_notional else "position_cap"),
    })
    if atr_pct:
        out["atr14_pct"] = atr_pct
        out["stop_in_atr"] = round(out["stop_distance_pct"] / atr_pct, 2)
        if out["stop_in_atr"] < 1.0:
            out["warning"] = "stop is inside one ATR of daily noise; expect whipsaw"
    return out
