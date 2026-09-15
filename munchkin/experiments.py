"""Intraday strategy experiments: versioned detector + bounded model classification + honest shadow execution.

First experiment: opening-range continuation (docs/intraday-experiment.md). Everything here is shadow-only by default.
Variants: A mechanical signal (analytical underlying returns), B mechanical + catalyst filter (accepted vs rejected),
C filtered signal expressed with a bought call/put (executable shadow trades on captured quotes). Two books per variant:
an analytical signal-outcome dataset and cash-constrained simulated portfolios ($500 and $750). Promotion is human.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import re
import time
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel

from .config import DATA_DIR, RiskLimits
from .util import ET, is_option, now_et, occ_human, parse_occ

log = logging.getLogger("munchkin.experiments")

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, version INTEGER, config_json TEXT, config_hash TEXT,
  status TEXT DEFAULT 'disabled', created_at TEXT, activated_at TEXT, notes TEXT DEFAULT '', UNIQUE(name, version));
CREATE TABLE IF NOT EXISTS exp_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id INTEGER, dedupe_key TEXT UNIQUE, session_date TEXT,
  symbol TEXT, direction TEXT, ts_eligible TEXT, ts_receipt TEXT, bar_end TEXT, detector_version TEXT, features_json TEXT, data_refs_json TEXT,
  class_state TEXT DEFAULT 'pending', classification_json TEXT, class_ts TEXT, class_latency_ms INTEGER, model_id TEXT, prompt_version TEXT);
CREATE TABLE IF NOT EXISTS exp_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, variant TEXT, portfolio TEXT, decision TEXT,
  reason_code TEXT, reason TEXT, decided_at TEXT, decision_price REAL, price_src TEXT, price_ts TEXT, UNIQUE(signal_id, variant, portfolio));
CREATE TABLE IF NOT EXISTS exp_quotes (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, contract TEXT, purpose TEXT, ts_receipt TEXT,
  ts_exchange TEXT, bid REAL, ask REAL, bid_size REAL, ask_size REAL, feed TEXT, fresh INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS exp_shadow_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, experiment_id INTEGER, variant TEXT,
  portfolio TEXT, instrument TEXT, contract TEXT, multiplier REAL, qty REAL, direction TEXT, entry_ts TEXT, entry_px REAL, entry_src TEXT,
  stop_level REAL, time_exit_ts TEXT, exit_ts TEXT, exit_px REAL, exit_reason TEXT, fees REAL DEFAULT 0, slippage REAL DEFAULT 0,
  pnl_gross REAL, pnl_net REAL, mfe_pct REAL, mae_pct REAL, status TEXT DEFAULT 'open', note TEXT);
CREATE TABLE IF NOT EXISTS exp_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, horizon_min INTEGER, px_start REAL, px_end REAL,
  ts_end TEXT, ret_pct REAL, mfe_pct REAL, mae_pct REAL, status TEXT, UNIQUE(signal_id, horizon_min));
CREATE TABLE IF NOT EXISTS exp_portfolios (experiment_id INTEGER, variant TEXT, portfolio TEXT, equity REAL, day_start_equity REAL, day TEXT,
  blocked INTEGER DEFAULT 0, blocked_reason TEXT, updated_at TEXT, PRIMARY KEY(experiment_id, variant, portfolio));
CREATE TABLE IF NOT EXISTS exp_events (id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id INTEGER, ts TEXT, kind TEXT, text TEXT);
"""
DETECTOR_VERSION = "orb-continuation/1"
PROMPT_VERSION = "catalyst-classifier/1"
VARIANTS = ("A", "B", "C")
PORTFOLIOS = ("analytical", "p500", "p750")
REASONS = {"outside_window", "stale_data", "insufficient_history", "relvol_below", "rs_below", "over_extended", "illiquid", "horizon_past_close",
           "duplicate", "classifier_rejected", "classifier_abstained", "classifier_unavailable", "no_suitable_contract_within_budget",
           "quote_stale", "spread_too_wide", "cash_unavailable", "breaker", "concurrency", "bearish_not_deployable", "accepted"}


class ExperimentConfig(BaseModel):
    name: str = "orb_continuation"
    mode: str = "shadow"                    # disabled | shadow | live (live routing exists but is never enabled by default)
    # detector
    or_minutes: int = 30
    entry_window: str = "10:00-14:00"       # ET; completed 5-minute closes inside this window
    bar_minutes: int = 5
    relvol_min: float = 1.5                 # IEX cumulative session volume vs the prior sessions' same-time average (IEX vs IEX)
    relvol_lookback_Runs: int = 10
    rs_min_pct: float = 0.3                 # symbol move since open minus SPY move since open, signed by direction
    max_extension_pct: float = 1.0          # close no further than this beyond the range edge
    min_avg_dollar_volume: float = 20e6
    freshness_s: int = 180                  # bar end must be within this of now
    horizon_min: int = 120
    diag_horizons: list[int] = [60, 180]
    close_buffer_min: int = 30              # no entry whose horizon would cross session close minus this
    universe_max: int = 60                  # top names by 20-day dollar volume, plus SPY reference
    reentry: str = "one_per_symbol_direction_session"
    # classifier
    classifier_enabled: bool = True
    classifier_timeout_s: int = 60
    news_max_age_h: int = 24
    # contracts (variant C)
    dte_min: int = 7
    dte_max: int = 21
    delta_lo: float = 0.45
    delta_hi: float = 0.65
    max_spread_pct: float = 15.0
    max_quote_age_s: int = 300
    fee_per_contract: float = 0.65
    option_slippage_pct: float = 1.0        # adverse, on top of crossing the spread
    # stock costs
    stock_slippage_bps: float = 5.0
    # decision latency model for shadow fills
    latency_s: int = 90
    # experiment risk envelope (can only tighten the account/style limits)
    equity: float = 500.0
    comparison_equity: float = 750.0
    max_single_option_pct: float = 2.0
    max_agg_option_pct: float = 4.0
    max_concurrent: int = 1
    daily_loss_pct: float = 2.0
    stock_pos_pct: float = 10.0
    stock_stop_budget_pct: float = 0.5


def config_hash(cfg: ExperimentConfig) -> str:
    d = cfg.model_dump()
    d.pop("mode", None)   # mode is operational, not part of the scientific identity
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def tighten_limits(base: RiskLimits, cfg: ExperimentConfig, equity: float | None = None) -> RiskLimits:
    """The experiment envelope can only tighten the active style limits, never relax them."""
    upd = {
        "max_position_pct": min(base.max_position_pct, cfg.stock_pos_pct / 100),
        "max_options_pct": min(base.max_options_pct, cfg.max_agg_option_pct / 100),
        "max_positions": min(base.max_positions, cfg.max_concurrent),
        "max_daily_loss_pct": min(base.max_daily_loss_pct, cfg.daily_loss_pct / 100),
        "allow_spreads": False,
        "allow_options": bool(base.allow_options),
        "allow_singles": bool(base.allow_singles),
        "min_option_dte": max(base.min_option_dte, cfg.dte_min),
        "size_mult_speculative": min(base.size_mult_speculative, 1.0),
        "size_mult_no_catalyst": min(base.size_mult_no_catalyst, 1.0),
    }
    return base.model_copy(update=upd)


