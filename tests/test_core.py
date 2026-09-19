import pytest
"""Fast unit tests for the pure-python parts (no network). Run: .venv/bin/python -m pytest -q"""
import datetime as dt
import pathlib
import tempfile

from munchkin.journal import Journal
from munchkin.util import occ_human, parse_occ, is_option, business_days_back


def test_occ_parsing():
    p = parse_occ("SOFI260918C00018000")
    assert p == {"underlying": "SOFI", "expiration": dt.date(2026, 9, 18), "type": "call", "strike": 18.0}
    assert occ_human("SPY261016P00600000") == "SPY 2026-10-16 600P"
    assert is_option("AAPL") is False


def test_business_days_back():
    # 2026-09-09 is a Wednesday; 4 business days back is the previous Thursday
    assert business_days_back(4, dt.date(2026, 9, 9)) == dt.date(2026, 9, 3)


def _journal():
    return Journal(pathlib.Path(tempfile.mkdtemp()) / "t.db")


def test_round_trips_day_trades_and_settlement(monkeypatch):
    j = _journal()
    today = dt.datetime.now(dt.timezone.utc).date()
    y = today - dt.timedelta(days=1)
    fills = [
        {"id": "f1", "activity_type": "FILL", "transaction_time": f"{y}T14:00:00Z", "symbol": "SOFI", "side": "buy", "qty": "10", "price": "18.0"},
        {"id": "f2", "activity_type": "FILL", "transaction_time": f"{today}T14:30:00Z", "symbol": "SOFI", "side": "buy", "qty": "5", "price": "17.5"},
        {"id": "f3", "activity_type": "FILL", "transaction_time": f"{today}T15:00:00Z", "symbol": "SOFI", "side": "sell", "qty": "15", "price": "18.5"},
        {"id": "f4", "activity_type": "FILL", "transaction_time": f"{today}T15:10:00Z", "symbol": "SOFI260918C00018000", "side": "buy", "qty": "1", "price": "0.60"},
    ]
    j.upsert_fills(fills)
    new = j.rebuild_trades()
    assert len(new) == 1 and new[0]["pnl"] == 10.0
    assert j.rebuild_trades() == []  # idempotent
    t = j.trades()[0]
    assert t["qty"] == 15 and abs(t["entry"] - 17.8333) < 1e-3 and t["exit"] == 18.5
    assert j.day_trades(5) == [{"date": today.isoformat(), "symbol": "SOFI"}]
    assert j.bought_today("SOFI260918C00018000") is True
    assert j.unsettled_proceeds(1) == 277.5


def test_lessons_plan_playbook():
    j = _journal()
    j.add_lesson("Do not chase gaps", weight=2.0)
    j.add_lesson("Size by stop distance")
    assert j.lessons(5)[0]["text"] == "Do not chase gaps"
    j.set_plan("watch SOFI")
    assert j.get_plan() == "watch SOFI"
    assert "Playbook" in j.playbook()


def test_research_gate_and_grades():
    from munchkin.research import ResearchTracker, normalize_grade
    from munchkin.risk import RiskEngine
    from munchkin.config import RiskLimits
    t = ResearchTracker(min_charts=2, require_dossier=False)
    v = t.gate("INTC")
    assert any("get_market_context" in x for x in v) and any("broad scan" in x for x in v)
    t.note_context(); t.note_screen(); t.note_chart("intc"); t.note_chart("SIG")
    assert t.gate("INTC") == [f"INTC has not been grounded in news in the last 3h (get_news with symbols=[INTC] or web_search mentioning INTC)"]
    t.note_search("why is INTC up today")
    assert t.gate("INTC") == []
    assert t.gate("NVDA")  # not charted / not grounded
    assert normalize_grade("Rumor") == "speculative" and normalize_grade("bogus") is None
    r = RiskEngine(None, None, None, RiskLimits())
    assert r.grade_multiplier("confirmed") == 1.0 and r.grade_multiplier("speculative") == 0.5 and r.grade_multiplier("none") == 0.35


def test_exit_levels_never_lower_and_registry():
    from munchkin.exits import ExitManager
    j = _journal()
    X = ExitManager(None, None, j)
    X.register("INTC", 100.0, 112.0)
    ok, msg = X.update("INTC", stop_price=99.0)
    assert not ok and "LOWER" in msg
    ok, _ = X.update("INTC", stop_price=103.0, target_price=115.0)
    assert ok and j.exits("INTC")["stop_price"] == 103.0 and j.exits("INTC")["target_price"] == 115.0
    ok, _ = X.update("INTC", stop_price=95.0, allow_lower_stop=True)
    assert ok
    assert "INTC" in j.all_exits()
    j.clear_exits("INTC")
    assert j.exits("INTC") is None and "INTC" not in j.all_exits()


def test_watcher_renotify_window():
    from munchkin.watch import Watcher
    from munchkin.config import WatchSettings
    w = Watcher.__new__(Watcher)
    w.notified = {}
    w.cfg = WatchSettings()
    assert not w._recently("tgt:X", 30)
    w._mark("tgt:X")
    assert w._recently("tgt:X", 30)


