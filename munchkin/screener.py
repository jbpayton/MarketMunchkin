"""Universe-wide screener: daily indicator table with cross-sectional ranks, relative strength, sectors,
breadth/regime, an intraday table, preset setups with backtested statistics, and a setup backtester."""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import pickle
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np
import pandas as pd

from . import analytics as A
from .config import DATA_DIR, SETTINGS
from .util import now_et

if TYPE_CHECKING:  # forward references in annotations only
    from .market import Market

log = logging.getLogger("munchkin.screener")

UNIVERSE_FILE = DATA_DIR / "universe.json"
SCREENER_FILE = DATA_DIR / "screener.pkl"
STATS_FILE = DATA_DIR / "setup_stats.json"
EARNINGS_FILE = DATA_DIR / "earnings.json"
VOLPROFILE_FILE = DATA_DIR / "volprofile.json"

CORE_ETFS = [
    "SPY", "QQQ", "IWM", "DIA", "VTI", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "SMH", "SOXX", "ARKK", "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA", "SPXL", "SPXS", "UVXY", "VXX", "SVXY",
    "GLD", "SLV", "GDX", "GDXJ", "USO", "UNG", "TLT", "TMF", "HYG", "LQD", "KWEB", "FXI", "EEM", "EFA", "EWZ", "EWJ",
    "XBI", "IBB", "ITB", "XHB", "KRE", "XOP", "OIH", "URA", "LIT", "TAN", "ICLN", "JETS", "XRT", "IYT", "BITO", "IBIT", "ETHA",
]
EXTRA_NAMES = [
    "MSTR", "COIN", "HOOD", "SOFI", "PLTR", "RKLB", "IONQ", "RGTI", "QBTS", "SMCI", "ARM", "CRWV", "NBIS",
    "OKLO", "SMR", "NNE", "VST", "CEG", "TEM", "HIMS", "UPST", "AFRM", "RIVN", "LCID", "NIO", "XPEV", "BABA", "PDD",
    "JD", "SE", "GRAB", "MARA", "RIOT", "CLSK", "CIFR", "IREN", "WULF", "APLD", "ASTS", "LUNR", "ACHR", "JOBY", "DJT",
    "GME", "AMC", "OPEN", "BBAI", "SOUN", "AI", "PATH", "U", "RBLX", "DKNG", "CHWY", "CELH", "ELF", "DUOL", "APP",
]

COLUMNS_DOC = (
    "Columns: symbol, price, chg_1d_pct, chg_5d_pct, chg_20d_pct, chg_60d_pct, rsi14, sma20_dist_pct, sma50_dist_pct, "
    "sma200_dist_pct, ema9_dist_pct, ema21_dist_pct, hi20_dist_pct, lo20_dist_pct, hi52_dist_pct, lo52_dist_pct, "
    "atr14_pct, rvol20_pct (annualized realized vol %), macd_hist, bb_pos (0=lower band,1=upper), bb_width_pct, "
    "bb_squeeze (percentile of band width over 6 months; <20 = tight), vol_ratio (last volume / 20d avg), "
    "avg_dollar_vol20_m ($M), gap_pct, range_pos (0=closed at low,1=high), adx14 (trend strength, >25 trending), "
    "z20_atr (distance from 20d mean in ATRs), up_days_5, is_etf, sector, "
    "mom_3m_rank / mom_1m_rank / mom_1w_rank (0-100 percentile across the universe), mom_score (composite 0-100), "
    "volsurge_rank, rs_spy_20d / rs_spy_60d (pct-points vs SPY), rs_sector_20d (vs its sector ETF), beta_spy, corr_spy, "
    "days_to_earnings (None if unknown), news_24h. *_dist_pct = % of price above(+)/below(-) that level."
)

_SAFE_QUERY = re.compile(r"^[\w\s\.\-\+\*/<>=!&|()\[\]',\"]*$")