# ---------------------------------------------------------------- store
class ExperimentStore:
    def __init__(self, journal: Any, clock: Any | None = None) -> None:
        self.j = journal
        self.conn = journal.conn
        self.clock = clock or now_et   # injectable so shadow timestamps follow the runner's clock (tests use a fixed one)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- versions
    def register(self, cfg: ExperimentConfig, notes: str = "") -> dict[str, Any]:
        """Immutable versions: a config with a new hash becomes a new version; an existing hash returns its record."""
        h = config_hash(cfg)
        row = self.conn.execute("SELECT * FROM experiments WHERE name=? AND config_hash=?", (cfg.name, h)).fetchone()
        if row:
            return dict(row)
        v = (self.conn.execute("SELECT COALESCE(MAX(version), 0) FROM experiments WHERE name=?", (cfg.name,)).fetchone()[0] or 0) + 1
        self.conn.execute("INSERT INTO experiments(name, version, config_json, config_hash, status, created_at, notes) VALUES (?,?,?,?,?,?,?)",
                          (cfg.name, v, cfg.model_dump_json(), h, "disabled", self.clock().isoformat(timespec="seconds"), notes))
        self.conn.commit()
        rec = dict(self.conn.execute("SELECT * FROM experiments WHERE name=? AND version=?", (cfg.name, v)).fetchone())
        self.event(rec["id"], "version", f"{cfg.name} v{v} registered ({h})")
        return rec

    def set_status(self, exp_id: int, status: str, who: str = "operator") -> bool:
        if status not in ("disabled", "shadow", "live"):
            return False
        if status == "live":
            return False   # live activation is not a code path in this version (brief §11.5); requires a deliberate later change
        self.conn.execute("UPDATE experiments SET status=?, activated_at=? WHERE id=?", (status, self.clock().isoformat(timespec="seconds") if status != "disabled" else None, exp_id))
        self.conn.commit()
        self.event(exp_id, "status", f"{status} ({who})")
        return True

    def get(self, exp_id: int) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
        return dict(r) if r else None

    def latest(self, name: str = "orb_continuation") -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM experiments WHERE name=? ORDER BY version DESC LIMIT 1", (name,)).fetchone()
        return dict(r) if r else None

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM experiments ORDER BY name, version").fetchall()]

    def config(self, exp: dict[str, Any]) -> ExperimentConfig:
        return ExperimentConfig(**json.loads(exp["config_json"]))

    def event(self, exp_id: int, kind: str, text: str) -> None:
        self.conn.execute("INSERT INTO exp_events(experiment_id, ts, kind, text) VALUES (?,?,?,?)", (exp_id, self.clock().isoformat(timespec="seconds"), kind, text[:600]))
        self.conn.commit()

    # ---- signals
    def add_signal(self, exp_id: int, key: str, symbol: str, direction: str, ts_eligible: str, ts_receipt: str, bar_end: str,
                   features: dict[str, Any], data_refs: dict[str, Any]) -> int | None:
        """Insert-or-ignore on the dedupe key: repeated ticks and restarts never create a second signal."""
        cur = self.conn.execute("INSERT OR IGNORE INTO exp_signals(experiment_id, dedupe_key, session_date, symbol, direction, ts_eligible, ts_receipt, bar_end, detector_version, features_json, data_refs_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (exp_id, key, ts_eligible[:10], symbol, direction, ts_eligible, ts_receipt, bar_end, DETECTOR_VERSION, json.dumps(features, default=str), json.dumps(data_refs, default=str)))
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def signal(self, sid: int) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM exp_signals WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["features"] = json.loads(d.pop("features_json") or "{}")
        d["data_refs"] = json.loads(d.pop("data_refs_json") or "{}")
        d["classification"] = json.loads(d.pop("classification_json") or "null")
        return d

    def signals(self, exp_id: int, limit: int = 200, class_state: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT id FROM exp_signals WHERE experiment_id=?" + (" AND class_state=?" if class_state else "") + " ORDER BY id DESC LIMIT ?"
        args = (exp_id, class_state, limit) if class_state else (exp_id, limit)
        return [self.signal(r[0]) for r in self.conn.execute(q, args).fetchall()]

    def set_classification(self, sid: int, state: str, payload: dict[str, Any] | None, latency_ms: int, model_id: str, prompt_version: str) -> bool:
        """First classification wins; later news or re-runs never overwrite the original decision inputs."""
        cur = self.conn.execute("UPDATE exp_signals SET class_state=?, classification_json=?, class_ts=?, class_latency_ms=?, model_id=?, prompt_version=? WHERE id=? AND class_state='pending'",
                                (state, json.dumps(payload, default=str) if payload is not None else None, self.clock().isoformat(timespec="seconds"), latency_ms, model_id, prompt_version, sid))
        self.conn.commit()
        return cur.rowcount > 0

    # ---- decisions / quotes / trades / outcomes
    def decide(self, sid: int, variant: str, portfolio: str, decision: str, reason_code: str, reason: str = "", price: float | None = None,
               price_src: str | None = None, price_ts: str | None = None) -> None:
        self.conn.execute("INSERT OR IGNORE INTO exp_decisions(signal_id, variant, portfolio, decision, reason_code, reason, decided_at, decision_price, price_src, price_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (sid, variant, portfolio, decision, reason_code, reason[:400], self.clock().isoformat(timespec="seconds"), price, price_src, price_ts))
        self.conn.commit()

    def decisions(self, sid: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM exp_decisions WHERE signal_id=? ORDER BY id", (sid,)).fetchall()]

    def add_quote(self, sid: int, contract: str, purpose: str, q: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO exp_quotes(signal_id, contract, purpose, ts_receipt, ts_exchange, bid, ask, bid_size, ask_size, feed, fresh, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (sid, contract, purpose, q.get("ts_receipt"), q.get("ts_exchange"), q.get("bid"), q.get("ask"), q.get("bid_size"), q.get("ask_size"), q.get("feed"), 1 if q.get("fresh") else 0, (q.get("note") or "")[:200]))
        self.conn.commit()

    def open_trade(self, sid: int, exp_id: int, variant: str, portfolio: str, instrument: str, contract: str | None, multiplier: float, qty: float,
                   direction: str, entry_ts: str, entry_px: float, entry_src: str, stop_level: float | None, time_exit_ts: str, fees: float, slippage: float, note: str = "") -> int:
        cur = self.conn.execute("INSERT INTO exp_shadow_trades(signal_id, experiment_id, variant, portfolio, instrument, contract, multiplier, qty, direction, entry_ts, entry_px, entry_src, stop_level, time_exit_ts, fees, slippage, status, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                                (sid, exp_id, variant, portfolio, instrument, contract, multiplier, qty, direction, entry_ts, entry_px, entry_src, stop_level, time_exit_ts, fees, slippage, note[:300]))
        self.conn.commit()
        return cur.lastrowid

    def close_trade(self, tid: int, exit_ts: str, exit_px: float | None, reason: str, fees_extra: float = 0.0, status: str = "closed",
                    mfe_pct: float | None = None, mae_pct: float | None = None) -> None:
        t = dict(self.conn.execute("SELECT * FROM exp_shadow_trades WHERE id=?", (tid,)).fetchone())
        pnl_gross = pnl_net = None
        if exit_px is not None and status == "closed":
            sign = 1.0 if (t["instrument"] == "stock" or t["direction"] == "bullish" or t["instrument"] in ("call", "put")) else -1.0
            # options: long premium regardless of direction; stock: long only (bearish stock is never simulated as a holding)
            pnl_gross = (exit_px - t["entry_px"]) * t["qty"] * t["multiplier"] * (1.0 if t["instrument"] != "stock" else sign)
            pnl_net = pnl_gross - (t["fees"] or 0) - fees_extra - (t["slippage"] or 0)
        self.conn.execute("UPDATE exp_shadow_trades SET exit_ts=?, exit_px=?, exit_reason=?, fees=COALESCE(fees,0)+?, pnl_gross=?, pnl_net=?, status=?, mfe_pct=?, mae_pct=? WHERE id=?",
                          (exit_ts, exit_px, reason, fees_extra, pnl_gross, pnl_net, status, mfe_pct, mae_pct, tid))
        self.conn.commit()

    def open_trades(self, exp_id: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM exp_shadow_trades WHERE status='open'" + (" AND experiment_id=?" if exp_id else "")
        return [dict(r) for r in self.conn.execute(q, (exp_id,) if exp_id else ()).fetchall()]

    def trades(self, exp_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM exp_shadow_trades WHERE experiment_id=? ORDER BY id", (exp_id,)).fetchall()]

    def set_outcome(self, sid: int, horizon: int, px_start: float | None, px_end: float | None, ts_end: str | None, ret_pct: float | None,
                    mfe: float | None, mae: float | None, status: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO exp_outcomes(signal_id, horizon_min, px_start, px_end, ts_end, ret_pct, mfe_pct, mae_pct, status) VALUES (?,?,?,?,?,?,?,?,?)",
                          (sid, horizon, px_start, px_end, ts_end, ret_pct, mfe, mae, status))
        self.conn.commit()

    def outcomes(self, sid: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM exp_outcomes WHERE signal_id=? ORDER BY horizon_min", (sid,)).fetchall()]

    # ---- portfolios
    def portfolio(self, exp_id: int, variant: str, portfolio: str, start_equity: float) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM exp_portfolios WHERE experiment_id=? AND variant=? AND portfolio=?", (exp_id, variant, portfolio)).fetchone()
        today = self.clock().date().isoformat()
        if not r:
            self.conn.execute("INSERT INTO exp_portfolios(experiment_id, variant, portfolio, equity, day_start_equity, day, blocked, updated_at) VALUES (?,?,?,?,?,?,0,?)",
                              (exp_id, variant, portfolio, start_equity, start_equity, today, self.clock().isoformat(timespec="seconds")))
            self.conn.commit()
            return self.portfolio(exp_id, variant, portfolio, start_equity)
        d = dict(r)
        if d["day"] != today:   # new session: reset the day-start mark; a tripped breaker stays tripped until the operator clears it
            self.conn.execute("UPDATE exp_portfolios SET day_start_equity=?, day=? WHERE experiment_id=? AND variant=? AND portfolio=?", (d["equity"], today, exp_id, variant, portfolio))
            self.conn.commit()
            d["day_start_equity"], d["day"] = d["equity"], today
        return d

    def update_portfolio(self, exp_id: int, variant: str, portfolio: str, equity: float | None = None, blocked: bool | None = None, reason: str | None = None) -> None:
        sets, args = ["updated_at=?"], [self.clock().isoformat(timespec="seconds")]
        if equity is not None:
            sets.append("equity=?"); args.append(equity)
        if blocked is not None:
            sets.append("blocked=?"); args.append(1 if blocked else 0)
            sets.append("blocked_reason=?"); args.append(reason)
        args += [exp_id, variant, portfolio]
        self.conn.execute(f"UPDATE exp_portfolios SET {', '.join(sets)} WHERE experiment_id=? AND variant=? AND portfolio=?", args)
        self.conn.commit()


# ---------------------------------------------------------------- session calendar
def session_bounds(broker: Any, day: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    """Regular-session open/close for a date from the broker calendar (holidays and early closes), in ET."""
    try:
        cal = broker.calendar(day, day)
    except Exception:
        cal = []
    def _t(v: Any) -> tuple[int, int] | None:
        s = str(v)
        m = re.search(r"(\d{1,2}):(\d{2})", s[-8:] if "T" in s or " " in s else s)
        return (int(m.group(1)), int(m.group(2))) if m else None
    for c in cal:
        if str(c.get("date"))[:10] == day.isoformat():
            o, cl = _t(c.get("open")), _t(c.get("close"))
            if o and cl:
                return dt.datetime(day.year, day.month, day.day, o[0], o[1], tzinfo=ET), dt.datetime(day.year, day.month, day.day, cl[0], cl[1], tzinfo=ET)
    return None


# ---------------------------------------------------------------- detector
def _hm(s: str) -> dt.time:
    return dt.time(int(s[:2]), int(s[3:5]))


def detect_signals(bars: pd.DataFrame, spy_bars: pd.DataFrame | None, cfg: ExperimentConfig, now: dt.datetime, session_open: dt.datetime,
                   session_close: dt.datetime, dollar_volume: dict[str, float] | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Pure function over completed IEX bars (MultiIndex symbol/timestamp). Returns eligible candidates and rejection counters.
    Candidates are recorded before any model sees them; every rejection is counted by reason."""
    counters: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    if bars is None or bars.empty:
        return out, {"insufficient_history": 1}
    w0, w1 = cfg.entry_window.split("-")
    t_now = now.astimezone(ET)
    day = t_now.date()
    or_end = session_open + dt.timedelta(minutes=cfg.or_minutes)
    latest_close_for_entry = session_close - dt.timedelta(minutes=cfg.close_buffer_min + cfg.horizon_min)
    spy_move = None
    if spy_bars is not None and not spy_bars.empty:
        s = spy_bars[spy_bars.index.get_level_values("timestamp") >= session_open] if "symbol" in spy_bars.index.names else spy_bars[spy_bars.index >= session_open]
        if len(s):
            spy_move = float(s["close"].iloc[-1] / s["open"].iloc[0] - 1) * 100
    for sym, g in bars.groupby(level="symbol"):
        g = g.droplevel("symbol").sort_index()
        g = g[g.index <= t_now]
        today = g[g.index >= session_open - dt.timedelta(minutes=1)]
        if len(today) < cfg.or_minutes // cfg.bar_minutes + 1:
            counters["insufficient_history"] = counters.get("insufficient_history", 0) + 1
            continue
        or_bars = today[today.index < or_end]
        after = today[today.index >= or_end]
        if or_bars.empty or after.empty:
            counters["insufficient_history"] = counters.get("insufficient_history", 0) + 1
            continue
        or_hi, or_lo = float(or_bars["high"].max()), float(or_bars["low"].min())
        last = after.iloc[-1]
        bar_start = after.index[-1].to_pydatetime()
        bar_end = bar_start + dt.timedelta(minutes=cfg.bar_minutes)
        if bar_end > t_now:   # not a completed bar
            after = after.iloc[:-1]
            if after.empty:
                continue
            last = after.iloc[-1]; bar_start = after.index[-1].to_pydatetime(); bar_end = bar_start + dt.timedelta(minutes=cfg.bar_minutes)
        if (t_now - bar_end).total_seconds() > cfg.freshness_s:
            counters["stale_data"] = counters.get("stale_data", 0) + 1
            continue
        if not (_hm(w0) <= bar_end.astimezone(ET).time() <= _hm(w1)):
            counters["outside_window"] = counters.get("outside_window", 0) + 1
            continue
        if bar_end > latest_close_for_entry:
            counters["horizon_past_close"] = counters.get("horizon_past_close", 0) + 1
            continue
        close = float(last["close"])
        direction = "bullish" if close > or_hi else "bearish" if close < or_lo else None
        if direction is None:
            continue
        edge = or_hi if direction == "bullish" else or_lo
        extension = abs(close / edge - 1) * 100
        if extension > cfg.max_extension_pct:
            counters["over_extended"] = counters.get("over_extended", 0) + 1
            continue
        if dollar_volume is not None and dollar_volume.get(sym, 0) < cfg.min_avg_dollar_volume:
            counters["illiquid"] = counters.get("illiquid", 0) + 1
            continue
        # relative volume: IEX cumulative volume so far today vs the prior Runs' cumulative volume at the same time of day (IEX vs IEX)
        cum_today = float(today[today.index < bar_end]["volume"].sum())
        prior = g[g.index < session_open]
        tod = bar_end.astimezone(ET).time()
        hist = []
        for d_, gd in prior.groupby(prior.index.date):
            gd = gd[[ts.astimezone(ET).time() < tod for ts in gd.index]]
            if len(gd):
                hist.append(float(gd["volume"].sum()))
        hist = hist[-cfg.relvol_lookback_Runs:]
        relvol = cum_today / (sum(hist) / len(hist)) if hist and sum(hist) > 0 else None
        if relvol is None or relvol < cfg.relvol_min:
            counters["relvol_below"] = counters.get("relvol_below", 0) + 1
            continue
        move = float(close / float(today["open"].iloc[0]) - 1) * 100
        rs = (move - spy_move) if spy_move is not None else None
        rs_signed = rs if direction == "bullish" else (-rs if rs is not None else None)
        if rs_signed is None or rs_signed < cfg.rs_min_pct:
            counters["rs_below"] = counters.get("rs_below", 0) + 1
            continue
        out.append({"symbol": sym, "direction": direction, "bar_end": bar_end.isoformat(timespec="seconds"), "close": close, "or_high": or_hi, "or_low": or_lo,
                    "edge": edge, "extension_pct": round(extension, 3), "relvol": round(relvol, 2), "move_since_open_pct": round(move, 3),
                    "spy_move_pct": None if spy_move is None else round(spy_move, 3), "rs_pct": None if rs is None else round(rs, 3),
                    "cum_volume_iex": cum_today, "relvol_Runs": len(hist), "bar_minutes": cfg.bar_minutes,
                    "dedupe_key": f"{DETECTOR_VERSION}|{day.isoformat()}|{sym}|{direction}"})
    return out, counters


# ---------------------------------------------------------------- classifier (bounded, structured, first-wins)
CLASSIFIER_SYSTEM = (
    "You classify whether a fresh, substantive catalyst explains an intraday move. Output ONLY a JSON object with keys: "
    "event_type (earnings|guidance|m&a|regulatory|product|analyst|macro|legal|management|other|none), direction (bullish|bearish|mixed|none), "
    "novelty (new|recycled|unknown), substantive (true|false), source_quality (primary|major_outlet|minor|social|unknown), "
    "published_at (ISO or null), url (string or null), excerpt (<=200 chars, verbatim), thesis (<=200 chars: why more movement could follow), "
    "confidence (low|medium|high), abstain (true|false). Abstain when the evidence is missing, stale, or contradictory. "
    "A confirmed source is not the same as an unpriced catalyst; do not inflate confidence because a source is reliable.")


def classify_signal(llm: Any, market: Any, sig: dict[str, Any], cfg: ExperimentConfig, news_search: Any | None = None) -> tuple[str, dict[str, Any] | None, int, str]:
    """Returns (state, payload, latency_ms, model_id). Never raises. Original evidence is what it saw at decision time."""
    sym = sig["symbol"]
    t0 = time.time()
    evidence: list[str] = []
    try:
        items = market.news([sym], 8, cfg.news_max_age_h)
        for i in items[:8]:
            evidence.append(f"[{i.get('time')}] {i.get('headline')} ({i.get('source')}) {i.get('url') or ''}")
    except Exception as e:
        evidence.append(f"(feed error: {str(e)[:60]})")
    if news_search is not None:
        try:
            for r in news_search(f"{sym} stock", "news", 6, "day")[:6]:
                evidence.append(f"[{r.get('date') or '?'}] {r.get('title')} {r.get('url') or ''}")
        except Exception:
            pass
    user = (f"Symbol {sym}. Direction of the move: {sig['direction']}. Time now: {now_et().isoformat(timespec='minutes')} ET. "
            f"Move since open {sig['features'].get('move_since_open_pct')}% vs SPY {sig['features'].get('spy_move_pct')}%, relative volume {sig['features'].get('relvol')}x.\n"
            f"Evidence (headlines available at decision time):\n" + ("\n".join(evidence) if evidence else "(none)"))
    try:
        r = llm.chat([{"role": "system", "content": CLASSIFIER_SYSTEM}, {"role": "user", "content": user}], max_tokens=600, temperature=0.0, reasoning_effort="low")
        txt = (r.get("message") or {}).get("content") or ""
        m = re.search(r"\{.*\}", txt, re.S)
        payload = json.loads(m.group(0)) if m else None
        latency = int((time.time() - t0) * 1000)
        model_id = getattr(llm, "model", "?")
        if not payload:
            return "error", {"error": "no json in reply", "raw": txt[:300], "evidence": evidence}, latency, model_id
        payload["evidence"] = evidence
        payload["evidence_hash"] = hashlib.sha256("\n".join(evidence).encode()).hexdigest()[:16]
        if payload.get("abstain") or payload.get("event_type") in (None, "none"):
            return "abstain", payload, latency, model_id
        return "classified", payload, latency, model_id
    except Exception as e:
        return "error", {"error": f"{type(e).__name__}: {str(e)[:160]}", "evidence": evidence}, int((time.time() - t0) * 1000), getattr(llm, "model", "?")


def classifier_accepts(payload: dict[str, Any] | None, direction: str) -> tuple[bool, str]:
    """Variant B's filter over the structured classification. Deterministic, documented, and separate from evidence quality."""
    if not payload or payload.get("abstain"):
        return False, "classifier_abstained"
    if not payload.get("substantive") or payload.get("novelty") == "recycled":
        return False, "classifier_rejected"
    d = payload.get("direction")
    if d not in (direction, "mixed") or payload.get("source_quality") in ("social", "unknown"):
        return False, "classifier_rejected"
    return True, "accepted"


# ---------------------------------------------------------------- contract selection (variant C)
def select_contract(chain: list[dict[str, Any]], direction: str, cfg: ExperimentConfig, budget: float, now: dt.datetime) -> tuple[dict[str, Any] | None, str]:
    """Deterministic: right type, DTE window, |delta| band, fresh two-sided quote, spread cap, affordable with fees.
    Ranking: closest |delta| to the band centre, then tighter spread, then nearer expiry. Never relaxes the policy for affordability."""
    want = "call" if direction == "bullish" else "put"
    cands = []
    reason = "no_suitable_contract_within_budget"
    saw_any = False
    for c in chain:
        if c.get("type") != want or c.get("error"):
            continue
        saw_any = True
        dte = c.get("dte")
        if dte is None or not (cfg.dte_min <= int(dte) <= cfg.dte_max):
            continue
        delta = c.get("delta")
        if delta is None or not (cfg.delta_lo <= abs(float(delta)) <= cfg.delta_hi):
            continue
        bid, ask = c.get("bid"), c.get("ask")
        if not bid or not ask or ask <= 0 or bid <= 0 or ask < bid:
            reason = "quote_stale"
            continue
        qa = c.get("quote_age_s")
        if qa is not None and qa > cfg.max_quote_age_s:
            reason = "quote_stale"
            continue
        sp = c.get("spread_pct")
        if sp is not None and sp > cfg.max_spread_pct:
            reason = "spread_too_wide"
            continue
        mult = float(c.get("multiplier") or 100)
        cost = ask * mult + cfg.fee_per_contract
        if cost > budget + 1e-9:
            reason = "no_suitable_contract_within_budget"
            continue
        centre = (cfg.delta_lo + cfg.delta_hi) / 2
        cands.append(((abs(abs(float(delta)) - centre), sp or 0, int(dte)), {**c, "cost": round(cost, 2), "multiplier": mult}))
    if not cands:
        return None, reason if saw_any else "no_suitable_contract_within_budget"
    cands.sort(key=lambda x: x[0])
    return cands[0][1], "accepted"


# ---------------------------------------------------------------- shadow fills (conservative)
def shadow_fill_price(side: str, quote: dict[str, Any], cfg: ExperimentConfig, instrument: str) -> tuple[float | None, str]:
    """Buys at the ask, sells at the bid, plus adverse slippage. No quote or a crossed/stale quote means no fill."""
    bid, ask = quote.get("bid"), quote.get("ask")
    if instrument == "stock" and (bid is None or ask is None) and quote.get("price"):
        px = float(quote["price"])
        slip = px * cfg.stock_slippage_bps / 10000
        return (px + slip if side == "buy" else px - slip), quote.get("price_src") or "last"
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None, "no_usable_quote"
    if instrument == "stock":
        slip = (ask if side == "buy" else bid) * cfg.stock_slippage_bps / 10000
        return (ask + slip if side == "buy" else bid - slip), quote.get("feed") or "quote"
    slip = (ask if side == "buy" else bid) * cfg.option_slippage_pct / 100
    return (ask + slip if side == "buy" else bid - slip), quote.get("feed") or "indicative"


# ---------------------------------------------------------------- evaluation
def _block_bootstrap_ci(values: list[float], days: list[str], n: int = 500, seed: int = 7) -> tuple[float, float] | None:
    if len(values) < 5:
        return None
    rng = np.random.default_rng(seed)
    by_day: dict[str, list[float]] = {}
    for v, d in zip(values, days):
        by_day.setdefault(d, []).append(v)
    keys = list(by_day)
    means = []
    for _ in range(n):
        pick = rng.choice(keys, size=len(keys), replace=True)
        s = [x for k in pick for x in by_day[k]]
        means.append(float(np.mean(s)))
    return round(float(np.percentile(means, 5)), 3), round(float(np.percentile(means, 95)), 3)


def summarize_trades(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [r for r in rows if r["status"] == "closed" and r.get("pnl_net") is not None]
    cens = [r for r in rows if r["status"] == "censored"]
    opn = [r for r in rows if r["status"] == "open"]
    out: dict[str, Any] = {"n": len(rows), "closed": len(closed), "open": len(opn), "censored": len(cens)}
    if not closed:
        return out
    pnl = [float(r["pnl_net"]) for r in closed]
    wins = [x for x in pnl if x > 0]; losses = [x for x in pnl if x <= 0]
    days = [r["entry_ts"][:10] for r in closed]
    eq = np.cumsum(pnl); dd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
    out.update({"unique_days": len(set(days)), "expectancy_net": round(float(np.mean(pnl)), 2), "median_net": round(float(np.median(pnl)), 2),
                "win_rate": round(len(wins) / len(pnl) * 100, 1), "avg_win": round(float(np.mean(wins)), 2) if wins else None, "avg_loss": round(float(np.mean(losses)), 2) if losses else None,
                "total_net": round(float(np.sum(pnl)), 2), "fees": round(float(sum((r.get("fees") or 0) + (r.get("slippage") or 0) for r in closed)), 2),
                "max_drawdown": round(dd, 2), "ci90_expectancy": _block_bootstrap_ci(pnl, days),
                "by_symbol": {k: round(float(sum(float(r["pnl_net"]) for r in closed if r["symbol"] == k)), 2) for k in sorted({r["symbol"] for r in closed if "symbol" in r})} if closed and "symbol" in closed[0] else None})
    return out


def summarize_outcomes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Analytical signed underlying returns at a horizon (variant A/B benchmark); bearish = negated returns, never a short holding."""
    ok = [r for r in rows if r["status"] == "ok" and r.get("ret_pct") is not None]
    cens = [r for r in rows if r["status"] != "ok"]
    out: dict[str, Any] = {"n": len(rows), "ok": len(ok), "censored": len(cens)}
    if not ok:
        return out
    v = [float(r["ret_pct"]) for r in ok]
    days = [r.get("day") or "" for r in ok]
    out.update({"unique_days": len(set(days)), "mean_pct": round(float(np.mean(v)), 3), "median_pct": round(float(np.median(v)), 3),
                "hit": round(float(np.mean([x > 0 for x in v]) * 100), 1), "worst": round(min(v), 2), "best": round(max(v), 2), "ci90_mean": _block_bootstrap_ci(v, days)})
    return out


# ---------------------------------------------------------------- runner (one tick = detect, classify, decide, fill, mark, resolve)
class ExperimentRunner:
    """Drives one experiment version. Owns nothing but the store; all data come through the market/broker/llm handed in, so
    tests use fakes and a fixed clock. Idempotent: every effect is keyed (signal dedupe key, decision per variant/portfolio)."""

    def __init__(self, store: ExperimentStore, exp: dict[str, Any], market: Any, broker: Any, llm: Any | None = None,
                 news_search: Any | None = None, clock: Any | None = None, universe: list[str] | None = None,
                 dollar_volume: dict[str, float] | None = None, chain_fn: Any | None = None) -> None:
        self.store, self.exp, self.m, self.b, self.llm = store, exp, market, broker, llm
        self.cfg = store.config(exp)
        self.news_search = news_search
        self.clock = clock or now_et
        self.store.clock = self.clock
        self.universe = universe or []
        self.dollar_volume = dollar_volume or {}
        self.chain_fn = chain_fn   # (underlying) -> list of contract rows with bid/ask/delta/dte/spread_pct/quote_age_s/multiplier/type
        self.counters: dict[str, int] = {}
        self._last_fetch: dt.datetime | None = None
        self._last_heartbeat: dt.datetime | None = None
        self._bars_cache: pd.DataFrame | None = None

    # ---- helpers
    def _bump(self, k: str, n: int = 1) -> None:
        self.counters[k] = self.counters.get(k, 0) + n

    def _quote(self, symbol: str) -> dict[str, Any]:
        """Underlying quote with receipt time and freshness flag. Fallback feeds are never executable."""
        try:
            s = self.m.snapshots([symbol]).get(symbol, {}) or {}
        except Exception as e:
            return {"error": str(e)[:80]}
        now = self.clock()
        fresh = bool(s.get("price")) and s.get("price_src") not in ("yfinance-fallback", None)
        return {"bid": s.get("bid"), "ask": s.get("ask"), "price": s.get("price"), "price_src": s.get("price_src"), "feed": s.get("price_src"),
                "ts_exchange": s.get("price_time"), "ts_receipt": now.isoformat(timespec="seconds"), "fresh": fresh}

    def _session(self, day: dt.date) -> tuple[dt.datetime, dt.datetime]:
        sb = session_bounds(self.b, day)
        if sb:
            return sb
        return dt.datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET), dt.datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET)

    # ---- tick
    def tick(self) -> dict[str, Any]:
        now = self.clock()
        if self.exp["status"] not in ("shadow", "live"):
            return {"skipped": "experiment disabled"}
        s_open, s_close = self._session(now.date())
        summary: dict[str, Any] = {"now": now.isoformat(timespec="seconds"), "new_signals": 0, "classified": 0, "fills": 0, "exits": 0}
        in_session = s_open <= now <= s_close
        if in_session:
            summary["new_signals"] = self._detect(now, s_open, s_close)
            summary["classified"] = self._classify_pending()
            summary["fills"] = self._place_and_fill(now, s_open, s_close)
        summary["exits"] = self._mark_and_exit(now, s_open, s_close)
        summary["outcomes"] = self._resolve_outcomes(now, s_open, s_close)
        summary["counters"] = dict(self.counters)
        return summary

    # ---- detection
    def _detect(self, now: dt.datetime, s_open: dt.datetime, s_close: dt.datetime) -> int:
        # every tick, with the bar fetch throttled to once a minute: a completed bar can arrive any time after the boundary,
        # so gating on the boundary itself races the feed and silently misses bars (that happened on the first day)
        syms = [s for s in self.universe if s != "SPY"]
        if not syms:
            self._heartbeat(now, "no universe")
            return 0
        if self._last_fetch is None or (now - self._last_fetch).total_seconds() >= 60 or self._bars_cache is None:
            try:
                self._bars_cache = self.m.bars_realtime(syms + ["SPY"], "5Min", limit=78 * (self.cfg.relvol_lookback_Runs + 2))
                self._last_fetch = now
            except Exception as e:
                self._bump("stale_data")
                self.store.event(self.exp["id"], "error", f"bars unavailable: {str(e)[:120]}")
                return 0
        bars = self._bars_cache
        if bars is None or bars.empty:
            self._bump("stale_data")
            self._heartbeat(now, "no bars")
            return 0
        spy = bars.xs("SPY", level="symbol", drop_level=False) if "SPY" in bars.index.get_level_values("symbol") else None
        rest = bars[bars.index.get_level_values("symbol") != "SPY"]
        cands, counters = detect_signals(rest, spy, self.cfg, now, s_open, s_close, self.dollar_volume or None)
        for k, v in counters.items():
            self._bump(k, v)
        self._heartbeat(now, f"universe {len(syms)}, bars {len(bars)} rows, candidates {len(cands)}")
        n = 0
        receipt = now.isoformat(timespec="seconds")
        for c in cands:
            key = c.pop("dedupe_key")
            sid = self.store.add_signal(self.exp["id"], key, c["symbol"], c["direction"], now.isoformat(timespec="seconds"), receipt, c["bar_end"], c,
                                       {"bars_feed": "iex", "bar_minutes": self.cfg.bar_minutes, "received_at": receipt})
            if sid is None:
                self._bump("duplicate")
                continue
            n += 1
            q = self._quote(c["symbol"])
            self.store.add_quote(sid, c["symbol"], "decision", q)
            px = q.get("price") if q.get("fresh") else None
            # variant A decides immediately on the mechanical signal (analytical book + cash-constrained stock portfolios)
            self.store.decide(sid, "A", "analytical", "accepted", "accepted", "mechanical signal", px, q.get("price_src"), q.get("ts_exchange"))
            for pf in ("p500", "p750"):
                ok, code, why = self._portfolio_admits(sid, "A", pf, c["direction"], "stock", px)
                self.store.decide(sid, "A", pf, "accepted" if ok else "infeasible", code, why, px, q.get("price_src"), q.get("ts_exchange"))
            if not self.cfg.classifier_enabled:
                self.store.set_classification(sid, "skipped", None, 0, "", PROMPT_VERSION)
        return n

    def _heartbeat(self, now: dt.datetime, note: str) -> None:
        """Once an hour: prove the detector ran and persist the rejection counters (they live in memory otherwise)."""
        if self._last_heartbeat is not None and (now - self._last_heartbeat).total_seconds() < 3600:
            return
        self._last_heartbeat = now
        self.store.event(self.exp["id"], "heartbeat", f"{note}; rejections so far {json.dumps(self.counters, sort_keys=True)}")

    # ---- classification (bounded: one signal per tick so the model is never monopolised)
    def _classify_pending(self) -> int:
        pend = self.store.signals(self.exp["id"], limit=5, class_state="pending")
        if not pend:
            return 0
        sig = pend[-1]
        if self.llm is None:
            self.store.set_classification(sig["id"], "error", {"error": "classifier unavailable"}, 0, "", PROMPT_VERSION)
            self._decide_b_c(sig["id"], "error", None)
            return 1
        state, payload, latency, model_id = classify_signal(self.llm, self.m, sig, self.cfg, self.news_search)
        if latency > self.cfg.classifier_timeout_s * 1000:
            state = "late"   # too late to act on: recorded, never filled retroactively
        self.store.set_classification(sig["id"], state, payload, latency, model_id, PROMPT_VERSION)
        self._decide_b_c(sig["id"], state, payload)
        return 1

    def _decide_b_c(self, sid: int, state: str, payload: dict[str, Any] | None) -> None:
        sig = self.store.signal(sid)
        q = self._quote(sig["symbol"])
        self.store.add_quote(sid, sig["symbol"], "decision", q)
        px = q.get("price") if q.get("fresh") else None
        if state in ("error", "late", "skipped"):
            code = "classifier_unavailable" if state != "skipped" else "classifier_abstained"
            for v in ("B", "C"):
                for pf in PORTFOLIOS:
                    self.store.decide(sid, v, pf, "abstained", code, f"classification {state}", px, q.get("price_src"), q.get("ts_exchange"))
            return
        ok, code = classifier_accepts(payload, sig["direction"])
        if not ok:
            for v in ("B", "C"):
                for pf in PORTFOLIOS:
                    self.store.decide(sid, v, pf, "rejected", code, (payload or {}).get("thesis") or "", px, q.get("price_src"), q.get("ts_exchange"))
            return
        self.store.decide(sid, "B", "analytical", "accepted", "accepted", (payload or {}).get("thesis") or "", px, q.get("price_src"), q.get("ts_exchange"))
        for pf in ("p500", "p750"):
            ok2, code2, why = self._portfolio_admits(sid, "B", pf, sig["direction"], "stock", px)
            self.store.decide(sid, "B", pf, "accepted" if ok2 else "infeasible", code2, why, px, q.get("price_src"), q.get("ts_exchange"))
        # variant C: pick a contract under each portfolio's budget; the analytical C book records the best contract regardless of budget
        for pf in PORTFOLIOS:
            ok3, code3, why3 = self._portfolio_admits(sid, "C", pf, sig["direction"], "option", px)
            self.store.decide(sid, "C", pf, "accepted" if ok3 else "infeasible", code3, why3, px, q.get("price_src"), q.get("ts_exchange"))

    # ---- portfolio admission (cash-constrained books; analytical book admits everything measurable)
    def _equity(self, pf: str) -> float:
        return {"p500": self.cfg.equity, "p750": self.cfg.comparison_equity}.get(pf, self.cfg.equity)

    def _portfolio_admits(self, sid: int, variant: str, pf: str, direction: str, instrument: str, px: float | None) -> tuple[bool, str, str]:
        if px is None:
            return False, "quote_stale", "no fresh executable quote at decision"
        if instrument == "option" and self.chain_fn is None:
            return False, "no_suitable_contract_within_budget", "no option chain source"
        if pf == "analytical":
            return True, "accepted", "analytical book"
        if instrument == "stock" and direction == "bearish":
            return False, "bearish_not_deployable", "cash account: bearish stock signals are analytical only"
        port = self.store.portfolio(self.exp["id"], variant, pf, self._equity(pf))
        if port["blocked"]:
            return False, "breaker", port.get("blocked_reason") or "breaker tripped"
        open_here = [t for t in self.store.open_trades(self.exp["id"]) if t["variant"] == variant and t["portfolio"] == pf]
        if len(open_here) >= self.cfg.max_concurrent:
            return False, "concurrency", f"{len(open_here)} open >= max {self.cfg.max_concurrent}"
        day_loss = (port["day_start_equity"] - port["equity"]) / max(1e-9, port["day_start_equity"]) * 100
        if day_loss >= self.cfg.daily_loss_pct:
            self.store.update_portfolio(self.exp["id"], variant, pf, blocked=True, reason=f"daily loss {day_loss:.2f}% >= {self.cfg.daily_loss_pct}%")
            return False, "breaker", f"daily loss {day_loss:.2f}%"
        if instrument == "option":
            budget = port["equity"] * self.cfg.max_single_option_pct / 100
            agg_open = sum((t["entry_px"] or 0) * (t["qty"] or 0) * (t["multiplier"] or 100) for t in open_here if t["instrument"] != "stock")
            if agg_open + budget > port["equity"] * self.cfg.max_agg_option_pct / 100 + 1e-9:
                return False, "concurrency", "aggregate option exposure cap"
            return True, "accepted", f"budget ${budget:.2f}"
        return True, "accepted", f"stock cap ${port['equity'] * self.cfg.stock_pos_pct / 100:.2f}"

    # ---- fills: first eligible observation after decision + latency; never the triggering quote
    def _place_and_fill(self, now: dt.datetime, s_open: dt.datetime, s_close: dt.datetime) -> int:
        fills = 0
        rows = self.store.conn.execute(
            "SELECT d.*, s.symbol, s.direction, s.experiment_id FROM exp_decisions d JOIN exp_signals s ON s.id=d.signal_id "
            "WHERE s.experiment_id=? AND d.decision='accepted' AND d.portfolio IN ('p500','p750') "
            "AND NOT EXISTS (SELECT 1 FROM exp_shadow_trades t WHERE t.signal_id=d.signal_id AND t.variant=d.variant AND t.portfolio=d.portfolio) "
            "AND NOT EXISTS (SELECT 1 FROM exp_decisions f WHERE f.signal_id=d.signal_id AND f.variant=d.variant AND f.portfolio=d.portfolio AND f.reason_code LIKE 'fill_%')",
            (self.exp["id"],)).fetchall()
        for d in rows:
            d = dict(d)
            decided = dt.datetime.fromisoformat(d["decided_at"])
            if (now - decided).total_seconds() < self.cfg.latency_s:
                continue
            if now > s_close - dt.timedelta(minutes=self.cfg.close_buffer_min + self.cfg.horizon_min):
                self._final_decision(d, "horizon_past_close", "too late after latency")
                continue
            sig = self.store.signal(d["signal_id"])
            port = self.store.portfolio(self.exp["id"], d["variant"], d["portfolio"], self._equity(d["portfolio"]))
            if d["variant"] in ("A", "B"):
                q = self._quote(sig["symbol"])
                self.store.add_quote(d["signal_id"], sig["symbol"], "entry", q)
                if not q.get("fresh"):
                    self._final_decision(d, "quote_stale", "no fresh quote at fill time")
                    continue
                px, src = shadow_fill_price("buy", q, self.cfg, "stock")
                if px is None:
                    self._final_decision(d, "quote_stale", src)
                    continue
                notional = port["equity"] * self.cfg.stock_pos_pct / 100
                qty = math.floor(notional / px * 1e6) / 1e6            # floor: the cap is never exceeded by rounding
                stop_dist = abs(px - sig["features"]["edge"]) / px
                budget_stop = port["equity"] * self.cfg.stock_stop_budget_pct / 100
                if stop_dist * qty * px > budget_stop:   # intended-loss budget binds before the position cap
                    qty = math.floor(budget_stop / (stop_dist * px) * 1e6) / 1e6 if stop_dist > 0 else qty
                notional = qty * px
                if notional < 5:
                    self._final_decision(d, "cash_unavailable", "stop budget leaves no viable size")
                    continue
                tid = self.store.open_trade(d["signal_id"], self.exp["id"], d["variant"], d["portfolio"], "stock", None, 1.0, qty, sig["direction"],
                                            now.isoformat(timespec="seconds"), px, src, float(sig["features"]["edge"]),
                                            (now + dt.timedelta(minutes=self.cfg.horizon_min)).isoformat(timespec="seconds"), 0.0, round(qty * px * self.cfg.stock_slippage_bps / 10000, 4),
                                            f"intended stop loss ${stop_dist * notional:.2f} of ${budget_stop:.2f} budget; slippage can exceed it")
                self.store.update_portfolio(self.exp["id"], d["variant"], d["portfolio"], equity=port["equity"])
                self._final_decision(d, "fill_ok", f"shadow trade #{tid}")
                fills += 1
            else:
                chain = self.chain_fn(sig["symbol"]) if self.chain_fn else []
                budget = port["equity"] * self.cfg.max_single_option_pct / 100
                contract, code = select_contract(chain, sig["direction"], self.cfg, budget, now)
                if contract is None:
                    self._final_decision(d, code, f"no contract under ${budget:.2f} within policy (dte {self.cfg.dte_min}-{self.cfg.dte_max}, |delta| {self.cfg.delta_lo}-{self.cfg.delta_hi}, spread <= {self.cfg.max_spread_pct}%)")
                    continue
                q = {"bid": contract.get("bid"), "ask": contract.get("ask"), "feed": contract.get("feed") or "indicative", "ts_receipt": now.isoformat(timespec="seconds"),
                     "ts_exchange": contract.get("quote_time"), "fresh": True, "bid_size": contract.get("bid_size"), "ask_size": contract.get("ask_size")}
                self.store.add_quote(d["signal_id"], contract["symbol"], "entry", q)
                px, src = shadow_fill_price("buy", q, self.cfg, "option")
                mult = float(contract.get("multiplier") or 100)
                if px is None or px * mult + self.cfg.fee_per_contract > budget:
                    self._final_decision(d, "no_suitable_contract_within_budget", "post-slippage cost over budget")
                    continue
                tid = self.store.open_trade(d["signal_id"], self.exp["id"], "C", d["portfolio"], "call" if sig["direction"] == "bullish" else "put", contract["symbol"], mult, 1,
                                            sig["direction"], now.isoformat(timespec="seconds"), px, src, None,
                                            (now + dt.timedelta(minutes=self.cfg.horizon_min)).isoformat(timespec="seconds"), self.cfg.fee_per_contract, round((px - contract["ask"]) * mult, 4),
                                            f"full premium at risk ${px * mult:.2f} (+fees); stop is contingent, indicative feed")
                self._final_decision(d, "fill_ok", f"shadow trade #{tid} {occ_human(contract['symbol'])}")
                fills += 1
        return fills

    def _final_decision(self, d: dict[str, Any], code: str, why: str) -> None:
        self.store.conn.execute("INSERT OR IGNORE INTO exp_decisions(signal_id, variant, portfolio, decision, reason_code, reason, decided_at) VALUES (?,?,?,?,?,?,?)",
                                (d["signal_id"], d["variant"], d["portfolio"] + ":fill", "filled" if code == "fill_ok" else "unfilled", code, why[:400], self.clock().isoformat(timespec="seconds")))
        self.store.conn.commit()
        if code != "fill_ok":
            self._bump(code)

    # ---- marks and exits
    def _mark_and_exit(self, now: dt.datetime, s_open: dt.datetime, s_close: dt.datetime) -> int:
        n = 0
        forced = now >= s_close - dt.timedelta(minutes=self.cfg.close_buffer_min)
        for t in self.store.open_trades(self.exp["id"]):
            time_exit = dt.datetime.fromisoformat(t["time_exit_ts"])
            if t["instrument"] == "stock":
                q = self._quote(t["contract"] or self._sym_of(t))
                self.store.add_quote(t["signal_id"], self._sym_of(t), "observe", q)
                px = q.get("price") if q.get("fresh") else None
                stop_hit = px is not None and t["stop_level"] is not None and px <= float(t["stop_level"])
                if not (stop_hit or now >= time_exit or forced):
                    continue
                if px is None:
                    if forced or now >= time_exit + dt.timedelta(minutes=30):
                        self.store.close_trade(t["id"], now.isoformat(timespec="seconds"), None, "no_exit_quote", status="censored")
                        n += 1
                    continue
                exit_px, _ = shadow_fill_price("sell", q, self.cfg, "stock")
                reason = "stop" if stop_hit else ("session_close" if forced else "time_exit")
                self.store.close_trade(t["id"], now.isoformat(timespec="seconds"), exit_px, reason)
            else:
                rows = self.chain_fn(self._sym_of(t)) if self.chain_fn else []
                c = next((r for r in rows if r.get("symbol") == t["contract"]), None)
                q = {"bid": c.get("bid") if c else None, "ask": c.get("ask") if c else None, "feed": "indicative", "ts_receipt": now.isoformat(timespec="seconds"), "fresh": bool(c and c.get("bid"))}
                self.store.add_quote(t["signal_id"], t["contract"], "observe", q)
                if not (now >= time_exit or forced):
                    continue
                exit_px, _ = shadow_fill_price("sell", q, self.cfg, "option")
                if exit_px is None:
                    if forced or now >= time_exit + dt.timedelta(minutes=30):
                        self.store.close_trade(t["id"], now.isoformat(timespec="seconds"), None, "no_exit_quote", status="censored")
                        n += 1
                    continue
                self.store.close_trade(t["id"], now.isoformat(timespec="seconds"), exit_px, "session_close" if forced else "time_exit", fees_extra=self.cfg.fee_per_contract)
            n += 1
            closed = dict(self.store.conn.execute("SELECT * FROM exp_shadow_trades WHERE id=?", (t["id"],)).fetchone())
            if closed.get("pnl_net") is not None:
                port = self.store.portfolio(self.exp["id"], t["variant"], t["portfolio"], self._equity(t["portfolio"]))
                self.store.update_portfolio(self.exp["id"], t["variant"], t["portfolio"], equity=round(port["equity"] + closed["pnl_net"], 2))
        return n

    def _sym_of(self, t: dict[str, Any]) -> str:
        if t["instrument"] == "stock":
            return self.store.signal(t["signal_id"])["symbol"]
        return parse_occ(t["contract"])["underlying"] if parse_occ(t["contract"]) else t["contract"]

    # ---- analytical outcomes at fixed horizons from the decision price (signed by direction; bearish never a holding)
    def _resolve_outcomes(self, now: dt.datetime, s_open: dt.datetime, s_close: dt.datetime) -> int:
        n = 0
        horizons = sorted({self.cfg.horizon_min, *self.cfg.diag_horizons})
        rows = self.store.conn.execute(
            "SELECT s.id, s.symbol, s.direction, s.ts_eligible, d.decision_price FROM exp_signals s JOIN exp_decisions d ON d.signal_id=s.id AND d.variant='A' AND d.portfolio='analytical' "
            "WHERE s.experiment_id=? AND (SELECT COUNT(*) FROM exp_outcomes o WHERE o.signal_id=s.id) < ?", (self.exp["id"], len(horizons))).fetchall()
        for r in rows:
            r = dict(r)
            t0 = dt.datetime.fromisoformat(r["ts_eligible"])
            done = {o["horizon_min"] for o in self.store.outcomes(r["id"])}
            for h in horizons:
                if h in done:
                    continue
                t_end = t0 + dt.timedelta(minutes=h)
                if t_end > s_close:
                    self.store.set_outcome(r["id"], h, r["decision_price"], None, None, None, None, None, "censored_session_end")
                    n += 1
                    continue
                if now < t_end + dt.timedelta(minutes=1):
                    continue
                if r["decision_price"] is None:
                    self.store.set_outcome(r["id"], h, None, None, None, None, None, None, "censored_no_decision_price")
                    n += 1
                    continue
                try:
                    bars = self.m.bars_realtime([r["symbol"]], "5Min", limit=100)
                    g = bars.xs(r["symbol"], level="symbol") if not bars.empty else pd.DataFrame()
                    g = g[(g.index > t0) & (g.index <= t_end)]
                except Exception:
                    g = pd.DataFrame()
                if g.empty:
                    self.store.set_outcome(r["id"], h, r["decision_price"], None, None, None, None, None, "censored_no_bars")
                    n += 1
                    continue
                sign = 1.0 if r["direction"] == "bullish" else -1.0
                px_end = float(g["close"].iloc[-1]); p0 = float(r["decision_price"])
                ret = (px_end / p0 - 1) * 100 * sign
                mfe = (float(g["high"].max()) / p0 - 1) * 100 * sign if sign > 0 else (1 - float(g["low"].min()) / p0) * 100
                mae = (float(g["low"].min()) / p0 - 1) * 100 * sign if sign > 0 else (1 - float(g["high"].max()) / p0) * 100
                self.store.set_outcome(r["id"], h, p0, px_end, t_end.isoformat(timespec="seconds"), round(ret, 4), round(mfe, 4), round(mae, 4), "ok")
                n += 1
        return n

    # ---- report
    def report(self) -> dict[str, Any]:
        exp_id = self.exp["id"]
        conn = self.store.conn
        sigs = [dict(r) for r in conn.execute("SELECT id, symbol, direction, session_date, class_state FROM exp_signals WHERE experiment_id=?", (exp_id,)).fetchall()]
        out: dict[str, Any] = {"experiment": {k: self.exp[k] for k in ("id", "name", "version", "status", "config_hash", "created_at")}, "config": self.cfg.model_dump(),
                               "signals": len(sigs), "unique_days": len({s["session_date"] for s in sigs}), "detector_version": DETECTOR_VERSION, "prompt_version": PROMPT_VERSION,
                               "class_states": {}, "counters": dict(self.counters), "variants": {}}
        for s in sigs:
            out["class_states"][s["class_state"]] = out["class_states"].get(s["class_state"], 0) + 1
        H = self.cfg.horizon_min
        def outcomes_for(ids: set[int]) -> list[dict[str, Any]]:
            if not ids:
                return []
            q = f"SELECT o.*, s.session_date AS day FROM exp_outcomes o JOIN exp_signals s ON s.id=o.signal_id WHERE o.horizon_min=? AND o.signal_id IN ({','.join('?' * len(ids))})"
            return [dict(r) for r in conn.execute(q, (H, *ids)).fetchall()]
        dec = [dict(r) for r in conn.execute("SELECT d.*, s.symbol FROM exp_decisions d JOIN exp_signals s ON s.id=d.signal_id WHERE s.experiment_id=?", (exp_id,)).fetchall()]
        acc_A = {d["signal_id"] for d in dec if d["variant"] == "A" and d["portfolio"] == "analytical" and d["decision"] == "accepted"}
        acc_B = {d["signal_id"] for d in dec if d["variant"] == "B" and d["portfolio"] == "analytical" and d["decision"] == "accepted"}
        rej_B = {d["signal_id"] for d in dec if d["variant"] == "B" and d["portfolio"] == "analytical" and d["decision"] == "rejected"}
        abs_B = {d["signal_id"] for d in dec if d["variant"] == "B" and d["portfolio"] == "analytical" and d["decision"] == "abstained"}
        out["variants"]["A"] = {"analytical": summarize_outcomes(outcomes_for(acc_A)), "n_signals": len(acc_A)}
        out["variants"]["B"] = {"analytical_accepted": summarize_outcomes(outcomes_for(acc_B)), "analytical_rejected": summarize_outcomes(outcomes_for(rej_B)),
                                "abstained": len(abs_B), "accepted": len(acc_B), "rejected": len(rej_B)}
        trades = self.store.trades(exp_id)
        for t in trades:
            t["symbol"] = self.store.signal(t["signal_id"])["symbol"]
        for v in VARIANTS:
            for pf in ("p500", "p750"):
                rows = [t for t in trades if t["variant"] == v and t["portfolio"] == pf]
                port = conn.execute("SELECT equity, day_start_equity, blocked, blocked_reason FROM exp_portfolios WHERE experiment_id=? AND variant=? AND portfolio=?", (exp_id, v, pf)).fetchone()
                d = summarize_trades(rows)
                if port:
                    d["equity"] = port["equity"]; d["blocked"] = bool(port["blocked"]); d["blocked_reason"] = port["blocked_reason"]
                if v == "C":
                    d["calls"] = summarize_trades([t for t in rows if t["instrument"] == "call"]); d["puts"] = summarize_trades([t for t in rows if t["instrument"] == "put"])
                out["variants"].setdefault(v, {})[pf] = d
        infeas = {}
        for d in dec:
            if d["decision"] in ("infeasible", "unfilled", "abstained", "rejected"):
                infeas[d["reason_code"]] = infeas.get(d["reason_code"], 0) + 1
        out["feasibility"] = infeas
        out["data_quality"] = {"options_feed": "indicative (delayed); variant C fills are exploratory, not executable validation", "bars_feed": "IEX real-time 5-minute completed bars",
                               "relvol_basis": "IEX volume vs IEX history at the same time of day", "poll_cadence_s": 30}
        return out