def _synthetic_frame(n=400, seed=0):
    import numpy as np, pandas as pd
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="B", tz="UTC")
    ret = rng.normal(0.0005, 0.02, n)
    close = 100 * np.cumprod(1 + ret)
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    openp = close * (1 + rng.normal(0, 0.005, n))
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame({"open": openp, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def test_screen_frame_and_backtest():
    import pandas as pd
    from munchkin import analytics as A
    f = A.screen_frame(_synthetic_frame())
    for col in ("chg_20d_pct", "rsi14", "adx14", "bb_squeeze", "z20_atr", "vol_ratio", "hi20_dist_pct"):
        assert col in f.columns and f[col].notna().sum() > 100
    frames = {f"S{i}": A.screen_frame(_synthetic_frame(seed=i)) for i in range(4)}
    for fr in frames.values():
        fr["_low"] = fr["price"] * 0.99
    stats = A.backtest_setups(frames, {"any_up": ("chg_1d_pct > 0", "chg_1d_pct", False)}, set())
    s = stats["setups"]["any_up"]
    assert s["n"] > 50 and "fwd10_win" in s and "by_year" in s and "baseline_all_days" in stats


def test_cross_sectional_regime_and_sizing():
    import pandas as pd
    from munchkin import analytics as A
    rows = []
    for i, sym in enumerate(["SPY", "XLK", "AAA", "BBB", "CCC"]):
        rows.append({"symbol": sym, "chg_60d_pct": i * 5.0, "chg_20d_pct": i * 2.0, "chg_5d_pct": i, "chg_1d_pct": 0.5 - i * 0.3,
                     "vol_ratio": 1 + i, "rvol20_pct": 20 + i, "sma20_dist_pct": i, "atr14_pct": 2.0, "sma50_dist_pct": 1.0 if i != 3 else -1,
                     "sma200_dist_pct": 2.0, "rsi14": 55.0, "hi20_dist_pct": -1.0, "lo20_dist_pct": 5.0, "is_etf": sym in ("SPY", "XLK")})
    t = pd.DataFrame(rows).set_index("symbol", drop=False)
    sectors = {"AAA": "Information Technology", "BBB": "Information Technology", "CCC": "Energy"}
    t = A.add_cross_sectional(t, t.loc["SPY"].to_dict(), sectors, t[t["is_etf"]])
    assert t.loc["CCC", "mom_3m_rank"] == 100 and t.loc["AAA", "rs_sector_20d"] == 2.0
    reg = A.regime(t, {"vix": 14.0, "vix3m": 16.0})
    assert reg["label"] in ("risk-on", "neutral", "risk-off") and reg["components"]["vol_regime"] == "low"
    sz = A.position_size(500, 0.05, 100.0, 95.0, 3.0, 200.0, 300.0)
    assert sz["loss_at_stop"] <= 25.01 and sz["binding"] == "position_cap" and sz["stop_in_atr"] < 2
    assert A.session_fraction(__import__("datetime").datetime(2026, 9, 10, 9, 0), [0.1] * 78) is None


def test_expiry_plan():
    import datetime as dt
    from munchkin.exits import expiry_plan
    from munchkin.util import ET
    exp_today = dt.date(2026, 9, 18)
    long_today = {"symbol": "INTC260918C00105000", "qty": "1"}
    long_tomorrow = {"symbol": "INTC260919C00105000", "qty": "1"}   # hypothetical
    far = {"symbol": "INTC261016C00110000", "qty": "1"}
    short_today = {"symbol": "INTC260918C00110000", "qty": "-1"}
    noon = dt.datetime(2026, 9, 18, 12, 0, tzinfo=ET)
    p = expiry_plan([long_today, far], noon)
    assert [a["action"] for a in p] == ["wake"]
    p = expiry_plan([long_today], dt.datetime(2026, 9, 18, 14, 31, tzinfo=ET))
    assert [a["action"] for a in p] == ["force_close"]
    p = expiry_plan([long_today], dt.datetime(2026, 9, 18, 15, 50, tzinfo=ET))
    assert [a["action"] for a in p] == ["force_close", "dne"]
    # a spread on the same expiry: no DNE on the long leg (the short could be assigned)
    p = expiry_plan([long_today, short_today], dt.datetime(2026, 9, 18, 15, 50, tzinfo=ET), deltas={"INTC260918C00110000": -0.9})
    acts = [a["action"] for a in p]
    assert "dne" not in acts and acts.count("force_close") == 2 and "assignment_risk" in acts
    # day before expiry -> wake
    p = expiry_plan([long_tomorrow], dt.datetime(2026, 9, 18, 10, 0, tzinfo=ET))
    assert p and p[0]["action"] == "wake" and p[0]["dte"] == 1


def test_research_gate_requires_dossier_and_macro_format():
    from munchkin.research import ResearchTracker
    from munchkin import macro as M
    t = ResearchTracker(min_charts=2)
    t.note_context(); t.note_screen(); t.note_chart("AAA"); t.note_chart("BBB"); t.note_news(["AAA"])
    v = t.gate("AAA")
    assert len(v) == 1 and "research_symbol" in v[0]
    t.note_dossier("aaa")
    assert t.gate("AAA") == []
    txt = M.format_macro_data({"source": "BLS", "fetched": "x", "ppi": {"label": "PPI", "series": "WPSFD4", "latest_period": "August 2026", "value": 157.4, "prev_value": 156.8, "mom_pct": 0.4, "yoy_pct": 3.1}})
    assert "ppi" in txt and "m/m +0.40%" in txt and "y/y +3.10%" in txt


def test_task_queue():
    j = _journal()
    a = j.add_task("research XYZ", priority=3)
    b = j.add_task("urgent: verify brief", priority=1, kind="task")
    assert j.next_task()["id"] == b
    assert j.complete_task(b, "verified") and j.next_task()["id"] == a
    assert not j.complete_task(b, "again")
    assert j.open_tasks()[0]["text"] == "research XYZ"


def test_entry_book_and_triggers():
    import datetime as dt
    from munchkin.entries import EntryBook, trigger_hit, expired
    j = _journal()
    eb = EntryBook(j)
    rec = eb.arm("CDW", "above", 145.58, 80.0, 139.0, 158.0, "breakout over 145.58 (Barclays PT 150, 2026-09-08)", "confirmed", "3-7d", 30.0, None)
    assert trigger_hit(rec, 145.60) and not trigger_hit(rec, 145.50)
    rec2 = eb.arm("UBER", "below", 72.0, 60.0, 70.0, 76.0, "pullback buy", "speculative", "5d", None, None)
    assert trigger_hit(rec2, 71.9) and not trigger_hit(rec2, 72.5)
    assert not expired(rec2, dt.datetime.now(dt.timezone.utc))
    assert expired({"expires": "2020-01-01T00:00:00"}, dt.datetime.now())
    assert set(eb.all()) == {"CDW", "UBER"} and "CDW" in eb.describe()
    assert eb.disarm("CDW") and not eb.disarm("CDW") and set(eb.all()) == {"UBER"}


def test_entry_time_and_tape_conditions():
    import datetime as dt
    from munchkin.entries import EntryBook, not_yet, index_ok
    from munchkin.util import ET
    j = _journal()
    rec = EntryBook(j).arm("UBER", "above", 73.5, 80, 70.5, 79, "Spain L4 permit (Benzinga 09-10)", "confirmed", "5d", 48, None,
                           not_before="2026-09-11T08:35:00-04:00", spy_min_chg_pct=-1.0)
    assert not_yet(rec, dt.datetime(2026, 9, 11, 8, 0, tzinfo=ET)) and not not_yet(rec, dt.datetime(2026, 9, 11, 9, 0, tzinfo=ET))
    assert index_ok(rec, -0.5) and not index_ok(rec, -1.5) and not index_ok(rec, None)
    assert index_ok({"spy_min_chg_pct": None}, None)


def test_option_resolution_and_limits():
    from munchkin.optentry import resolve_contracts, limit_for, tick_round, SpreadBook
    rows = [
        {"symbol": "UBER261002C00072000", "strike": 72.0, "exp": "2026-10-02", "dte": 22, "bid": 2.82, "ask": 3.07, "delta": 0.553},
        {"symbol": "UBER261002C00074000", "strike": 74.0, "exp": "2026-10-02", "dte": 22, "bid": 2.00, "ask": 2.07, "delta": 0.435},
        {"symbol": "UBER261002C00078000", "strike": 78.0, "exp": "2026-10-02", "dte": 22, "bid": 0.80, "ask": 0.91, "delta": 0.231},
        {"symbol": "UBER261002C00080000", "strike": 80.0, "exp": "2026-10-02", "dte": 22, "bid": 0.52, "ask": 0.58, "delta": 0.161},
        {"symbol": "UBER261009C00073000", "strike": 73.0, "exp": "2026-10-09", "dte": 29, "bid": 2.73, "ask": 3.16, "delta": 0.503},
    ]
    legs, note = resolve_contracts(rows, "call", 14, 4)
    assert len(legs) == 1 and legs[0][0]["strike"] == 72.0  # nearest expiry to 14 DTE, delta ~0.5
    legs, note = resolve_contracts(rows, "call_spread", 14, 4)
    assert [(l[0]["strike"], l[1]) for l in legs] == [(72.0, "buy"), (78.0, "sell")]
    lim = limit_for(legs)
    assert 1.9 < lim < 2.3  # net debit around mid (2.945 - 0.855) plus a quarter of the spreads
    assert tick_round(2.987) == 2.98 and tick_round(3.02) == 3.0 and tick_round(3.07) == 3.05
    j = _journal()
    sb = SpreadBook(j)
    sb.register("UBER261002C00072000", "UBER261002C00078000", 1, 2.1, 1.05, 4.2, None)
    assert "UBER261002C00072000" in sb.all() and sb.update("UBER261002C00072000", stop=1.3)
    assert sb.all()["UBER261002C00072000"]["stop"] == 1.3
    sb.clear("UBER261002C00072000"); assert sb.all() == {}


def test_no_undefined_names_in_package():
    """A NameError in a rarely-taken branch crashed the daemon once; pyflakes catches it statically."""
    import subprocess, sys, pathlib
    pkg = pathlib.Path(__file__).resolve().parent.parent / "munchkin"
    out = subprocess.run([sys.executable, "-m", "pyflakes", str(pkg)], capture_output=True, text=True).stdout
    bad = [l for l in out.splitlines() if "undefined name" in l or "used before assignment" in l or "invalid syntax" in l]
    assert not bad, "\n".join(bad)


def test_provider_config_and_keys(tmp_path, monkeypatch):
    from munchkin import providers as P
    monkeypatch.setattr(P, "SEARCH_CONFIG_FILE", tmp_path / "search.json")
    monkeypatch.setattr(P, "ENV_FILE", tmp_path / ".env")
    P._cfg_cache = None
    (tmp_path / ".env").write_text("ALPACA_API_KEY=abc\n")
    assert P.get_key("tavily") == "" and not P.enabled("tavily")
    P.set_key("tavily", "tvly-1234567890abcdef")
    assert P.get_key("tavily") == "tvly-1234567890abcdef" and P.masked_key("tavily").endswith("cdef") and "tvly-1234" not in P.masked_key("tavily")
    assert "ALPACA_API_KEY=abc" in (tmp_path / ".env").read_text()  # other lines preserved
    assert P.enabled("tavily")
    cfg = P.load_config(); cfg["enabled"]["tavily"] = False; P.save_config(cfg)
    assert not P.enabled("tavily")
    P.set_key("tavily", None)
    assert P.get_key("tavily") == "" and "TAVILY" not in (tmp_path / ".env").read_text()
    from munchkin.search import _dedupe
    rows = [{"url": "https://a/x"}, {"url": "https://a/x/"}, {"url": "https://b"}, {"url": ""}]
    assert [r["url"] for r in _dedupe(rows, 5)] == ["https://a/x", "https://b"]


def test_world_dials_scores_are_bounded_and_signed():
    from munchkin.macro import world_dials
    tape = {"spy": {"chg_20d_pct": 3.0}, "hyg": {"chg_20d_pct": 1.0}, "lqd": {"chg_20d_pct": -1.0}, "iwm": {"chg_20d_pct": 9.0},
            "us10y_yield": {"last": 4.1, "chg_20d_bp": 40.0}, "us3m_yield": {"last": 4.3}, "dollar_index": {"last": 98.0, "chg_20d_pct": -30.0},
            "vix": {"last": 14.0}, "vix3m": {"last": 16.0}}
    d = {x["key"]: x for x in world_dials(tape, {"breadth": {"pct_above_sma50": 70}}, None)}
    assert d["breadth"]["score"] > 0 and d["credit"]["score"] > 0 and d["size"]["score"] == 1.0
    assert d["rates"]["score"] < 0 and "10y−3m" in d["rates"]["value"]
    assert d["dollar"]["score"] == 1.0            # clamped
    assert d["vol"]["score"] > 0 and "term 0.88" in d["vol"]["value"]
    assert all(-1 <= x["score"] <= 1 for x in d.values())


def test_knowledge_base_roundtrip(tmp_path):
    from munchkin.knowledge import KnowledgeBase, CURRICULUM
    kb = KnowledgeBase(tmp_path)
    assert kb.index() == [] and kb.next_topics(2)[0]["slug"] == CURRICULUM[0][0]
    kb.save("Credit Spreads!", "Credit spreads", "HY minus IG.\nMore text here.", ["https://example.org/a", ""], summary=None)
    idx = kb.index()
    assert idx[0]["slug"] == "credit-spreads" and idx[0]["summary"] == "HY minus IG." and idx[0]["sources"] == 1
    got = kb.get("credit spreads")
    assert got and got["title"] == "Credit spreads" and got["body"].startswith("HY minus IG.")
    assert kb.next_topics(30)[-1]["slug"] == "credit-spreads"   # studied topics rotate to the back
    assert "credit-spreads" in kb.index_text()


def test_context_plan_scales_with_window(monkeypatch):
    from munchkin import llm
    from munchkin.config import LLMSettings
    s = LLMSettings(context_tokens=65536, max_tokens=8192, context_char_budget=190_000, tool_result_max_chars=5000)
    p = llm.context_plan(s)
    assert p["effective_chars"] == 189_376 and p["tool_result_chars"] == 4983 and p["warning"] is None
    small = LLMSettings(context_tokens=16384, max_tokens=4096, context_char_budget=190_000, tool_result_max_chars=5000)
    q = llm.context_plan(small)
    assert q["effective_chars"] == 9152 + 0 or q["effective_chars"] == 12_000
    assert q["tool_result_chars"] < 1000 and "below the recommended" in q["warning"]
    monkeypatch.setattr(llm, "detect_context_tokens", lambda s: (None, "server does not report a context size"))
    u = llm.context_plan(LLMSettings(context_tokens=0))
    assert u["effective_chars"] == u["configured_chars"] and "unknown" in u["warning"]


def _write_skill(root, name, body="x" * 400, desc="A test skill.", extra=""):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n{extra}---\n{body}\n")
    return d


def test_skill_store_loads_validates_and_gates(tmp_path):
    from munchkin.skills import SkillStore, MAX_BODY
    repo, agent, state = tmp_path / "skills", tmp_path / "data" / "skills", tmp_path / "data" / "skills.json"
    d = _write_skill(repo, "print-day")
    (d / "scripts").mkdir(); (d / "scripts" / "event_study.py").write_text("print(ARGS)\n")
    (d / "resources").mkdir(); (d / "resources" / "dates.txt").write_text("2026-09-11 CPI\n")
    _write_skill(repo, "too-big", body="y" * (MAX_BODY + 1))
    _write_skill(repo, "Bad_Name")
    st = SkillStore(repo, agent, state)
    names = [s.name for s in st.all()]
    assert names == ["print-day"] and len(st.errors) == 2
    assert "print-day: A test skill. [scripts: event_study.py]" in st.index_text()
    code, err = st.script_source("print-day", "event_study.py"); assert code.startswith("print(") and err == ""
    assert st.script_source("print-day", "../SKILL.md")[0] is None
    assert st.resource_text("print-day", "dates.txt").startswith("2026-09-11")
    assert "no resource" in st.resource_text("print-day", "../../SKILL.md")
    # agent draft: not active until approved; disable/enable; editing an approved skill returns it to draft
    assert st.save_draft("bad name", "d", "z" * 400).startswith("REJECTED")
    assert st.save_draft("print-day", "d", "z" * 400).startswith("REJECTED")      # cannot shadow an operator skill
    assert st.save_draft("earnings-runner", "Runs into earnings.", "z" * 400).startswith("created draft")
    s = st.get("earnings-runner"); assert s.source == "agent" and s.status == "draft"
    assert "earnings-runner" not in st.index_text()
    assert st.script_source("earnings-runner", "x.py")[0] is None
    assert st.approve("earnings-runner") and st.get("earnings-runner").status == "active" and "earnings-runner" in st.index_text()
    assert st.set_enabled("earnings-runner", False) and not st.get("earnings-runner").enabled and "earnings-runner" not in st.index_text()
    assert st.save_draft("earnings-runner", "Runs into earnings.", "w" * 400).startswith("updated draft") and st.get("earnings-runner").status == "draft"
    assert st.delete_draft("earnings-runner") and st.get("earnings-runner") is None
    assert not st.approve("print-day")     # operator skills are not "approved", they are active by construction


def test_bundled_skills_are_valid_and_scripts_pass_the_sandbox_allowlist():
    from munchkin import sandbox as SB
    from munchkin.skills import SkillStore
    st = SkillStore()
    skills = {s.name: s for s in st.all()}
    assert st.errors == [] and {"print-day", "option-expression", "armed-entries", "expiry-and-assignment", "opening-range", "post-trade-review"} <= set(skills)
    for s in skills.values():
        for sc in s.scripts:
            code, err = st.script_source(s.name, sc)
            assert err == "" and SB.validate(code) is None, f"{s.name}/{sc}: {SB.validate(code)}"


def test_skill_script_runs_in_the_sandbox():
    from munchkin import sandbox as SB
    from munchkin.skills import SkillStore
    code, _ = SkillStore().script_source("option-expression", "vertical_math.py")
    out = SB.run(code, {"bars": {}, "screener": None, "chains": {}, "fills": None, "trades": None, "equity": None,
                        "ARGS": {"type": "C", "long_strike": 72, "short_strike": 74, "long_mid": 1.99, "short_mid": 1.06, "spot": 72.99, "expected_move_pct": 8.4}})
    assert "debit 0.93 = $93.0 risk, max gain $107.0" in out and "breakeven 72.93" in out
    code, _ = SkillStore().script_source("post-trade-review", "trade_stats.py")
    out = SB.run(code, {"bars": {}, "screener": None, "chains": {}, "fills": None, "trades": None, "equity": None, "ARGS": {}})
    assert "no closed trades yet" in out


def test_research_gate_spans_recent_sessions():
    import datetime as dt
    from munchkin.research import ResearchTracker

    class FakeJournal:
        def __init__(self): self.kv = {}
        def get(self, k, default=None): return self.kv.get(k, default)
        def set(self, k, v): self.kv[k] = v

    j = FakeJournal()
    a = ResearchTracker(min_charts=2).attach(j, 3.0)
    a.note_context(); a.note_screen(); a.note_chart("UBER"); a.note_chart("GD"); a.note_news(["UBER"]); a.note_dossier("UBER")
    assert a.gate("UBER") == []
    b = ResearchTracker(min_charts=2).attach(j, 3.0)          # a new session a minute later sees the same evidence
    assert b.gate("UBER") == [] and "GD" in b.charted and b.screened and b.context_checked
    old = (dt.datetime.now() - dt.timedelta(hours=4)).isoformat(timespec="seconds")
    for kind in ("charted", "newsed", "dossiers", "flags"):
        j.kv["research:state"][kind] = {k: old for k in j.kv["research:state"][kind]}
    c = ResearchTracker(min_charts=2).attach(j, 3.0)          # four hours later it has all expired
    assert len(c.gate("UBER")) == 6 and "in the last 3h" in c.gate("UBER")[0]
    d = ResearchTracker(min_charts=2)                          # no journal: per-session behaviour, no crash
    d.note_chart("X"); assert "X" in d.charted


def test_broker_clock_falls_back_when_alpaca_errors():
    from munchkin.broker import Broker

    class Boom:
        def get_clock(self): raise RuntimeError("Internal Server Error")
    b = Broker.__new__(Broker); b.tc = Boom()
    b.is_trading_day = lambda d: True
    c = b.clock()
    assert c["degraded"] is True and isinstance(c["is_open"], bool) and "Internal Server Error" in c["error"]


def test_feed_breaker_fast_fails_after_repeated_timeouts():
    from munchkin.market import FeedBreaker, FeedDegraded, GuardedClient

    class Flaky:
        calls = 0
        def get(self, x):
            Flaky.calls += 1
            raise RuntimeError('{"message":"backend request timeout"}')
    b = FeedBreaker(threshold=2, cooldown_s=60)
    g = GuardedClient(Flaky(), b)
    for _ in range(2):
        try: g.get(1)
        except RuntimeError: pass
    assert b.open and Flaky.calls == 2
    try:
        g.get(1); assert False, "should fast-fail"
    except FeedDegraded as e:
        assert "yfinance" in str(e)
    assert Flaky.calls == 2            # the third call never reached the client
    b.until = 0                         # cooldown over
    class Fine:
        def get(self, x): return "ok"
    assert GuardedClient(Fine(), b).get(1) == "ok" and b.fails == 0


def test_telegram_bot_pairing_commands_and_requests(tmp_path, monkeypatch):
    from munchkin import telegram as TG
    monkeypatch.setattr(TG, "STATE_FILE", tmp_path / "telegram.json")
    monkeypatch.setattr(TG, "token", lambda: "123:abc")
    sent = []
    monkeypatch.setattr(TG, "send", lambda chat, text: sent.append((chat, text)) or True)

    class J:
        def __init__(self): self.kv, self.tasks, self.events = {}, [], []
        def get(self, k, d=None): return self.kv.get(k, d)
        def set(self, k, v): self.kv[k] = v
        def add_event(self, kind, text): self.events.append((kind, text))
        def add_task(self, text, priority=2, session_id=None, kind="task"): self.tasks.append((text, kind)); return len(self.tasks)
        def open_tasks(self, n=10): return [{"id": i + 1, "kind": k, "priority": 0, "text": t} for i, (t, k) in enumerate(self.tasks)]
        def sessions(self, n=1): return [{"id": 7, "phase": "intraday", "started_at": "2026-09-11T10:00:00", "ended_at": "2026-09-11T10:05:00", "summary": "held UBER", "actions": "held UBER"}]
    class B:
        def positions(self): return [{"symbol": "UBER", "qty": "1.377", "avg_entry_price": "72.61", "current_price": "71.5", "unrealized_pl": "-1.5", "unrealized_plpc": "-0.02"}]
    class R:
        class S: virtual_equity, daily_pnl, buying_power_now, market_open = 498.7, -5.1, 400.0, True
        def state(self): return R.S()
    import munchkin.entries as E
    class FakeBook:
        def __init__(self, j): pass
        def all(self): return {}
        def disarm(self, s): pass
    monkeypatch.setattr(E, "EntryBook", FakeBook)
    j = J(); bot = TG.Bot(j, B(), R())
    assert "not paired" in bot.handle_text(42, "/status")
    assert "No valid pairing code" in bot.handle_text(42, "/pair NOPE")
    code = TG.new_pair_code()
    assert bot.handle_text(42, f"/pair {code.lower()}").startswith("Paired") and TG.paired() == [42]
    assert "Equity $498.70" in bot.handle_text(42, "/status") and "UBER" in bot.handle_text(42, "/positions")
    assert "Confirm" in bot.handle_text(42, "/halt")
    r = bot.handle_text(42, "/halt yes"); assert r.startswith("Halted") and TG.HALT_FILE.exists()
    assert bot.handle_text(42, "/resume") and bot.handle_text(42, "/resume yes").startswith("Resumed") and not TG.HALT_FILE.exists()
    assert bot.handle_text(42, "what is the plan for UBER?").startswith("Queued as request #1") and j.tasks[0][1] == "operator" and j.kv["telegram:task:1"] == 42
    assert "✓ fills" in bot.handle_text(42, "/notify") and "✗ fills" in bot.handle_text(42, "/notify fills off")
    # notify honours toggles, mute and rate limits
    assert TG.notify("x", kind="fills") is False              # toggled off above
    assert TG.notify("x", kind="errors") is True and TG.notify("y", kind="errors") is False   # rate limited
    assert TG.notify("z", kind="errors", force=True) is True
    TG.mute(5); assert TG.notify("m", kind="sessions") is False and TG.notify("m", kind="replies", chat_id=42, force=True) is True
    assert bot.handle_text(99, "/status").startswith("This chat is not paired")


def test_clamp_to_cap_and_arm_outcome():
    import pandas as pd
    from munchkin.entries import arm_outcome, arm_outcome_text, clamp_to_cap
    v = ["exposure to HPQ would be $100.00 (stock + options combined) > cap $99.98 (catalyst grade 'speculative' scales the cap by 0.50)"]
    n, note = clamp_to_cap(100.0, v)
    assert n == 99.93 and "trimmed" in note
    assert clamp_to_cap(100.0, v + ["market closed"]) == (100.0, None)          # only trims when the cap is the sole problem
    assert clamp_to_cap(100.0, ["exposure to X would be $100.00 > cap $10.00"]) == (100.0, None)   # below the floor: leave it to the engine

    class M:
        def bars(self, sym, tf, n):
            idx = pd.date_range("2026-09-11 09:30", periods=6, freq="5min", tz="America/New_York")
            return pd.DataFrame({"open": [200] * 6, "high": [201, 201, 200.5, 200, 199.8, 199.9], "low": [199.5, 199, 197.2, 196.3, 197, 198], "close": [200, 199.5, 198, 197, 198, 199], "volume": [1] * 6}, index=idx)
    rec = {"symbol": "RTX", "direction": "below", "trigger_price": 196.5, "armed_at": "2026-09-11T09:30:00-04:00"}
    o = arm_outcome(M(), rec)
    assert o["touched"] is True and o["extreme"] == 196.3 and o["price_at_arm"] == 200 and o["drift_pct"] == -0.5
    assert "touched" in arm_outcome_text(o)
    rec["trigger_price"] = 195.0
    o2 = arm_outcome(M(), rec)
    assert o2["touched"] is False and o2["closest_pct"] > 0 and "never reached" in arm_outcome_text(o2)


def test_watcher_takes_stock_target_mechanically():
    from munchkin.config import WatchSettings
    from munchkin.watch import Watcher

    class J:
        def __init__(self): self.kv, self.decisions, self.marks = {"exits:UBER": {"stop_price": 70.5, "target_price": 79.0}}, [], {}
        def exits(self, sym): return self.kv.get("exits:" + sym)
        def set(self, k, v): self.kv[k] = v
        def get(self, k, d=None): return self.kv.get(k, d)
        def add_decision(self, *a, **k): self.decisions.append((a, k))
        def set_exits(self, *a, **k): pass
    class B:
        def __init__(self): self.orders = []
        def submit_stock_order(self, sym, side, qty=None, notional=None, order_type="market", limit_price=None):
            self.orders.append((sym, side, qty, order_type)); return {"id": "o1", "status": "filled", "filled_avg_price": 79.2}
    class X:
        def __init__(self): self.cancelled = []
        def cancel_exit_orders(self, sym, *a, **k): self.cancelled.append(sym)
        def update(self, *a, **k): return True, ""
        def place_stop(self, *a, **k): return {}
    class M:
        pass
    j, b, x = J(), B(), X()
    w = Watcher.__new__(Watcher); w.b, w.m, w.j, w.x = b, M(), j, x; w.cfg = WatchSettings(target_mode="take", target_take_pct=100); w._market_open = True
    w._recently = lambda key, minutes: False; w._mark = lambda key: j.marks.__setitem__(key, 1)
    actions, events = [], []
    w._take_stock_target("UBER", {"symbol": "UBER", "qty": "1.377", "qty_available": "1.377", "avg_entry_price": "72.61"}, 79.2, 79.0, actions, events)
    assert b.orders == [("UBER", "sell", 1.377, "market")] and x.cancelled == ["UBER"] and j.kv["exits:UBER"] is None
    assert actions and actions[0].startswith("TARGET TAKEN: UBER sold 1.377") and events and "review the thesis" in events[0]
    assert j.decisions[0][0][1] == "close" and j.decisions[0][1]["meta"]["mechanical"] is True


def _fake_market(seed=1):
    import numpy as np, pandas as pd
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-02", periods=520, tz="America/New_York")

    class M:
        def bars(self, sym, tf, limit=780):
            syms = sym if isinstance(sym, list) else [sym]
            frames = []
            for s in syms:
                r = rng.normal(0.0004, 0.012, len(idx)) + (0.002 if s == "UP" else 0.0)
                close = 100 * np.cumprod(1 + r)
                df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6, "vwap": close, "trade_count": 100}, index=idx[-limit:] if limit < len(idx) else idx)
                df = df.iloc[-limit:]
                if isinstance(sym, list):
                    df["symbol"] = s; frames.append(df.set_index("symbol", append=True).swaplevel())
                else:
                    return df
            return pd.concat(frames)
    return M()


