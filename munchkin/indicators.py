"""Technical indicators on OHLCV DataFrames (pandas, no external TA lib)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    ag = gain.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    al = loss.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(al != 0, 100.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df: single-symbol OHLCV indexed by time ascending. Returns a copy with indicator columns."""
    d = df.copy()
    c = d["close"].astype(float)
    d["sma20"] = c.rolling(20).mean()
    d["sma50"] = c.rolling(50).mean()
    d["sma200"] = c.rolling(200).mean()
    d["ema9"] = c.ewm(span=9, adjust=False).mean()
    d["ema21"] = c.ewm(span=21, adjust=False).mean()
    d["rsi14"] = rsi(c, 14)
    d["atr14"] = atr(d, 14)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    d["macd"] = ema12 - ema26
    d["macd_signal"] = d["macd"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = d["macd"] - d["macd_signal"]
    std20 = c.rolling(20).std()
    d["bb_upper"] = d["sma20"] + 2 * std20
    d["bb_lower"] = d["sma20"] - 2 * std20
    d["hi20"] = d["high"].rolling(20).max()
    d["lo20"] = d["low"].rolling(20).min()
    d["hi52"] = d["high"].rolling(252, min_periods=20).max()
    d["lo52"] = d["low"].rolling(252, min_periods=20).min()
    d["avg_vol20"] = d["volume"].rolling(20).mean()
    d["ret1"] = c.pct_change(1)
    d["rvol20"] = d["ret1"].rolling(20).std() * np.sqrt(252) * 100
    return d


def summarize_latest(d: pd.DataFrame) -> dict:
    """One-row snapshot of the latest indicator values, as percentages where sensible."""
    if d.empty:
        return {}
    last = d.iloc[-1]
    c = float(last["close"])

    def dist(col: str):
        v = last.get(col)
        return None if v is None or pd.isna(v) else round((c / float(v) - 1) * 100, 2)

    def chg(n: int):
        if len(d) <= n:
            return None
        p = float(d["close"].iloc[-1 - n])
        return round((c / p - 1) * 100, 2)

    out = {
        "price": round(c, 4),
        "chg_1d_pct": chg(1), "chg_5d_pct": chg(5), "chg_20d_pct": chg(20), "chg_60d_pct": chg(60),
        "rsi14": None if pd.isna(last["rsi14"]) else round(float(last["rsi14"]), 1),
        "sma20_dist_pct": dist("sma20"), "sma50_dist_pct": dist("sma50"), "sma200_dist_pct": dist("sma200"),
        "ema9_dist_pct": dist("ema9"), "ema21_dist_pct": dist("ema21"),
        "hi20_dist_pct": dist("hi20"), "lo20_dist_pct": dist("lo20"),
        "hi52_dist_pct": dist("hi52"), "lo52_dist_pct": dist("lo52"),
        "atr14_pct": None if pd.isna(last["atr14"]) else round(float(last["atr14"]) / c * 100, 2),
        "rvol20_pct": None if pd.isna(last["rvol20"]) else round(float(last["rvol20"]), 1),
        "macd_hist": None if pd.isna(last["macd_hist"]) else round(float(last["macd_hist"]), 3),
        "bb_pos": None if pd.isna(last["bb_upper"]) or last["bb_upper"] == last["bb_lower"] else round(float((c - last["bb_lower"]) / (last["bb_upper"] - last["bb_lower"])), 2),
        "vol_ratio": None if pd.isna(last["avg_vol20"]) or last["avg_vol20"] == 0 else round(float(last["volume"]) / float(last["avg_vol20"]), 2),
        "avg_dollar_vol20_m": None if pd.isna(last["avg_vol20"]) else round(float(last["avg_vol20"]) * c / 1e6, 1),
    }
    return out