SETUPS = {
    "breakouts_on_volume": ("hi20_dist_pct >= -0.75 and vol_ratio > 1.5 and chg_1d_pct > 1.5 and avg_dollar_vol20_m > 20", "vol_ratio", False),
    "momentum_leaders": ("chg_20d_pct > 8 and sma20_dist_pct > 0 and hi52_dist_pct > -6 and avg_dollar_vol20_m > 50 and not is_etf", "chg_20d_pct", False),
    "pullbacks_in_uptrend": ("sma50_dist_pct > 0 and sma200_dist_pct > 0 and rsi14 >= 35 and rsi14 <= 50 and sma20_dist_pct < 0 and chg_5d_pct < 0 and avg_dollar_vol20_m > 30", "chg_20d_pct", False),
    "oversold_quality": ("rsi14 < 32 and sma200_dist_pct > -8 and avg_dollar_vol20_m > 50 and not is_etf", "rsi14", True),
    "unusual_volume": ("vol_ratio > 2.5 and avg_dollar_vol20_m > 20 and price > 5", "vol_ratio", False),
    "big_losers_liquid": ("chg_1d_pct < -6 and avg_dollar_vol20_m > 50", "chg_1d_pct", True),
    "squeeze_setups": ("bb_squeeze < 15 and adx14 < 20 and sma50_dist_pct > -3 and avg_dollar_vol20_m > 30", "bb_squeeze", True),
    "trend_pullback_adx": ("adx14 > 25 and sma50_dist_pct > 0 and z20_atr < 0 and z20_atr > -1.5 and rsi14 > 40 and avg_dollar_vol20_m > 30", "adx14", False),
}


# ------------------------------------------------------------------ universe
def _wiki_table(url: str, col_candidates: tuple[str, ...]) -> pd.DataFrame | None:
    r = httpx.get(url, timeout=30.0, headers={"User-Agent": "Mozilla/5.0 (research bot)"})
    r.raise_for_status()
    for t in pd.read_html(io.StringIO(r.text)):
        cols = [str(c) for c in t.columns]
        for cand in col_candidates:
            if cand in cols and len(t) > 50:
                t = t.rename(columns={cand: "symbol"})
                t["symbol"] = t["symbol"].astype(str).str.strip()
                t = t[t["symbol"].str.fullmatch(r"[A-Z][A-Z0-9\.]{0,5}")]
                return t
    return None


def build_universe(force: bool = False) -> list[str]:
    if UNIVERSE_FILE.exists() and not force:
        data = json.loads(UNIVERSE_FILE.read_text())
        if time.time() - data.get("ts", 0) < 7 * 86400 and data.get("sectors"):
            return data["symbols"]
    syms: set[str] = set(CORE_ETFS) | set(EXTRA_NAMES) | set(SETTINGS.screener_universe_extra)
    sectors: dict[str, str] = {}
    for url, cands in (("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", ("Symbol",)),
                       ("https://en.wikipedia.org/wiki/Nasdaq-100", ("Ticker", "Symbol"))):
        try:
            t = _wiki_table(url, cands)
            if t is not None:
                syms |= set(t["symbol"])
                sec_col = next((c for c in t.columns if "GICS Sector" in str(c) or str(c) == "Sector"), None)
                if sec_col:
                    for s_, sec in zip(t["symbol"], t[sec_col]):
                        if isinstance(sec, str):
                            sectors.setdefault(s_, sec)
        except Exception as e:
            log.warning("universe fetch failed for %s: %s", url, e)
    for e in CORE_ETFS:
        sectors[e] = "ETF"
    symbols = sorted(syms)
    if len(symbols) < 200 and UNIVERSE_FILE.exists():
        return json.loads(UNIVERSE_FILE.read_text())["symbols"]
    UNIVERSE_FILE.write_text(json.dumps({"ts": time.time(), "symbols": symbols, "sectors": sectors}))
    return symbols


def universe_sectors() -> dict[str, str]:
    if not UNIVERSE_FILE.exists():
        build_universe()
    data = json.loads(UNIVERSE_FILE.read_text())
    if not data.get("sectors"):
        build_universe(force=True)
        data = json.loads(UNIVERSE_FILE.read_text())
    return data.get("sectors", {})


