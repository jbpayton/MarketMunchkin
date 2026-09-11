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
    assert t.gate("INTC") == [f"INTC has not been grounded in news this session (get_news with symbols=[INTC] or web_search mentioning INTC)"]
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