def test_lab_ledger_spec_and_gates(tmp_path):
    import sqlite3
    from munchkin.lab import Lab, validate_spec, judge, parse_custom_result
    from munchkin.config import LabSettings

    class J:
        def __init__(self):
            self.conn = sqlite3.connect(str(tmp_path / "lab.db"), check_same_thread=False); self.conn.row_factory = sqlite3.Row
    lab = Lab(J())
    r = lab.propose("Policy threat dips revert", "After a policy threat headline pushes SPY down more than 1.5%, SPY is higher five sessions later more often than after other 1.5% down days.", "operator", "test")
    assert r["status"] == "proposed" and r["id"] == 1
    dup = lab.propose("Threat dips revert", "When a policy threat headline pushes SPY down more than 1.5%, SPY is higher five sessions later more often than after other down days.", "agent")
    assert "duplicate_of" in dup
    bad = validate_spec({"trigger": {"kind": "declarative", "conditions": {"nope": {"lte": 1}}}, "universe": "SPY", "side": "short", "holding": {"sessions": 0}, "expected": {}})
    assert len(bad) >= 4
    spec = {"trigger": {"kind": "declarative", "conditions": {"spy_chg_pct": {"lte": -1.5}}}, "universe": ["SPY"], "side": "long", "holding": {"sessions": 5}, "expected": {"horizon": "+5d", "effect_pct": 1.0}}
    assert validate_spec(spec) == [] and lab.specify(1, spec)["status"] == "specified"
    L = LabSettings()
    ok = {"n": 25, "mean": 1.8, "hit": 70, "worst": -3.0, "control": {"n": 40, "mean": 0.9, "hit": 55}, "halves": [{"mean": 1.5}, {"mean": 2.1}]}
    assert judge(ok, "event_study", spec, L)[0] == "pass"
    assert judge({**ok, "n": 5}, "event_study", spec, L)[0] == "inconclusive"          # small sample: not evidence against
    assert judge({**ok, "mean": 0.2, "hit": 40}, "event_study", spec, L)[0] == "fail"    # worse than the control on both counts
    assert judge({**ok, "mean": 1.0}, "event_study", spec, L)[0] == "inconclusive"
    assert judge({**ok, "halves": [{"mean": 3.0}, {"mean": -0.5}]}, "event_study", spec, L)[0] == "inconclusive"
    assert judge({**ok, "control": {}}, "event_study", spec, L)[0] == "inconclusive"
    lab.record_test(1, "event_study", {}, ok, "pass", ["ok"])
    assert lab.get(1)["status"] == "tested"
    lab.record_test(1, "event_study", {}, {**ok, "mean": 1.0}, "inconclusive", ["x"])   # a later inconclusive does not demote a tested claim
    assert lab.get(1)["status"] == "tested" and "verdict pass" in lab.describe(1)
    assert lab.set_status(1, "rejected", "operator") and lab.get(1)["status"] == "rejected"
    assert parse_custom_result("junk\nRESULT: {\"n\": 30, \"mean\": 1.2, \"hit\": 60, \"worst\": -4, \"control\": {\"n\": 100, \"mean\": 0.3, \"hit\": 52}}")["kind"] == "custom"
    assert "error" in parse_custom_result("RESULT: {\"n\": 3}")