# ------------------------------------------------------------------ earnings cache (yfinance, weekly)
def refresh_earnings(symbols: list[str], force: bool = False, workers: int = 8) -> dict[str, str]:
    cache: dict[str, Any] = {}
    if EARNINGS_FILE.exists():
        cache = json.loads(EARNINGS_FILE.read_text())
        if not force and time.time() - cache.get("ts", 0) < 7 * 86400:
            return cache.get("dates", {})
    try:
        from . import providers as P
        fh = P.finnhub_earnings_calendar(120)
        if fh:
            dates = {s_: v["date"] for s_, v in fh.items() if s_ in set(symbols) and v.get("date")}
            if len(dates) > 50:
                EARNINGS_FILE.write_text(json.dumps({"ts": time.time(), "dates": dates, "source": "finnhub"}))
                log.info("earnings cache from finnhub: %d dates", len(dates))
                return dates
    except Exception as e:
        log.warning("finnhub earnings calendar failed (%s); falling back to yfinance", e)
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    def one(sym: str) -> tuple[str, str | None]:
        try:
            cal = yf.Ticker(sym).calendar or {}
            ed = cal.get("Earnings Date")
            if ed:
                d0 = ed[0] if isinstance(ed, list) else ed
                return sym, str(d0)[:10]
        except Exception:
            pass
        return sym, None

    dates: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sym, d in ex.map(one, symbols):
            if d:
                dates[sym] = d
    EARNINGS_FILE.write_text(json.dumps({"ts": time.time(), "dates": dates}))
    log.info("earnings cache: %d dates", len(dates))
    return dates


def earnings_dates() -> dict[str, str]:
    if EARNINGS_FILE.exists():
        return json.loads(EARNINGS_FILE.read_text()).get("dates", {})
    return {}


# ------------------------------------------------------------------ frames
def build_frames(market: "Market", symbols: list[str], limit: int = 260, chunk: int = 150) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        try:
            df = market.bars(batch, "1D", limit=limit)
        except Exception as e:
            log.warning("bars chunk failed: %s", e)
            continue
        if df.empty:
            continue
        for sym, g in df.groupby(level="symbol"):
            g = g.droplevel("symbol")
            if len(g) < 25:
                continue
            f = A.screen_frame(g)
            f["_low"] = g["low"].astype(float)
            f["_high"] = g["high"].astype(float)
            frames[sym] = f
    return frames


