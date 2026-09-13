"""Intraday experiment: deterministic clock, fake broker/market/model, synthetic bars and quotes. No network."""
import datetime as dt
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from munchkin.experiments import (DETECTOR_VERSION, ExperimentConfig, ExperimentRunner, ExperimentStore, classifier_accepts, config_hash,
                                  detect_signals, select_contract, shadow_fill_price, tighten_limits)
from munchkin.util import ET

DAY = dt.date(2026, 9, 14)   # a Monday


def T(h, m=0, day=DAY):
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ET)


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def make_bars(spec, prior_days=3, today=DAY, last_end=None):
    """spec: {symbol: {"open": px, "or": (hi, lo), "path": [(HH:MM, close), ...], "vol": today_vol_per_bar, "hist_vol": prior_vol_per_bar}}.
    Prior sessions are flat at the open with hist_vol; today opens with a 30-minute range then follows `path`."""
    frames = []
    for sym, s in spec.items():
        rows = []
        prior = []
        d = today
        while len(prior) < prior_days:
            d -= dt.timedelta(days=1)
            if d.weekday() < 5:
                prior.append(d)
        for d in reversed(prior):
            for i in range(78):
                ts = dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET) + dt.timedelta(minutes=5 * i)
                rows.append((sym, ts, s["open"], s["open"] * 1.001, s["open"] * 0.999, s["open"], s.get("hist_vol", 1000)))
        hi, lo = s["or"]
        for i in range(6):   # opening range 09:30-10:00
            ts = T(9, 30 + 5 * i, today)
            rows.append((sym, ts, s["open"], hi, lo, s["open"], s.get("vol", 1000)))
        for hm, close in s.get("path", []):
            h, m = map(int, hm.split(":"))
            ts = T(h, m, today)
            if last_end is not None and ts + dt.timedelta(minutes=5) > last_end:
                break
            rows.append((sym, ts, close, max(close, hi) if close > hi else close * 1.001, min(close, lo) if close < lo else close * 0.999, close, s.get("vol", 1000)))
        frames += rows
    df = pd.DataFrame(frames, columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"]).set_index(["symbol", "timestamp"]).sort_index()
    return df


class FakeBroker:
    def __init__(self, close="16:00"): self.close = close
    def calendar(self, a, b): return [{"date": a.isoformat(), "open": "09:30", "close": self.close}]
    def option_contract(self, sym): return {"size": 10 if sym.startswith("ADJ") else 100}


class FakeMarket:
    def __init__(self, bars, quotes=None, news=None):
        self.bars_df, self.quotes, self.news_items = bars, quotes or {}, news or []
    def bars_realtime(self, symbols, tf="5Min", limit=100):
        return self.bars_df[self.bars_df.index.get_level_values("symbol").isin([s.upper() for s in symbols])]
    def snapshots(self, symbols):
        return {s: dict(self.quotes.get(s, {})) for s in symbols}
    def news(self, symbols, n, hours): return list(self.news_items)


class FakeLLM:
    model = "fake-27b"
    def __init__(self, reply=None, fail=False, sleep_ms=0):
        self.reply, self.fail, self.sleep_ms = reply, fail, sleep_ms
    def chat(self, messages, **k):
        if self.fail:
            raise RuntimeError("model down")
        if self.sleep_ms:
            import time; time.sleep(self.sleep_ms / 1000)
        return {"message": {"content": json.dumps(self.reply or {"event_type": "product", "direction": "bullish", "novelty": "new", "substantive": True,
                                                                  "source_quality": "major_outlet", "abstain": False, "thesis": "fresh permit"})}}


BULL = {"BRK": {"open": 100.0, "or": (101.0, 99.0), "path": [("10:00", 100.5), ("10:05", 101.3), ("10:10", 101.4)], "vol": 3000, "hist_vol": 1000},
        "BEAR": {"open": 50.0, "or": (50.5, 49.5), "path": [("10:00", 49.8), ("10:05", 49.2), ("10:10", 49.1)], "vol": 3000, "hist_vol": 1000},
        "QUIET": {"open": 30.0, "or": (30.3, 29.7), "path": [("10:00", 30.0), ("10:05", 30.5), ("10:10", 30.6)], "vol": 900, "hist_vol": 1000},
        "SPY": {"open": 500.0, "or": (501, 499), "path": [("10:00", 500.2), ("10:05", 500.4), ("10:10", 500.5)], "vol": 1000, "hist_vol": 1000}}


def store(tmp_path):
    class J:
        def __init__(self):
            self.conn = sqlite3.connect(str(tmp_path / "exp.db"), check_same_thread=False); self.conn.row_factory = sqlite3.Row
    return ExperimentStore(J())


def runner(tmp_path, clock, market, llm=None, cfg=None, broker=None, chain=None, st=None):
    st = st or store(tmp_path)
    exp = st.register(cfg or ExperimentConfig())
    st.set_status(exp["id"], "shadow"); exp = st.get(exp["id"])
    dv = {"BRK": 50e6, "BEAR": 50e6, "QUIET": 50e6}
    return st, ExperimentRunner(st, exp, market, broker or FakeBroker(), llm=llm, clock=clock, universe=["BRK", "BEAR", "QUIET"], dollar_volume=dv, chain_fn=chain)


# ---------------------------------------------------------------- detector
def test_detector_finds_continuation_and_counts_rejections():
    bars = make_bars(BULL)
    now = T(10, 16)
    spy = bars.xs("SPY", level="symbol", drop_level=False)
    cands, counters = detect_signals(bars[bars.index.get_level_values("symbol") != "SPY"], spy, ExperimentConfig(), now, T(9, 30), T(16, 0), {"BRK": 50e6, "BEAR": 50e6, "QUIET": 50e6})
    got = {c["symbol"]: c for c in cands}
    assert set(got) == {"BRK", "BEAR"}
    assert got["BRK"]["direction"] == "bullish" and got["BRK"]["relvol"] >= 1.5 and got["BRK"]["edge"] == 101.0 and got["BRK"]["dedupe_key"].startswith(DETECTOR_VERSION)
    assert got["BEAR"]["direction"] == "bearish" and counters.get("relvol_below") == 1     # QUIET broke out on thin volume
    # stale data: a bar that ended long ago is not fresh
    cands2, counters2 = detect_signals(bars[bars.index.get_level_values("symbol") == "BRK"], spy, ExperimentConfig(freshness_s=60), T(11, 0), T(9, 30), T(16, 0))
    assert not cands2 and counters2.get("stale_data") == 1


# ---------------------------------------------------------------- lifecycle
def test_runner_dedupes_across_ticks_and_restarts(tmp_path):
    clock = Clock(T(10, 16))
    m = FakeMarket(make_bars(BULL), quotes={"BRK": {"price": 101.4, "bid": 101.3, "ask": 101.5, "price_src": "iex-realtime"}, "BEAR": {"price": 49.1, "bid": 49.0, "ask": 49.2, "price_src": "iex-realtime"}})
    st, r = runner(tmp_path, clock, m, llm=FakeLLM())
    a = r.tick(); b = r.tick()
    assert a["new_signals"] == 2 and b["new_signals"] == 0
    r2 = ExperimentRunner(st, r.exp, m, FakeBroker(), llm=FakeLLM(), clock=clock, universe=["BRK", "BEAR", "QUIET"], dollar_volume=r.dollar_volume)
    clock.t = T(10, 21)
    assert r2.tick()["new_signals"] == 0 and len(st.signals(r.exp["id"])) == 2


def test_variant_a_decisions_and_bearish_is_analytical_only(tmp_path):
    clock = Clock(T(10, 16))
    m = FakeMarket(make_bars(BULL), quotes={"BRK": {"price": 101.4, "bid": 101.3, "ask": 101.5, "price_src": "iex-realtime"}, "BEAR": {"price": 49.1, "bid": 49.0, "ask": 49.2, "price_src": "iex-realtime"}})
    st, r = runner(tmp_path, clock, m, llm=None)
    r.tick()
    bear = next(s for s in st.signals(r.exp["id"]) if s["symbol"] == "BEAR")
    d = {(x["variant"], x["portfolio"]): x for x in st.decisions(bear["id"])}
    assert d[("A", "analytical")]["decision"] == "accepted" and d[("A", "p500")]["reason_code"] == "bearish_not_deployable"
    assert not [t for t in st.open_trades() if t["direction"] == "bearish" and t["instrument"] == "stock"]


def test_classifier_unavailable_means_abstain_not_baseline(tmp_path):
    clock = Clock(T(10, 16))
    m = FakeMarket(make_bars(BULL), quotes={"BRK": {"price": 101.4, "bid": 101.3, "ask": 101.5, "price_src": "iex-realtime"}, "BEAR": {"price": 49.1, "price_src": "iex-realtime"}})
    st, r = runner(tmp_path, clock, m, llm=FakeLLM(fail=True))
    r.tick(); r.tick()
    brk = next(s for s in st.signals(r.exp["id"]) if s["symbol"] == "BRK")
    assert brk["class_state"] == "error"
    d = {(x["variant"], x["portfolio"]): x for x in st.decisions(brk["id"])}
    assert d[("B", "p500")]["decision"] == "abstained" and d[("B", "p500")]["reason_code"] == "classifier_unavailable"
    assert d[("A", "p500")]["decision"] == "accepted"          # the mechanical variant still stands on its own


def test_classification_is_immutable(tmp_path):
    st = store(tmp_path)
    exp = st.register(ExperimentConfig())
    sid = st.add_signal(exp["id"], "k1", "BRK", "bullish", T(10, 15).isoformat(), T(10, 15).isoformat(), T(10, 15).isoformat(), {"edge": 101}, {})
    assert st.set_classification(sid, "classified", {"event_type": "product", "excerpt": "original"}, 800, "m", "v1")
    assert st.set_classification(sid, "classified", {"event_type": "product", "excerpt": "revised later"}, 900, "m", "v1") is False
    assert st.signal(sid)["classification"]["excerpt"] == "original"


def test_late_model_never_fills_retroactively(tmp_path):
    clock = Clock(T(10, 16))
    m = FakeMarket(make_bars(BULL), quotes={"BRK": {"price": 101.4, "bid": 101.3, "ask": 101.5, "price_src": "iex-realtime"}, "BEAR": {"price": 49.1, "price_src": "iex-realtime"}})
    cfg = ExperimentConfig(classifier_timeout_s=0)        # any latency is late
    st, r = runner(tmp_path, clock, m, llm=FakeLLM(sleep_ms=20), cfg=cfg)
    r.tick(); r.tick()
    brk = next(s for s in st.signals(r.exp["id"]) if s["symbol"] == "BRK")
    assert brk["class_state"] == "late"
    assert all(x["decision"] == "abstained" for x in st.decisions(brk["id"]) if x["variant"] in ("B", "C"))


def test_shadow_fill_waits_for_latency_and_needs_a_fresh_quote(tmp_path):
    clock = Clock(T(10, 16))
    quotes = {"BRK": {"price": 101.4, "bid": 101.3, "ask": 101.5, "price_src": "iex-realtime"}, "BEAR": {"price": 49.1, "price_src": "iex-realtime"}}
    m = FakeMarket(make_bars(BULL), quotes=quotes)
    st, r = runner(tmp_path, clock, m, llm=FakeLLM())
    r.tick(); r.tick()
    assert r.tick()["fills"] == 0                                       # inside the latency window
    quotes["BRK"] = {"price": 101.6, "price_src": "yfinance-fallback"}   # fallback feed at fill time: not executable
    clock.t = T(10, 18)
    assert r.tick()["fills"] == 0
    fills = [x for x in st.decisions(next(s["id"] for s in st.signals(r.exp["id"]) if s["symbol"] == "BRK")) if x["portfolio"].endswith(":fill") and x["variant"] in ("A", "B")]
    assert fills and all(f["reason_code"] == "quote_stale" for f in fills)


def test_stock_fill_exits_and_portfolio_accounting(tmp_path):
    clock = Clock(T(10, 16))
    quotes = {"BRK": {"price": 101.4, "bid": 101.3, "ask": 101.5, "price_src": "iex-realtime"}, "BEAR": {"price": 49.1, "price_src": "iex-realtime"}}
    m = FakeMarket(make_bars(BULL), quotes=quotes)
    st, r = runner(tmp_path, clock, m, llm=FakeLLM())
    r.tick(); r.tick()
    clock.t = T(10, 18)
    assert r.tick()["fills"] >= 2                                       # A and B stock books for p500 and p750
    t = [x for x in st.open_trades() if x["variant"] == "A" and x["portfolio"] == "p500"][0]
    assert t["instrument"] == "stock" and t["entry_px"] > 101.5 and t["stop_level"] == 101.0
    assert abs(t["qty"] * t["entry_px"]) <= 500 * 0.10 + 1e-6            # position cap (stop budget may shrink it further)
    quotes["BRK"] = {"price": 100.9, "bid": 100.8, "ask": 101.0, "price_src": "iex-realtime"}   # through the range edge
    clock.t = T(10, 25)
    assert r.tick()["exits"] >= 2
    closed = [x for x in st.trades(r.exp["id"]) if x["status"] == "closed" and x["variant"] == "A" and x["portfolio"] == "p500"][0]
    assert closed["exit_reason"] == "stop" and closed["exit_px"] < 100.8 and closed["pnl_net"] < 0
    port = st.portfolio(r.exp["id"], "A", "p500", 500)
    assert port["equity"] < 500 and port["equity"] == 500 + round(closed["pnl_net"], 2)


def test_missing_exit_quote_is_censored_not_dropped(tmp_path):
    clock = Clock(T(10, 16))
    quotes = {"BRK": {"price": 101.4, "bid": 101.3, "ask": 101.5, "price_src": "iex-realtime"}, "BEAR": {"price": 49.1, "price_src": "iex-realtime"}}
    m = FakeMarket(make_bars(BULL), quotes=quotes)
    st, r = runner(tmp_path, clock, m, llm=FakeLLM())
    r.tick(); r.tick(); clock.t = T(10, 18); r.tick()
    quotes["BRK"] = {"price": None, "price_src": None}
    clock.t = T(13, 0)                                                   # past the time exit + grace, still no quote
    r.tick()
    rows = st.trades(r.exp["id"])
    assert rows and all(x["status"] == "censored" and x["exit_reason"] == "no_exit_quote" for x in rows)


def test_option_budget_rejects_but_keeps_the_signal(tmp_path):
    clock = Clock(T(10, 16))
    quotes = {"BRK": {"price": 101.4, "bid": 101.3, "ask": 101.5, "price_src": "iex-realtime"}, "BEAR": {"price": 49.1, "price_src": "iex-realtime"}}
    chain = lambda u: [{"symbol": "BRK260925C00100000", "type": "call", "dte": 11, "delta": 0.55, "bid": 0.24, "ask": 0.25, "spread_pct": 4.0, "quote_age_s": 5, "multiplier": 100}]
    st, r = runner(tmp_path, clock, FakeMarket(make_bars(BULL), quotes=quotes), llm=FakeLLM(), chain=chain)
    r.tick(); r.tick(); clock.t = T(10, 18); r.tick()
    brk = next(s for s in st.signals(r.exp["id"]) if s["symbol"] == "BRK")
    fills = {x["portfolio"]: x for x in st.decisions(brk["id"]) if x["variant"] == "C" and x["portfolio"].endswith(":fill")}
    assert fills["p500:fill"]["reason_code"] == "no_suitable_contract_within_budget"   # $25 > 2% of $500
    assert fills["p750:fill"]["reason_code"] == "no_suitable_contract_within_budget"   # $25 > $15
    assert st.signal(brk["id"]) is not None and not [t for t in st.trades(r.exp["id"]) if t["variant"] == "C"]


def test_contract_policy_is_never_relaxed_for_affordability():
    cfg = ExperimentConfig()
    chain = [{"symbol": "X1", "type": "call", "dte": 30, "delta": 0.55, "bid": 0.05, "ask": 0.06, "spread_pct": 5, "quote_age_s": 5, "multiplier": 100},   # too far out
             {"symbol": "X2", "type": "call", "dte": 10, "delta": 0.30, "bid": 0.05, "ask": 0.06, "spread_pct": 5, "quote_age_s": 5, "multiplier": 100},   # delta too low
             {"symbol": "X3", "type": "call", "dte": 10, "delta": 0.55, "bid": 0.50, "ask": 0.52, "spread_pct": 4, "quote_age_s": 900, "multiplier": 100}]  # stale
    c, code = select_contract(chain, "bullish", cfg, budget=1000.0, now=T(10, 20))
    assert c is None and code in ("quote_stale", "no_suitable_contract_within_budget")
    good = {"symbol": "X4", "type": "call", "dte": 10, "delta": 0.55, "bid": 0.50, "ask": 0.52, "spread_pct": 4, "quote_age_s": 5, "multiplier": 100}
    c, code = select_contract(chain + [good], "bullish", cfg, budget=1000.0, now=T(10, 20))
    assert c["symbol"] == "X4" and c["cost"] == 52.65                     # 0.52 x 100 + $0.65 fee: exposure is dollars, not the premium quote
    assert select_contract([good], "bullish", cfg, budget=50.0, now=T(10, 20))[1] == "no_suitable_contract_within_budget"
    assert select_contract([good], "bearish", cfg, budget=1000.0, now=T(10, 20))[0] is None   # a call never serves a bearish signal


def test_shadow_fill_prices_are_conservative():
    cfg = ExperimentConfig()
    assert shadow_fill_price("buy", {"bid": 1.0, "ask": 1.1, "feed": "indicative"}, cfg, "option")[0] == pytest.approx(1.1 * 1.01)
    assert shadow_fill_price("sell", {"bid": 1.0, "ask": 1.1}, cfg, "option")[0] == pytest.approx(1.0 * 0.99)
    assert shadow_fill_price("buy", {"bid": 1.2, "ask": 1.1}, cfg, "option")[0] is None      # crossed
    assert shadow_fill_price("buy", {"bid": None, "ask": None}, cfg, "stock")[0] is None       # no quote, no price
    assert shadow_fill_price("buy", {"bid": 100, "ask": 100.1, "feed": "iex"}, cfg, "stock")[0] > 100.1


def test_breaker_blocks_and_persists_across_restart(tmp_path):
    st = store(tmp_path); st.clock = Clock(T(10, 0))                     # one clock for the store and both runners
    exp = st.register(ExperimentConfig()); st.set_status(exp["id"], "shadow"); exp = st.get(exp["id"])
    st.portfolio(exp["id"], "A", "p500", 500.0)
    st.update_portfolio(exp["id"], "A", "p500", equity=489.0)            # -2.2% on the day
    r = ExperimentRunner(st, exp, FakeMarket(make_bars(BULL)), FakeBroker(), clock=Clock(T(10, 30)))
    ok, code, _ = r._portfolio_admits(1, "A", "p500", "bullish", "stock", 101.0)
    assert not ok and code == "breaker"
    r2 = ExperimentRunner(st, exp, FakeMarket(make_bars(BULL)), FakeBroker(), clock=Clock(T(11, 0)))   # "restart"
    assert r2._portfolio_admits(1, "A", "p500", "bullish", "stock", 101.0)[1] == "breaker"
    st.update_portfolio(exp["id"], "A", "p500", equity=505.0)            # marked equity recovers: still blocked until the operator clears it
    assert r2._portfolio_admits(1, "A", "p500", "bullish", "stock", 101.0)[1] == "breaker"


def test_early_close_blocks_entries_that_cannot_complete(tmp_path):
    clock = Clock(T(11, 16))
    late = {"BRK": {**BULL["BRK"], "path": [("10:00", 100.5), ("11:05", 101.3), ("11:10", 101.4)]}, "SPY": BULL["SPY"]}
    m = FakeMarket(make_bars(late), quotes={"BRK": {"price": 101.4, "bid": 101.3, "ask": 101.5, "price_src": "iex-realtime"}})
    st, r = runner(tmp_path, clock, m, llm=None, broker=FakeBroker(close="13:00"))
    r.universe = ["BRK"]
    res = r.tick()
    assert res["new_signals"] == 0 and r.counters.get("horizon_past_close") == 1   # 11:15 + 120 min + 30 min buffer > 13:00


def test_versions_are_immutable_cohorts(tmp_path):
    st = store(tmp_path)
    v1 = st.register(ExperimentConfig())
    again = st.register(ExperimentConfig(mode="shadow"))                 # mode is operational, not identity
    assert again["id"] == v1["id"]
    v2 = st.register(ExperimentConfig(relvol_min=2.0))
    assert v2["version"] == 2 and v2["config_hash"] != v1["config_hash"] and st.get(v1["id"])["config_hash"] == v1["config_hash"]
    assert st.set_status(v2["id"], "live") is False                      # live activation is not a code path here


def test_envelope_only_tightens():
    from munchkin.styles import effective_limits
    from munchkin.config import RiskLimits
    aggressive = effective_limits(RiskLimits(), "aggressive")
    t = tighten_limits(aggressive, ExperimentConfig())
    assert t.max_position_pct == 0.10 and t.max_positions == 1 and t.max_daily_loss_pct == 0.02 and t.allow_spreads is False and t.max_options_pct == 0.04
    defensive = effective_limits(RiskLimits(), "defensive")
    t2 = tighten_limits(defensive, ExperimentConfig())
    assert t2.allow_options is False and t2.max_position_pct == 0.10     # a stricter style stays stricter


def test_classifier_filter_rules():
    ok = {"event_type": "product", "direction": "bullish", "novelty": "new", "substantive": True, "source_quality": "major_outlet", "abstain": False}
    assert classifier_accepts(ok, "bullish") == (True, "accepted")
    assert classifier_accepts({**ok, "novelty": "recycled"}, "bullish")[1] == "classifier_rejected"
    assert classifier_accepts({**ok, "direction": "bearish"}, "bullish")[1] == "classifier_rejected"
    assert classifier_accepts({**ok, "source_quality": "social"}, "bullish")[1] == "classifier_rejected"
    assert classifier_accepts({**ok, "abstain": True}, "bullish")[1] == "classifier_abstained"


# ---------------------------------------------------------------- journal foundations
def test_cash_reservation_is_atomic_and_expires(tmp_path):
    from munchkin.journal import Journal
    j = Journal(tmp_path / "j.db")
    ok1, held = j.reserve("order-a", 300.0, available=400.0)
    ok2, held2 = j.reserve("order-b", 300.0, available=400.0)
    assert ok1 and not ok2 and held2 == 300.0
    j.release("order-a")
    assert j.reserve("order-b", 300.0, available=400.0)[0]
    ok3, _ = j.reserve("order-c", 50.0, available=400.0, ttl_s=-1)      # already expired
    assert ok3 and j.reserved_total() == 300.0


def test_legacy_database_is_migrated_in_place(tmp_path):
    from munchkin.journal import Journal, SCHEMA
    p = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(p))
    legacy = "\n".join(l for l in SCHEMA.splitlines() if "reservations" not in l)
    conn.executescript(legacy)
    conn.execute("INSERT INTO trades(symbol, opened_at, closed_at, qty, entry, exit, pnl, pnl_pct, is_option) VALUES ('UBER','2026-09-10T13:56','2026-09-11T10:00',1.377,72.61,71.5,-1.5,-1.5,0)")
    conn.commit(); conn.close()
    j = Journal(p)
    cols = {r[1] for r in j.conn.execute("PRAGMA table_info(trades)").fetchall()}
    assert {"strategy_id", "experiment_id", "execution_mode"} <= cols
    row = j.conn.execute("SELECT symbol, strategy_id FROM trades").fetchone()
    assert row["symbol"] == "UBER" and row["strategy_id"] is None          # legacy rows: unknown attribution, preserved


def test_contract_multiplier_comes_from_the_contract():
    from munchkin.risk import RiskEngine
    e = RiskEngine.__new__(RiskEngine); e.b = FakeBroker(); RiskEngine._mult_cache.clear()
    assert e.multiplier("ADJ260918C00010000") == 10.0 and e.multiplier("UBER260918C00072500") == 100.0