def test_lab_templates_run_with_controls(tmp_path):
    import sqlite3
    from munchkin.lab import Lab, run_test
    m = _fake_market()

    class J:
        def __init__(self):
            self.conn = sqlite3.connect(str(tmp_path / "lab2.db"), check_same_thread=False); self.conn.row_factory = sqlite3.Row
    lab = Lab(J())
    hid = lab.propose("Big down days in UP revert", "When SPY falls 1.5% or more in a day, the name UP is higher five sessions later than the average down day suggests.", "agent")["id"]
    lab.specify(hid, {"trigger": {"kind": "declarative", "conditions": {"spy_chg_pct": {"lte": -1.5}}}, "universe": ["UP"], "side": "long", "holding": {"sessions": 5}, "expected": {"horizon": "+5d", "effect_pct": 1.0}})
    rec = run_test(lab, m, hid, "event_study", {"control_chg_lte": -1.0})
    r = rec["result"]
    assert r["n"] > 0 and "control" in r and r["control"]["n"] > 0 and "halves" in r and rec["verdict"] in ("pass", "fail", "inconclusive")
    hid2 = lab.propose("Oversold screen", "Names with rsi14 under 30 that are still above the 200-day average outperform over ten sessions versus all days.", "agent")["id"]
    lab.specify(hid2, {"trigger": {"kind": "screen", "expr": "rsi14 < 35"}, "universe": ["UP", "DN"], "side": "long", "holding": {"sessions": 10}, "expected": {"horizon": "+10d", "effect_pct": 1.0}})
    rec2 = run_test(lab, m, hid2, "screen_backtest", {"symbols": ["UP", "DN"], "years": 2})
    r2 = rec2["result"]
    assert r2.get("kind") == "screen_backtest" and ("n" in r2) and (r2.get("n", 0) == 0 or r2["control"]["n"] > 0)
    assert len(lab.get(hid2)["tests"]) == 1