def refresh(market: "Market", extra_symbols: list[str] | None = None) -> pd.DataFrame:
    """Rebuild the daily table (about 10-20 s)."""
    symbols = build_universe()
    if extra_symbols:
        symbols = sorted(set(symbols) | {s.upper() for s in extra_symbols})
    sectors = universe_sectors()
    etfs = set(CORE_ETFS)
    frames = build_frames(market, symbols, limit=260)
    rows: list[dict[str, Any]] = []
    rets: dict[str, pd.Series] = {}
    for sym, f in frames.items():
        last = f.iloc[-1]
        r = {k: (None if pd.isna(v) else (float(v) if isinstance(v, (np.floating, float, int, np.integer)) else v))
             for k, v in last.items() if not k.startswith("_")}
        r["symbol"] = sym
        r["is_etf"] = sym in etfs
        r["last_date"] = f.index[-1].date().isoformat()
        rows.append(r)
        rets[sym] = f["ret1"].tail(70)
    table = pd.DataFrame(rows)
    partial = False
    if table.empty:
        return table
    table = table.set_index("symbol", drop=False)
    for col in table.columns:
        if table[col].dtype == object and col not in ("symbol", "sector", "last_date"):
            try:
                table[col] = pd.to_numeric(table[col])
            except Exception:
                pass
    now = now_et()
    if table["last_date"].max() == now.date().isoformat() and now.time() < dt.time(16, 0):
        elapsed = (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / (6.5 * 3600)
        table["vol_ratio"] = (table["vol_ratio"] / min(1.0, max(0.05, elapsed))).round(2)
        partial = True
    spy = table.loc["SPY"].to_dict() if "SPY" in table.index else None
    etf_rows = table[table["is_etf"]]
    table = A.add_cross_sectional(table, spy, sectors, etf_rows)
    try:
        wide = pd.DataFrame(rets).dropna(how="all")
        bc = A.beta_corr(wide, "SPY", 60)
        table = table.join(bc, how="left")
    except Exception as e:
        log.warning("beta calc failed: %s", e)
    ed = earnings_dates()
    today = now.date()
    def _dte(sym):
        d = ed.get(sym)
        if not d:
            return None
        try:
            return (dt.date.fromisoformat(d) - today).days
        except Exception:
            return None
    table["days_to_earnings"] = [_dte(s) for s in table.index]
    try:
        nc = market.news_counts(24)
        table["news_24h"] = [int(nc.get(s, 0)) for s in table.index]
    except Exception:
        table["news_24h"] = 0
    with open(SCREENER_FILE, "wb") as f:
        pickle.dump({"ts": time.time(), "table": table, "partial": partial}, f)
    log.info("screener refreshed: %d symbols", len(table))
    return table


def load() -> tuple[pd.DataFrame | None, float | None]:
    if not SCREENER_FILE.exists():
        return None, None
    with open(SCREENER_FILE, "rb") as f:
        d = pickle.load(f)
    table = d["table"]
    table.attrs["partial"] = d.get("partial", False)
    return table, (time.time() - d["ts"]) / 3600.0


def _table(market: "Market | None") -> tuple[pd.DataFrame | None, float | None, str | None]:
    table, age_h = load()
    if table is None or (age_h is not None and age_h > 20 and market is not None) or (table is not None and "mom_score" not in table.columns and market is not None):
        if market is None:
            return None, None, "ERROR: screener table not built yet; run `munchkin screener refresh`."
        table = refresh(market)
        age_h = 0.0
    if table is None or table.empty:
        return None, None, "ERROR: screener table empty."
    return table, age_h, None


def _when(table: pd.DataFrame) -> str:
    return f"{table['last_date'].max()} {'intraday partial bar, vol_ratio pace-adjusted' if table.attrs.get('partial') else 'close'}"


DEFAULT_COLS = ["symbol", "price", "chg_1d_pct", "chg_5d_pct", "chg_20d_pct", "rsi14", "sma20_dist_pct", "sma50_dist_pct",
                "hi52_dist_pct", "atr14_pct", "adx14", "vol_ratio", "avg_dollar_vol20_m", "mom_score", "rs_spy_20d", "days_to_earnings", "sector"]


def _fmt(res: pd.DataFrame, cols: list[str]) -> str:
    cols = [c for c in cols if c in res.columns]
    return res[cols].to_string(index=False, float_format=lambda x: f"{x:.2f}", na_rep="-")


def query(expr: str, sort_by: str | None = None, ascending: bool = False, limit: int = 25,
          market: "Market | None" = None) -> str:
    table, age_h, err = _table(market)
    if err:
        return err
    expr = (expr or "").strip()
    if expr and not _SAFE_QUERY.match(expr):
        return "ERROR: query contains unsupported characters. Use column names, numbers, comparison operators, and/or/not, parentheses."
    try:
        res = table.query(expr, engine="python") if expr else table
    except Exception as e:
        return f"ERROR: bad query ({type(e).__name__}: {str(e)[:160]}). {COLUMNS_DOC}"
    if sort_by:
        if sort_by not in res.columns:
            return f"ERROR: unknown sort column {sort_by}. {COLUMNS_DOC}"
        res = res.sort_values(sort_by, ascending=ascending)
    res = res.head(max(1, min(limit, 60)))
    cols = list(DEFAULT_COLS)
    if sort_by and sort_by not in cols:
        cols.append(sort_by)
    return f"screener through {_when(table)} (age {age_h:.1f}h), {len(table)} symbols, {len(res)} matched:\n" + _fmt(res, cols)


# ------------------------------------------------------------------ setups + stats
def load_stats() -> dict[str, Any] | None:
    if STATS_FILE.exists():
        return json.loads(STATS_FILE.read_text())
    return None


def setups(market: "Market | None" = None, per: int = 8) -> str:
    table, age_h, err = _table(market)
    if err:
        return err
    stats = load_stats() or {}
    base = stats.get("baseline_all_days")
    cols = ["symbol", "price", "chg_1d_pct", "chg_5d_pct", "chg_20d_pct", "rsi14", "hi52_dist_pct", "atr14_pct",
            "vol_ratio", "avg_dollar_vol20_m", "mom_score", "rs_spy_20d", "days_to_earnings", "sector"]
    out = [f"preset setups through {_when(table)} ({len(table)} symbols). Stats = historical forward returns of this setup "
           f"({stats.get('generated', 'no backtest yet')[:10]}). Verify any candidate with get_chart + get_news before acting."]
    for name, (expr, sort_by, asc) in SETUPS.items():
        try:
            res = table.query(expr, engine="python").sort_values(sort_by, ascending=asc).head(per)
        except Exception as e:
            out.append(f"## {name}: error {e}")
            continue
        line = A.stats_line(name, stats.get("setups", {}).get(name, {}), base) if stats else "(no backtest yet)"
        body = _fmt(res, cols) if len(res) else "(none)"
        out.append(f"## {name}\nstats: {line}\n{body}")
    return "\n\n".join(out)


def backtest(market: "Market", years: int = 3) -> dict[str, Any]:
    symbols = build_universe()
    frames = build_frames(market, symbols, limit=int(252 * years + 30))
    for f in frames.values():
        pass
    stats = A.backtest_setups(frames, SETUPS, set(CORE_ETFS))
    stats["years"] = years
    stats["symbols"] = len(frames)
    STATS_FILE.write_text(json.dumps(stats, indent=1))
    return stats


# ------------------------------------------------------------------ regime / sectors
def regime_report(market: "Market", journal=None) -> str:
    table, age_h, err = _table(market)
    if err:
        return err
    vix = market.vix()
    reg = A.regime(table, vix)
    if journal is not None:
        try:
            journal.record_breadth({"score": reg["score"], "label": reg["label"], **reg["breadth"], "vix": vix.get("vix")})
        except Exception:
            pass
    b = reg["breadth"]
    lines = [f"REGIME: {reg['label'].upper()} (score {reg['score']}/100) | components: " + ", ".join(f"{k}={v}" for k, v in reg["components"].items()),
             f"breadth ({b['n']} stocks): {b['pct_above_sma50']}% above 50d, {b['pct_above_sma200']}% above 200d, {b['pct_rsi_gt_50']}% RSI>50, "
             f"20d highs {b['new_20d_highs']} vs lows {b['new_20d_lows']}, adv/dec {b['advancers']}/{b['decliners']}, median 20d chg {b['median_chg_20d']}%"]
    if journal is not None:
        hist = journal.breadth_history(10)
        if len(hist) > 1:
            lines.append("breadth trend (score / %>50d): " + " -> ".join(f"{h['date'][5:]}:{h.get('score')}/{h.get('pct_above_sma50')}" for h in hist))
    etf_rows = table[table["is_etf"]]
    st = A.sector_table(table, etf_rows)
    lines.append("sectors (avg of members, sorted by 20d):\n" + st.to_string(float_format=lambda x: f"{x:.1f}", na_rep="-"))
    return "\n".join(lines)


# ------------------------------------------------------------------ intraday
_INTRADAY_CACHE: dict[str, Any] = {}


def volume_profile(market: "Market") -> list[float]:
    if VOLPROFILE_FILE.exists():
        d = json.loads(VOLPROFILE_FILE.read_text())
        if time.time() - d.get("ts", 0) < 86400:
            return d["profile"]
    syms = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "AMD", "INTC", "SOFI", "PLTR", "XLF", "XLE", "SMH"]
    try:
        bars = market.bars(syms, "5Min", limit=78 * 12)
        prof = A.volume_profile(bars)
    except Exception as e:
        log.warning("volume profile failed: %s", e)
        prof = [min(1.0, (i + 1) / 78) for i in range(78)]
    VOLPROFILE_FILE.write_text(json.dumps({"ts": time.time(), "profile": prof}))
    return prof


def intraday(market: "Market", force: bool = False) -> pd.DataFrame:
    now = now_et()
    c = _INTRADAY_CACHE.get("t")
    if c is not None and not force and time.time() - _INTRADAY_CACHE.get("ts", 0) < 180:
        return c
    table, _, err = _table(market)
    if err:
        return pd.DataFrame()
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockSnapshotRequest
    syms = list(table.index)
    snaps = market.stocks.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=syms, feed=DataFeed.DELAYED_SIP))
    try:  # freshest prices from IEX for the latest trade
        iex = market.stocks.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=syms, feed=DataFeed.IEX))
        for s, v in iex.items():
            if s in snaps and v.latest_trade and (snaps[s].latest_trade is None or v.latest_trade.timestamp > snaps[s].latest_trade.timestamp):
                snaps[s].latest_trade = v.latest_trade
    except Exception:
        pass
    try:
        nc = market.news_counts(24)
    except Exception:
        nc = {}
    t = A.intraday_table(snaps, table, volume_profile(market), now, nc)
    _INTRADAY_CACHE.update({"t": t, "ts": time.time()})
    return t


INTRADAY_COLS = ["symbol", "price", "chg_today_pct", "gap_pct", "from_open_pct", "range_pos", "from_hod_pct", "vwap_dist_pct", "rel_vol",
                 "rs_today", "rsi14", "sma50_dist_pct", "hi52_dist_pct", "atr14_pct", "avg_dollar_vol20_m", "mom_score", "days_to_earnings", "news_24h", "sector"]