def test_search_free_first_cache_and_pacing(tmp_path, monkeypatch):
    from munchkin import providers as P, search as S
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Uber wins Spain permit - Reuters</title><link>https://r.example/1</link>
    <pubDate>Thu, 11 Sep 2026 14:00:00 GMT</pubDate><description>&lt;a href="x"&gt;Uber wins&lt;/a&gt; the first national permit</description><source url="https://reuters.com">Reuters</source></item>
    <item><title>Second</title><link>https://r.example/2</link><pubDate>bad</pubDate><description></description></item></channel></rss>"""
    class R:
        status_code = 200; text = rss
    counts = {}
    monkeypatch.setattr(P.httpx, "get", lambda *a, **k: R())
    monkeypatch.setattr(P, "_count", lambda p: counts.__setitem__(p, counts.get(p, 0) + 1))
    rows = P.googlenews_search("UBER stock", 5, "week")
    assert rows[0]["title"].startswith("Uber wins") and rows[0]["date"] == "2026-09-11T14:00" and rows[0]["engine"] == "google-news/Reuters" and rows[1]["date"] is None
    # chain: google news answers a news query, tavily is never called; the second call is a cache hit
    monkeypatch.setattr(S, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(P, "load_config", lambda: {"order": ["googlenews", "searxng", "brave", "tavily"], "enabled": {"googlenews": True, "searxng": False, "tavily": True, "brave": False}, "budgets": {"tavily": 900}, "merge_news": True})
    monkeypatch.setattr(P, "enabled", lambda p: p == "googlenews" or p == "tavily")
    monkeypatch.setattr(P, "usage", lambda p: 0)
    monkeypatch.setattr(P, "usage_today", lambda p: 0)
    tav = []
    monkeypatch.setattr(P, "tavily_search", lambda *a, **k: tav.append(1) or [])
    out1 = S.web_search("UBER stock", "news", 5, "week")
    calls_after_first = counts.get("googlenews", 0)
    out2 = S.web_search("uber stock", "news", 5, "week")
    assert out1 and out2 == out1 and counts.get("googlenews") == calls_after_first and tav == []
    # pacing: a monthly budget is spread over the remaining days
    monkeypatch.setattr(P, "usage", lambda p: 900)
    monkeypatch.setattr(P, "usage_today", lambda p: 0)
    assert P.within_budget("tavily") is False
    monkeypatch.setattr(P, "usage", lambda p: 400)
    allowance = P.daily_allowance("tavily"); assert allowance is not None and 3 <= allowance < 900
    monkeypatch.setattr(P, "usage_today", lambda p: allowance)
    assert P.within_budget("tavily") is False
    monkeypatch.setattr(P, "usage_today", lambda p: 0)
    assert P.within_budget("tavily") is True


def test_uncompleted_tasks_are_deferred_then_abandoned(tmp_path):
    from munchkin.journal import Journal
    j = Journal(tmp_path / "t.db")
    tid = j.add_task("do a thing", 2, None, "research")
    assert j.next_task()["id"] == tid
    assert j.touch_task(tid, 120).startswith("deferred")
    assert j.next_task() is None                                 # deferred tasks are not dispatched again immediately
    assert j.open_tasks(5)[0]["id"] == tid                       # but still visible as open
    assert j.touch_task(tid, 120).startswith("deferred") and j.touch_task(tid, 120) == "abandoned"
    assert j.open_tasks(5) == [] and j.touch_task(tid, 120) == "closed"


def test_first_entry_is_capped_at_the_probe_maximum():
    from munchkin.risk import RiskEngine, RiskState
    from munchkin.styles import effective_limits
    from munchkin.config import RiskLimits
    e = RiskEngine.__new__(RiskEngine); e.L = effective_limits(RiskLimits(), "aggressive"); e.j = type("J", (), {"reserved_total": lambda self: 0.0})()
    st = RiskState.__new__(RiskState); st.max_position_notional = 250.0; st.halted = False; st.daily_loss_breached = False; st.positions_count = 1; st.virtual_settled_cash = 400.0
    v = e._entry_common(st, 295.0, "PNW", [])
    assert any("probe maximum $200" in x for x in v)                    # a $295 first entry is not a probe
    assert not any("probe" in x for x in e._entry_common(st, 140.0, "PNW", []))
    held = [{"symbol": "PNW", "cost_basis": "140.0"}]
    assert not any("probe" in x for x in e._entry_common(st, 100.0, "PNW", held))   # adds are governed by the cap, not the probe


def test_lab_error_verdict_does_not_move_a_claim(tmp_path):
    import sqlite3
    from munchkin.lab import Lab
    class J:
        def __init__(self):
            self.conn = sqlite3.connect(str(tmp_path / "lab3.db"), check_same_thread=False); self.conn.row_factory = sqlite3.Row
    lab = Lab(J())
    hid = lab.propose("A claim about breadth", "When fewer than 30% of names sit above their 50-day average, SPY is higher ten sessions later more often than usual.", "agent")["id"]
    lab.specify(hid, {"trigger": {"kind": "declarative", "conditions": {"breadth_pct_above_50d": {"lte": 30}}}, "universe": ["SPY"], "side": "long", "holding": {"sessions": 10}, "expected": {"horizon": "+10d", "effect_pct": 1.0}})
    for _ in range(3):
        lab.record_test(hid, "custom", {}, {"error": "no RESULT line"}, "error", ["no RESULT line"])
    assert lab.get(hid)["status"] == "specified"                          # three harness failures reject nothing


def test_portfolio_policy_reserve_sector_slow_and_return_on_time(monkeypatch):
    from munchkin.risk import RiskEngine, RiskState
    from munchkin.styles import effective_limits
    from munchkin.config import RiskLimits
    from munchkin import risk as R
    e = RiskEngine.__new__(RiskEngine); e.L = effective_limits(RiskLimits(), "aggressive")
    class J:
        def reserved_total(self): return 0.0
        def thesis_for(self, sym): return {"horizon": "3-10 trading days"} if sym in ("SO", "PNW") else {"horizon": "hours"}
    e.j = J()
    monkeypatch.setattr(RiskEngine, "_sector_of", lambda self, s: {"SO": "Utilities", "PNW": "Utilities", "NEE": "Utilities", "NVDA": "Information Technology"}.get(s))
    monkeypatch.setattr(RiskEngine, "_atr_of", lambda self, s: {"NEE": 1.2, "NVDA": 3.5}.get(s))
    st = RiskState.__new__(RiskState); st.max_position_notional = 250.0; st.halted = False; st.daily_loss_breached = False; st.positions_count = 2
    st.virtual_equity = 500.0; st.virtual_settled_cash = 150.0
    held = [{"symbol": "SO", "cost_basis": "100", "market_value": "100"}, {"symbol": "PNW", "cost_basis": "195", "market_value": "195"}]
    # reserve: 20% of $500 = $100 must stay; $100 more leaves $50 -> refused
    v = e.policy_checks(st, 100.0, "NVDA", held, "hours", False)
    assert any("cash reserve" in x for x in v)
    st.virtual_settled_cash = 300.0
    # sector cap: utilities already 59% -> any utility add refused
    assert any("sector cap" in x for x in e.policy_checks(st, 50.0, "NEE", held, "hours", False))
    # slow-thesis cap under aggressive (40%): 59% already slow -> a 10-day thesis anywhere is refused
    assert any("slow-thesis cap" in x for x in e.policy_checks(st, 50.0, "NVDA", held, "10 days", False))
    # return-on-time: a 1.2% ATR name over 5 days is ~2.7% < the 3% aggressive bar; as an option it is exempt
    v = e.policy_checks(st, 50.0, "NEE", [], "5 days", False)
    assert any("return-on-time" in x for x in v)
    assert not any("return-on-time" in x for x in e.policy_checks(st, 50.0, "NEE", [], "5 days", True))
    assert not any("return-on-time" in x for x in e.policy_checks(st, 50.0, "NVDA", [], "2 days", False))   # 3.5% x sqrt(2) = 4.9%
    # defensive has no return-on-time bar and a bigger reserve
    e.L = effective_limits(RiskLimits(), "defensive")
    assert not any("return-on-time" in x for x in e.policy_checks(st, 50.0, "NEE", [], "10 days", False))
    st.virtual_settled_cash = 160.0
    assert any("30% cash reserve" in x for x in e.policy_checks(st, 50.0, "NVDA", [], "hours", False))


def test_book_state_flags_and_horizon_parsing():
    from munchkin.book import book_state, parse_horizon_days, trading_days_between
    from munchkin.styles import effective_limits
    from munchkin.config import RiskLimits
    import datetime as dt, pandas as pd
    from munchkin.util import ET
    assert [parse_horizon_days(x) for x in ("2-5 trading days", "hours", "1-2 weeks", "intraday", None)] == [5.0, 0.5, 10.0, 0.5, None]
    assert trading_days_between(dt.datetime(2026, 9, 10, 10, 0, tzinfo=ET), dt.datetime(2026, 9, 15, 10, 0, tzinfo=ET)) == pytest.approx(3.08, abs=0.01)
    class B:
        def positions(self): return [{"symbol": "SO", "market_value": "100", "avg_entry_price": "86.9", "current_price": "86.9", "unrealized_plpc": "0"},
                                     {"symbol": "PNW", "market_value": "195", "avg_entry_price": "95.4", "current_price": "95.4", "unrealized_plpc": "0"}]
    class J:
        def thesis_for(self, sym): return {"horizon": "3-10 trading days", "ts": "2026-09-14T10:32:00-04:00", "target": "100"}
    class S: virtual_equity, virtual_settled_cash = 500.0, 5.11
    table = pd.DataFrame({"sector": ["Utilities", "Utilities"], "beta_spy": [0.3, 0.5], "atr14_pct": [1.1, 1.2]}, index=["SO", "PNW"])
    bs = book_state(B(), J(), effective_limits(RiskLimits(), "aggressive"), "aggressive", table, S())
    assert not bs["reserve_ok"] and bs["deployable"] == 0.0
    assert any("cash $5.11 is below" in f for f in bs["flags"]) and any("Utilities 59%" in f for f in bs["flags"]) and any("slow theses" in f for f in bs["flags"])
    assert not any("PNW is 39.0%" in f for f in bs["flags"])                  # 39% is under the 50% aggressive position cap; the probe rule is at entry
    assert bs["positions"][1]["slow"] and bs["positions"][1]["expected_move_pct"] == pytest.approx(3.79, abs=0.01)


def test_brief_versions_developments_and_diff(tmp_path):
    from munchkin.journal import Journal
    from munchkin import brief as B
    j = Journal(tmp_path / "b.db")
    B.rewrite(j, "## Regime\nrisk-off 31\n## Drivers\noil shock (WTI 103, Reuters 09-15)", session_id=10)
    ok, why = B.rewrite_allowed(j, "intraday")
    assert not ok and "add_development" in why                              # just rebuilt: intraday may only append
    assert B.rewrite_allowed(j, "premarket")[0]
    bullet = B.add_development(j, "Drivers", "Hormuz reopening talks reported", "Reuters 09-15 11:02", session_id=12)
    assert bullet.startswith("- [") and "## Developments" in j.get("world_brief")
    since = B.since(j, session_id=12)                                        # what session 12 sees vs what session 10 wrote
    assert "added:" in since and "Hormuz" in since
    B.rewrite(j, "## Regime\nneutral 46\n## Drivers\noil shock easing (Reuters 09-15)", session_id=20)   # allowed: premarket-style rewrite by phase
    since2 = B.since(j, session_id=20)
    assert "removed:" in since2 and "risk-off 31" in since2
    assert B.missing_sections("## Regime\nx\n## Drivers\ny") == ["Themes", "Calendar", "Risks", "Facts"]
    assert len(B.history(j)) == 3


def test_dial_flips_and_theme_news_wake_only_dependent_positions(tmp_path, monkeypatch):
    from munchkin.watch import Watcher
    from munchkin import watch as W
    import munchkin.macro as M
    class J:
        def __init__(self): self.kv, self.marks = {}, {}
        def get(self, k, d=None): return self.kv.get(k, d)
        def set(self, k, v): self.kv[k] = v
        def thesis_for(self, sym): return {"meta": '{"depends_on": "oil shock; AI data-center power", "invalidated_by": "oil dial flips to headwind"}'} if sym == "SO" else {"meta": "{}"}
    w = Watcher.__new__(Watcher); w.j = J(); w._recently = lambda k, m: False; w._mark = lambda k: None
    monkeypatch.setattr(M, "market_dashboard", lambda: {})
    monkeypatch.setattr(M, "world_dials", lambda tape, reg, vix: [{"key": "oil", "score": -0.9}, {"key": "rates", "score": 0.4}])
    w.j.kv["watch:dials"] = {"oil": 0.5, "rates": 0.4}                        # oil was a tailwind last hour
    monkeypatch.setattr("munchkin.search.web_search", lambda q, c, n, t: [{"title": f"headline about {q}", "url": "u", "date": "2026-09-15T11:00"}])
    ev = w.check_world([{"symbol": "SO"}, {"symbol": "UBER"}])
    flips = [e for e in ev if e.startswith("DIAL FLIP")]
    assert len(flips) == 1 and "oil" in flips[0] and "SO" in flips[0] and "UBER" not in flips[0]
    assert any(e.startswith("THEME NEWS (oil shock; SO)") for e in ev) and any("AI data-center power" in e for e in ev)
    assert w.j.kv["watch:dials"]["oil"] == -0.9 and len(w.j.kv["watch:theme_seen"]) == 2
    ev2 = w.check_world([{"symbol": "SO"}])                                   # same headlines again: silence
    assert not [e for e in ev2 if e.startswith("THEME NEWS")]


def test_options_first_refuses_fast_stock_ideas_with_a_contract(monkeypatch):
    from munchkin.tools import ToolRegistry
    from munchkin.styles import effective_limits
    from munchkin.config import RiskLimits, Settings
    reg = ToolRegistry.__new__(ToolRegistry)
    class Ctx: pass
    ctx = Ctx(); ctx.settings = Settings(risk=effective_limits(RiskLimits(), "aggressive"))
    class B:
        def option_contracts(self, u, a, b, t, limit=2000): return [{"symbol": "X260925C00100000", "open_interest": 500}]
    class M:
        def option_chain(self, u, a, b, m, t, oi_map=None): return [{"symbol": "X260925C00100000", "exp": "2026-09-25", "dte": 9, "delta": 0.52, "bid": 1.10, "ask": 1.20, "type": "call", "strike": 100.0}]
    ctx.broker, ctx.market = B(), M(); reg.ctx = ctx
    r = reg._options_first_block("X", "bullish", "hours", 120.0)
    assert r and r.startswith("REJECTED (Aggressive is options-first)") and "X260925C00100000" in r and "buy_option" in r
    assert reg._options_first_block("X", "bullish", "3-10 trading days", 120.0) is None          # slow theses stay stock
    ctx.settings = Settings(risk=effective_limits(RiskLimits(), "balanced"))
    assert reg._options_first_block("X", "bullish", "hours", 120.0) is None                       # only Aggressive is options-first
    ctx.settings = Settings(risk=effective_limits(RiskLimits(), "aggressive"))
    class M2:
        def option_chain(self, u, a, b, m, t, oi_map=None): return [{"symbol": "X260925C00100000", "exp": "2026-09-25", "dte": 9, "delta": 0.52, "bid": 4.10, "ask": 4.30, "type": "call", "strike": 100.0}]
    ctx.market = M2()
    assert reg._options_first_block("X", "bullish", "hours", 120.0) is None                       # $420 premium: nothing under the cap, stock allowed


def test_probe_minimum_is_enforced_when_cash_allows():
    from munchkin.risk import RiskEngine, RiskState
    from munchkin.styles import effective_limits
    from munchkin.config import RiskLimits
    e = RiskEngine.__new__(RiskEngine); e.L = effective_limits(RiskLimits(), "aggressive"); e.j = type("J", (), {"reserved_total": lambda self: 0.0, "thesis_for": lambda self, s: {}})()
    st = RiskState.__new__(RiskState); st.max_position_notional = 250.0; st.halted = False; st.daily_loss_breached = False; st.positions_count = 1
    st.virtual_equity = 500.0; st.virtual_settled_cash = 320.0
    assert any("probe minimum $100" in x for x in e._entry_common(st, 50.0, "AEE", [], horizon="hours"))
    st.virtual_settled_cash = 140.0                                                              # deployable $40: a $40 probe is all there is
    assert not any("probe minimum" in x for x in e._entry_common(st, 40.0, "AEE", [], horizon="hours"))


def test_bearish_tape_filter_on_arms():
    from munchkin.entries import index_ok
    assert index_ok({"spy_max_chg_pct": -0.4}, -0.6) and not index_ok({"spy_max_chg_pct": -0.4}, 0.1)
    assert index_ok({"spy_min_chg_pct": 0.4}, 0.5) and not index_ok({"spy_min_chg_pct": 0.4}, -0.6)
    assert index_ok({}, None) and not index_ok({"spy_max_chg_pct": -0.4}, None)


def test_working_option_buy_is_repriced_then_cancelled(monkeypatch):
    import datetime as dt
    from munchkin import watch as W
    from munchkin.watch import Watcher
    class J:
        def __init__(self): self.kv, self.decisions = {}, []
        def get(self, k, d=None): return self.kv.get(k, d)
        def set(self, k, v): self.kv[k] = v
        def add_decision(self, *a, **k): self.decisions.append((a, k))
        def all_kv_keys(self, p): return [k for k, v in self.kv.items() if k.startswith(p) and v]
    class B:
        def __init__(self): self.cancelled, self.submitted = [], []
        def cancel_order(self, oid): self.cancelled.append(oid)
        def submit_option_order(self, sym, side, qty, limit, intent, client_order_id=None): self.submitted.append((sym, side, qty, limit, intent)); return {"id": f"o{len(self.submitted)}", "status": "accepted"}
    class M:
        def option_snapshots(self, syms): return [{"symbol": syms[0], "bid": 1.26, "ask": 1.32}]
    j, b = J(), B()
    w = Watcher.__new__(Watcher); w.j, w.b, w.m = j, b, M()
    order = {"id": "o0", "symbol": "VTRS261016C00016000", "side": "buy", "type": "limit", "limit_price": "1.28", "qty": "1"}
    t0 = dt.datetime(2026, 9, 16, 14, 59, tzinfo=W.ET) if hasattr(W, "ET") else None
    from munchkin.util import ET
    t0 = dt.datetime(2026, 9, 16, 14, 59, tzinfo=ET)
    monkeypatch.setattr(W, "now_et", lambda: t0)
    assert w.check_option_orders([order]) == [] and j.kv["optwork:VTRS261016C00016000"]["orig_limit"] == 1.28
    monkeypatch.setattr(W, "now_et", lambda: t0 + dt.timedelta(minutes=5))
    acts = w.check_option_orders([order])
    assert b.cancelled == ["o0"] and b.submitted[0][3] == 1.31 and "re-priced 1.28 -> 1.31" in acts[0]     # min(ask 1.32, 1.28 x 1.02 = 1.3056) -> 1.31
    order2 = {**order, "id": "o1", "limit_price": "1.31"}
    monkeypatch.setattr(W, "now_et", lambda: t0 + dt.timedelta(minutes=10))
    acts = w.check_option_orders([order2])
    assert b.submitted[1][3] == 1.32 and j.kv["optwork:VTRS261016C00016000"]["reprices"] == 2                  # min(ask x 1.01 = 1.3332, orig x 1.03 = 1.3184) -> 1.32
    order3 = {**order, "id": "o2", "limit_price": "1.32"}
    monkeypatch.setattr(W, "now_et", lambda: t0 + dt.timedelta(minutes=15))
    acts = w.check_option_orders([order3])
    assert "cancelled after 15 min" in acts[0] and j.kv.get("optwork:VTRS261016C00016000") is None and any(a[0][1] == "cancel" for a in j.decisions)
    assert w.check_option_orders([{**order, "side": "sell"}]) == []                                          # sells (exits) are never touched


def test_signal_wake_text_offers_the_contract_or_the_reason(tmp_path):
    import sqlite3
    from munchkin.experiments import ExperimentStore, ExperimentConfig, signal_wake_text
    from munchkin.styles import effective_limits
    from munchkin.config import RiskLimits, Settings
    class J:
        def __init__(self):
            self.conn = sqlite3.connect(str(tmp_path / "sw.db"), check_same_thread=False); self.conn.row_factory = sqlite3.Row
    st = ExperimentStore(J()); exp = st.register(ExperimentConfig())
    sid = st.add_signal(exp["id"], "k", "LRCX", "bearish", "2026-09-16T15:40:00-04:00", "2026-09-16T15:40:05-04:00", "2026-09-16T15:40:00-04:00", {"relvol": 1.53, "rs_pct": -2.49, "extension_pct": 0.99, "edge": 90.0}, {})
    class Ctx: pass
    ctx = Ctx(); ctx.settings = Settings(risk=effective_limits(RiskLimits(), "aggressive"))
    class B:
        def option_contracts(self, u, a, b, t, limit=2000): return [{"symbol": "LRCX260925P00090000", "open_interest": 900}]
    class M:
        def option_chain(self, u, a, b, m, t, oi_map=None): return [{"symbol": "LRCX260925P00090000", "exp": "2026-09-25", "dte": 9, "delta": -0.48, "bid": 1.80, "ask": 1.95, "type": "put", "strike": 90.0}]
    ctx.broker, ctx.market = B(), M()
    text = signal_wake_text(st, ctx, sid)
    assert text.startswith("SIGNAL: LRCX bearish opening-range breakout at 15:40") and "expression ready: buy LRCX260925P00090000" in text and "buy_option(" in text
    ctx.settings = Settings(risk=effective_limits(RiskLimits(), "defensive"))
    assert "not expressible under this style" in signal_wake_text(st, ctx, sid)


def test_day_start_equity_comes_from_our_record(tmp_path):
    from munchkin.journal import Journal
    from munchkin.risk import RiskEngine
    j = Journal(tmp_path / "ds.db")
    j.conn.execute("INSERT INTO equity(ts, equity, cash, market_open) VALUES ('2026-09-17T23:38:30-04:00', 413.08, 285.08, 0)")
    j.conn.execute("INSERT INTO equity(ts, equity, cash, market_open) VALUES ('2026-09-16T23:58:14-04:00', 495.16, 495.16, 0)")
    j.conn.commit()
    e = RiskEngine.__new__(RiskEngine); e.j = j
    import munchkin.risk as R, datetime as dt
    from munchkin.util import ET
    e_now = lambda: dt.datetime(2026, 9, 18, 0, 3, tzinfo=ET)
    import unittest.mock as um
    with um.patch.object(R, "now_et", e_now):
        assert e._day_start_equity({"last_equity": "495.16", "equity": "413.0"}) == 413.08    # ours, not the broker's stale figure
    j2 = Journal(tmp_path / "empty.db"); e2 = RiskEngine.__new__(RiskEngine); e2.j = j2
    with um.patch.object(R, "now_et", e_now):
        assert e2._day_start_equity({"last_equity": "500.0", "equity": "499.0"}) == 500.0     # no history: broker fallback


def test_directional_option_cap_and_position_count():
    from munchkin.risk import RiskEngine, RiskState
    from munchkin.styles import effective_limits
    from munchkin.config import RiskLimits
    e = RiskEngine.__new__(RiskEngine); e.L = effective_limits(RiskLimits(), "aggressive")
    st = RiskState.__new__(RiskState); st.virtual_equity = 495.0
    held = [{"symbol": "OXY261002P00059000", "cost_basis": "179.0"}]
    v = e._directional_checks(st, 178.0, "CRWV260925P00075000", held)                   # the second put of 09-17
    assert any("directional cap" in x and "puts" in x for x in v)
    assert not e._directional_checks(st, 150.0, "CRWV260925C00090000", held)            # a call is a different view (30% < 35%)
    held2 = held + [{"symbol": "AAPL261002C00200000", "cost_basis": "120.0"}]
    assert any("max option positions (2)" in x for x in e._directional_checks(st, 50.0, "MSFT261002C00400000", held2))
    assert not any("max option positions" in x for x in e._directional_checks(st, 50.0, "AAPL261002C00200000", held2))   # adding to a held contract


def test_option_through_its_stop_is_sold_at_the_bid(monkeypatch):
    from munchkin.exits import ExitManager
    class J:
        def __init__(self): self.kv, self.decisions = {"exits:CRWV260925P00075000": {"stop_price": 1.5}}, []
        def all_exits(self): return {k[6:]: v for k, v in self.kv.items() if k.startswith("exits:") and v}
        def exits(self, s): return self.kv.get("exits:" + s)
        def clear_exits(self, s): self.kv.pop("exits:" + s, None)
        def set_exits(self, s, **f): self.kv.setdefault("exits:" + s, {}).update(f)
        def add_decision(self, *a, **k): self.decisions.append((a, k))
    class B:
        def __init__(self): self.sold = []; self.cancelled = 0
        def cancel_orders_for(self, sym, side, oo): self.cancelled += 1; return 0
        def submit_option_order(self, sym, side, qty, limit, intent, client_order_id=None): self.sold.append((sym, side, qty, limit, intent)); return {"id": "s1", "status": "accepted"}
        def submit_stop_order(self, *a, **k): raise AssertionError("no stop order should be placed below the market")
    class M:
        def option_snapshots(self, syms): return [{"symbol": syms[0], "bid": 1.36, "ask": 1.44, "mid": 1.40}]
    x = ExitManager.__new__(ExitManager); x.j, x.b, x.m = J(), B(), M()
    acts = x.ensure([{"symbol": "CRWV260925P00075000", "qty": "1"}], [])
    assert x.b.sold == [("CRWV260925P00075000", "sell", 1, 1.36, "sell_to_close")] and acts[0].startswith("STOP:") and "exits:CRWV260925P00075000" not in x.j.kv
    assert x.j.decisions[0][1]["meta"]["mechanical"] is True