INTRADAY_DOC = ("Intraday columns: price, chg_today_pct, gap_pct (open vs prior close), from_open_pct, range_pos (0=at low,1=at high), "
                "from_hod_pct, from_lod_pct, day_range_pct, vwap_dist_pct, day_vol, rel_vol (volume vs 20d average at this time of day; "
                ">2 = heavy), rs_today (pct-points vs SPY today), plus daily context: rsi14, sma20/50/200_dist_pct, hi52_dist_pct, hi20_dist_pct, "
                "atr14_pct, avg_dollar_vol20_m, mom_1m_rank, mom_3m_rank, mom_score, rs_spy_20d, adx14, bb_squeeze, sector, days_to_earnings, news_24h, is_etf.")


def intraday_query(expr: str, sort_by: str | None, ascending: bool, limit: int, market: "Market") -> str:
    t = intraday(market)
    if t.empty:
        return "intraday table unavailable (market closed or no snapshot data)."
    expr = (expr or "").strip()
    if expr and not _SAFE_QUERY.match(expr):
        return "ERROR: unsupported characters in query."
    try:
        res = t.query(expr, engine="python") if expr else t
    except Exception as e:
        return f"ERROR: bad query ({str(e)[:140]}). {INTRADAY_DOC}"
    if sort_by:
        if sort_by not in res.columns:
            return f"ERROR: unknown sort column {sort_by}. {INTRADAY_DOC}"
        res = res.sort_values(sort_by, ascending=ascending)
    res = res.head(max(1, min(limit, 60)))
    cols = list(INTRADAY_COLS) + ([sort_by] if sort_by and sort_by not in INTRADAY_COLS else [])
    stamp = now_et().strftime("%H:%M")
    return f"intraday screener at {stamp} ET (15-min delayed consolidated volume, latest trade merged), {len(t)} symbols, {len(res)} matched:\n" + _fmt(res, cols)


def intraday_setups(market: "Market", per: int = 6) -> str:
    t = intraday(market)
    if t.empty:
        return "intraday table unavailable (market closed or no snapshot data)."
    out = [f"intraday setups at {now_et().strftime('%H:%M')} ET (rel_vol is time-of-day adjusted). Verify with get_chart 5Min/15Min + news."]
    for name, (expr, sort_by, asc) in A.INTRADAY_SETUPS.items():
        try:
            res = t.query(expr, engine="python").sort_values(sort_by, ascending=asc).head(per)
        except Exception as e:
            out.append(f"## {name}: error {str(e)[:100]}")
            continue
        out.append(f"## {name}\n[{expr}]\n" + (_fmt(res, INTRADAY_COLS) if len(res) else "(none)"))
    return "\n\n".join(out)
