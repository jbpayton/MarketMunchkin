"""The Lab: hypotheses -> tests with controls -> shadow runs -> strategies (docs/hypothesis-lab-spec.md).

Phase 1 (this module): the ledger, spec validation, the gates, and two test templates that run in-process on our own
data (event study with matched-day controls; screen backtest with an unconditional baseline). Custom tests are model
code in the sandbox whose output must carry a RESULT json with a control. Promotion and demotion are operator-only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import numpy as np
import pandas as pd

from .config import SETTINGS

LAB_SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, updated_at TEXT, origin TEXT, origin_ref TEXT,
  title TEXT, statement TEXT, spec_json TEXT, status TEXT DEFAULT 'proposed', notes TEXT DEFAULT '', declined_at TEXT);
CREATE TABLE IF NOT EXISTS hypothesis_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, hypothesis_id INTEGER, session_id INTEGER, created_at TEXT,
  kind TEXT, params_json TEXT, window_start TEXT, window_end TEXT, result_json TEXT, verdict TEXT, reasons TEXT);
CREATE TABLE IF NOT EXISTS shadow_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, hypothesis_id INTEGER, ts TEXT, symbol TEXT, trigger_price REAL,
  entry_px REAL, exit_px REAL, exit_ts TEXT, pnl_pct REAL, status TEXT DEFAULT 'open', reason TEXT);
CREATE TABLE IF NOT EXISTS strategies (id INTEGER PRIMARY KEY AUTOINCREMENT, hypothesis_id INTEGER, name TEXT, skill_name TEXT, detector_json TEXT,
  budget_pct REAL, max_concurrent INTEGER, kill_json TEXT, status TEXT DEFAULT 'active', stats_json TEXT, created_at TEXT, updated_at TEXT);
"""
STATUSES = ["proposed", "specified", "testing", "tested", "shadowing", "live", "paused", "retired", "rejected"]
KINDS = ("event_study", "screen_backtest", "custom")
EXPRESSIONS = ("stock", "call", "put", "call_spread", "put_spread")
# declarative trigger fields the watcher / replay can compute
TRIGGER_FIELDS = {"spy_chg_pct", "qqq_chg_pct", "chg_pct", "gap_pct", "rel_vol", "vix", "vix_chg_pct", "regime_score", "breadth_pct_above_50d",
                  "rsi14", "sma50_dist_pct", "sma200_dist_pct", "atr14_pct", "hi52_dist_pct", "mom_score", "headline_regex", "headline_count_24h",
                  "iv_over_rv", "days_to_earnings"}
OPS = {"lte", "gte", "lt", "gt", "eq", "regex"}
_SAFE_EXPR = re.compile(r"^[A-Za-z0-9_\s<>=!().&|+\-*/,'\"]+$")
_STOP = {"the", "a", "an", "of", "on", "in", "to", "and", "or", "after", "before", "when", "is", "are", "it", "that", "this", "with", "for", "by", "at", "as", "be"}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9%+-]+", (s or "").lower()) if w not in _STOP and len(w) > 1}


# ---------------------------------------------------------------- spec validation
def validate_spec(spec: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be an object"]
    trig = spec.get("trigger")
    if not isinstance(trig, dict) or trig.get("kind") not in ("declarative", "dates", "screen", "script"):
        errs.append("trigger.kind must be one of declarative | dates | screen | script")
    else:
        k = trig["kind"]
        if k == "declarative":
            conds = trig.get("conditions")
            if not isinstance(conds, dict) or not conds:
                errs.append("declarative trigger needs conditions {field: {op: value}}")
            else:
                for f, c in conds.items():
                    if f not in TRIGGER_FIELDS:
                        errs.append(f"unknown trigger field '{f}' (known: {', '.join(sorted(TRIGGER_FIELDS))})")
                    elif not isinstance(c, dict) or not set(c) <= OPS:
                        errs.append(f"condition for '{f}' must be {{lte|gte|lt|gt|eq|regex: value}}")
        elif k == "dates":
            ds = trig.get("dates")
            if not isinstance(ds, list) or not all(re.match(r"^\d{4}-\d{2}-\d{2}$", str(x)) for x in ds):
                errs.append("dates trigger needs dates: [YYYY-MM-DD, ...]")
        elif k == "screen":
            expr = trig.get("expr")
            if not isinstance(expr, str) or not expr.strip() or not _SAFE_EXPR.match(expr):
                errs.append("screen trigger needs expr: a screener query like 'rsi14 < 30 and sma200_dist_pct > -6'")
        elif k == "script" and not isinstance(trig.get("script"), str):
            errs.append("script trigger needs script: name of a sandbox script")
    uni = spec.get("universe")
    if not (isinstance(uni, str) and uni.strip()) and not (isinstance(uni, list) and uni):
        errs.append("universe: a symbol list, 'SPY', or a screener filter like 'screener:sector=Energy'")
    if spec.get("side") not in ("long", "bearish"):
        errs.append("side must be long or bearish (this book never shorts stock; bearish views are expressed with bought puts)")
    hold = spec.get("holding")
    if not isinstance(hold, dict) or not isinstance(hold.get("sessions"), int) or not 1 <= hold["sessions"] <= 60:
        errs.append("holding.sessions must be an integer 1-60 (stop_pct / target_pct optional)")
    exp = spec.get("expected")
    if not isinstance(exp, dict) or not re.match(r"^\+\d{1,2}d$", str(exp.get("horizon", ""))) or not isinstance(exp.get("effect_pct"), (int, float)):
        errs.append("expected: {horizon: '+5d', effect_pct: 1.5}")
    if spec.get("expression", "stock") not in EXPRESSIONS:
        errs.append(f"expression must be one of {EXPRESSIONS}")
    return errs


def spec_horizon(spec: dict[str, Any]) -> int:
    try:
        return int(str(spec["expected"]["horizon"]).strip("+d"))
    except Exception:
        return 5


# ---------------------------------------------------------------- the gates
def judge(result: dict[str, Any], kind: str, spec: dict[str, Any], lab_cfg: Any | None = None) -> tuple[str, list[str]]:
    """pass | fail | inconclusive, with reasons. Defaults from [lab] in munchkin.toml."""
    L = lab_cfg or SETTINGS.lab
    reasons: list[str] = []
    n = int(result.get("n") or 0)
    need = L.min_events if kind == "event_study" else L.min_signals
    if n < need:
        reasons.append(f"n={n} < {need}")
    mean, hit = result.get("mean"), result.get("hit")
    ctrl = result.get("control") or {}
    if mean is None or hit is None:
        return "inconclusive", reasons + ["result lacks mean/hit"]
    if not ctrl or ctrl.get("mean") is None:
        return "inconclusive", reasons + ["no control in result"]
    edge = float(mean) - float(ctrl["mean"])
    hit_edge = float(hit) - float(ctrl.get("hit") or 0)
    if edge < L.min_edge_pct:
        reasons.append(f"edge over control {edge:+.2f}pp < {L.min_edge_pct}")
    if hit_edge < L.min_hit_edge_pts:
        reasons.append(f"hit-rate edge {hit_edge:+.0f}pts < {L.min_hit_edge_pts}")
    halves = result.get("halves") or []
    if len(halves) == 2 and (np.sign(halves[0].get("mean", 0)) != np.sign(halves[1].get("mean", 0)) or halves[0].get("mean", 0) <= 0):
        reasons.append(f"halves disagree ({halves[0].get('mean')} / {halves[1].get('mean')})")
    worst = result.get("worst")
    if worst is not None and mean > 0 and float(worst) < -L.worst_mult * float(mean):
        reasons.append(f"worst {worst} < -{L.worst_mult}x mean")
    if not reasons:
        return "pass", ["n ok", f"edge {edge:+.2f}pp", f"hit edge {hit_edge:+.0f}pts", "halves agree", "worst bounded"]
    # too small a sample is not evidence against the claim: inconclusive (two of those reject it); a negative edge on both counts is a fail
    hard = edge < 0 and hit_edge < 0
    return ("fail" if hard else "inconclusive"), reasons


# ---------------------------------------------------------------- test templates (in-process, our code, our data)
def _fwd_table(closes: pd.Series, dates: list[pd.Timestamp], horizons: list[int], sign: float) -> pd.DataFrame:
    idx = closes.index
    rows = []
    for d in dates:
        i = idx.get_indexer([d])[0]
        if i < 0:
            continue
        rec = {"date": d.strftime("%Y-%m-%d")}
        for h in horizons:
            rec[f"+{h}d"] = float((closes.iloc[i + h] / closes.iloc[i] - 1) * 100 * sign) if i + h < len(closes) else np.nan
        rows.append(rec)
    return pd.DataFrame(rows)


def _summ(t: pd.DataFrame, col: str) -> dict[str, Any]:
    s = t[col].dropna()
    if s.empty:
        return {"n": 0}
    return {"n": int(len(s)), "mean": round(float(s.mean()), 2), "median": round(float(s.median()), 2), "hit": round(float((s > 0).mean() * 100), 1),
            "worst": round(float(s.min()), 2), "best": round(float(s.max()), 2)}


def _daily(market: Any, sym: str, bars: int = 780) -> pd.Series:
    df = market.bars(sym, "1D", bars)
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    return pd.Series(df["close"].astype(float).values, index=idx.normalize())


def run_event_study(market: Any, spec: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Forward returns after event dates, versus matched days (every day meeting the mechanical condition, story or not)."""
    horizon = spec_horizon(spec)
    horizons = sorted({1, 3, 5, 10, horizon})
    sign = -1.0 if spec.get("side") == "bearish" else 1.0
    uni = params.get("symbols") or (spec["universe"] if isinstance(spec["universe"], list) else [spec["universe"] if spec["universe"].upper() == "SPY" else "SPY"])
    ref = params.get("reference", "SPY")
    ref_c = _daily(market, ref)
    ref_chg = ref_c.pct_change() * 100
    replay = params.get("replay") or (spec["trigger"] if spec["trigger"].get("kind") == "declarative" else None)
    dates: list[pd.Timestamp] = []
    if spec["trigger"].get("kind") == "dates" or params.get("dates"):
        dates = [pd.Timestamp(d) for d in (params.get("dates") or spec["trigger"]["dates"])]
    elif replay:
        conds = replay.get("conditions") or {}
        c = conds.get("spy_chg_pct") or conds.get("chg_pct") or {}
        thr = float(c.get("lte", c.get("lt", -1.5)))
        dates = list(ref_chg[ref_chg <= thr].index)
    if not dates:
        return {"error": "no event dates: give trigger.dates or a declarative spy_chg_pct condition to replay"}
    # control: matched days on the reference symbol; threshold = the events' median day change (or the replay threshold)
    ev_chg = ref_chg.reindex(dates).dropna()
    thr_ctrl = float(params.get("control_chg_lte", ev_chg.median() if len(ev_chg) else -1.5))
    ctrl_dates = [d for d in ref_chg[ref_chg <= thr_ctrl].index if d not in set(dates)]
    ev_rows, ctrl_rows = [], []
    for sym in uni[:20]:
        try:
            cl = _daily(market, sym)
        except Exception:
            continue
        ev_rows.append(_fwd_table(cl, dates, horizons, sign).assign(symbol=sym))
        ctrl_rows.append(_fwd_table(cl, ctrl_dates, horizons, sign).assign(symbol=sym))
    ev = pd.concat(ev_rows) if ev_rows else pd.DataFrame()
    ct = pd.concat(ctrl_rows) if ctrl_rows else pd.DataFrame()
    col = f"+{horizon}d"
    if ev.empty or col not in ev:
        return {"error": "no bars for the universe"}
    out = _summ(ev, col)
    out.update({"kind": "event_study", "horizon": horizon, "symbols": uni[:20], "events": sorted({r for r in ev["date"]}),
                "by_horizon": {f"+{h}d": _summ(ev, f"+{h}d") for h in horizons},
                "control": {**_summ(ct, col), "definition": f"{ref} day change <= {thr_ctrl:.2f}%, excluding event days"} if not ct.empty else {},
                "window": [str(ref_c.index[0].date()), str(ref_c.index[-1].date())]})
    evs = ev.dropna(subset=[col]).sort_values("date")
    if len(evs) >= 4:
        mid = len(evs) // 2
        out["halves"] = [_summ(evs.iloc[:mid], col), _summ(evs.iloc[mid:], col)]
    return out


def run_screen_backtest(market: Any, spec: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Signal days from a screener expression over daily indicator history, forward returns versus the unconditional baseline."""
    from . import screener as scr
    from .analytics import screen_frame
    expr = params.get("expr") or (spec["trigger"].get("expr") if spec["trigger"].get("kind") == "screen" else None)
    if not expr or not _SAFE_EXPR.match(expr):
        return {"error": "screen_backtest needs expr (screener query syntax)"}
    horizon = spec_horizon(spec)
    horizons = sorted({1, 3, 5, 10, 20, horizon})
    sign = -1.0 if spec.get("side") == "bearish" else 1.0
    hold = int(spec.get("holding", {}).get("sessions", horizon))
    syms = params.get("symbols")
    if not syms:
        uni = spec["universe"]
        if isinstance(uni, list):
            syms = uni
        else:
            table, _ = scr.load()
            if table is None:
                return {"error": "screener table not built; pass symbols"}
            t = table
            m = re.match(r"screener:(\w+)=(.+)", str(uni))
            if m and m.group(1) in t.columns:
                t = t[t[m.group(1)].astype(str).str.lower() == m.group(2).strip().lower()]
            syms = list(t.sort_values("avg_dollar_vol20_m", ascending=False).index[:int(params.get("max_symbols", 120))]) if "avg_dollar_vol20_m" in t.columns else list(t.index[:120])
    syms = [s.upper() for s in syms][:int(params.get("max_symbols", 120))]
    years = int(params.get("years", 3))
    frames = scr.build_frames(market, syms, limit=int(252 * years + 30))
    sig_rows, base_rows = [], []
    for sym, f in frames.items():
        if len(f) < 80:
            continue
        g = f.copy() if "price" in f.columns else screen_frame(f)
        c = g["price"]
        for h in horizons:
            g[f"+{h}d"] = (c.shift(-h) / c - 1) * 100 * sign
        cols = [f"+{h}d" for h in horizons]
        base_rows.append(g[cols].dropna().assign(symbol=sym))
        try:
            hit = g.query(expr, engine="python")
        except Exception as e:
            return {"error": f"bad expr: {str(e)[:120]}"}
        keep, last = [], None
        for ts in hit.index:
            if last is None or (ts - last).days >= hold:
                keep.append(ts); last = ts
        if keep:
            sig_rows.append(hit.loc[keep, cols].assign(symbol=sym, date=[t.strftime("%Y-%m-%d") for t in keep]))
    if not sig_rows:
        return {"kind": "screen_backtest", "n": 0, "error": "no signals", "symbols": len(frames)}
    sig = pd.concat(sig_rows); base = pd.concat(base_rows)
    col = f"+{horizon}d"
    out = _summ(sig, col)
    out.update({"kind": "screen_backtest", "horizon": horizon, "expr": expr, "symbols": int(sig["symbol"].nunique()), "universe_size": len(frames),
                "by_horizon": {f"+{h}d": _summ(sig, f"+{h}d") for h in horizons},
                "control": {**_summ(base, col), "definition": "all days, same symbols and window (unconditional)"}})
    s2 = sig.dropna(subset=[col]).sort_values("date")
    if len(s2) >= 4:
        mid = len(s2) // 2
        out["halves"] = [_summ(s2.iloc[:mid], col), _summ(s2.iloc[mid:], col)]
    return out


def parse_custom_result(output: str) -> dict[str, Any]:
    """A custom test must print `RESULT: {json}` with n, mean, hit, worst and control{mean, hit, n}."""
    m = re.search(r"RESULT:\s*(\{.*\})", output, re.S)
    if not m:
        return {"error": "no RESULT: {...} line in the output"}
    try:
        d = json.loads(m.group(1))
    except Exception as e:
        return {"error": f"RESULT is not valid json: {e}"}
    need = {"n", "mean", "hit", "worst", "control"}
    if not need <= set(d) or not isinstance(d["control"], dict) or "mean" not in d["control"]:
        return {"error": f"RESULT must contain {sorted(need)} with control.mean and control.hit"}
    d["kind"] = "custom"
    return d


# ---------------------------------------------------------------- the ledger
class Lab:
    def __init__(self, journal: Any) -> None:
        self.j = journal
        self.conn = journal.conn
        self.conn.executescript(LAB_SCHEMA)
        self.conn.commit()

    # ---- rows
    def _row(self, r: Any) -> dict[str, Any]:
        d = dict(r)
        for k in ("spec_json",):
            try:
                d["spec"] = json.loads(d.pop(k) or "null")
            except Exception:
                d["spec"] = None
        return d

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = "SELECT * FROM hypotheses" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT ?"
        args = (status, limit) if status else (limit,)
        return [self._row(r) for r in self.conn.execute(q, args).fetchall()]

    def get(self, hid: int) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM hypotheses WHERE id=?", (int(hid),)).fetchone()
        if not r:
            return None
        d = self._row(r)
        d["tests"] = [dict(t) for t in self.conn.execute("SELECT id, created_at, kind, verdict, reasons, window_start, window_end, result_json, params_json FROM hypothesis_tests WHERE hypothesis_id=? ORDER BY id", (int(hid),)).fetchall()]
        for t in d["tests"]:
            try:
                t["result"] = json.loads(t.pop("result_json") or "null"); t["params"] = json.loads(t.pop("params_json") or "null")
            except Exception:
                t["result"] = None
        d["shadow"] = [dict(s) for s in self.conn.execute("SELECT * FROM shadow_signals WHERE hypothesis_id=? ORDER BY id DESC LIMIT 50", (int(hid),)).fetchall()]
        return d

    def similar(self, statement: str, threshold: float = 0.6) -> dict[str, Any] | None:
        toks = _tokens(statement)
        if not toks:
            return None
        best, score = None, 0.0
        for h in self.list(limit=500):
            t2 = _tokens(h["statement"]) | _tokens(h["title"])
            jac = len(toks & t2) / max(1, len(toks | t2))
            if jac > score:
                best, score = h, jac
        return best if best is not None and score >= threshold else None

    # ---- lifecycle
    def propose(self, title: str, statement: str, origin: str, origin_ref: str | None = None, spec: dict[str, Any] | None = None) -> dict[str, Any]:
        title, statement = (title or "").strip()[:140], (statement or "").strip()[:1500]
        if len(statement) < 40:
            return {"error": "statement too short: say what happens, when, to what, over what horizon"}
        dup = self.similar(statement)
        if dup:
            last = self.get(dup["id"])
            verdict = (last["tests"][-1]["verdict"] if last and last["tests"] else None)
            return {"error": f"looks like hypothesis #{dup['id']} '{dup['title']}' ({dup['status']}{', last verdict ' + verdict if verdict else ''}); read it with get_hypothesis instead of re-proposing", "duplicate_of": dup["id"]}
        status = "proposed"
        if spec is not None and not validate_spec(spec):
            status = "specified"
        cur = self.conn.execute("INSERT INTO hypotheses(created_at, updated_at, origin, origin_ref, title, statement, spec_json, status) VALUES (?,?,?,?,?,?,?,?)",
                                (_now(), _now(), origin, origin_ref, title, statement, json.dumps(spec) if spec else None, status))
        self.conn.commit()
        return {"id": cur.lastrowid, "status": status}

    def specify(self, hid: int, spec: dict[str, Any]) -> dict[str, Any]:
        h = self.get(hid)
        if not h:
            return {"error": f"no hypothesis #{hid}"}
        errs = validate_spec(spec)
        if errs:
            return {"error": "spec invalid", "problems": errs}
        if h["status"] in ("live", "paused", "retired"):
            return {"error": f"#{hid} is {h['status']}; a live strategy's spec is changed by the operator"}
        self.conn.execute("UPDATE hypotheses SET spec_json=?, status=?, updated_at=? WHERE id=?", (json.dumps(spec), "specified", _now(), int(hid)))
        self.conn.commit()
        return {"id": int(hid), "status": "specified"}

    def note(self, hid: int, text: str) -> bool:
        h = self.get(hid)
        if not h:
            return False
        notes = (h.get("notes") or "") + f"\n[{_now()[:16]}] {text.strip()[:600]}"
        self.conn.execute("UPDATE hypotheses SET notes=?, updated_at=? WHERE id=?", (notes.strip()[-6000:], _now(), int(hid)))
        self.conn.commit()
        return True

    def set_status(self, hid: int, status: str, reason: str = "") -> bool:
        if status not in STATUSES or not self.get(hid):
            return False
        self.conn.execute("UPDATE hypotheses SET status=?, updated_at=?, declined_at=? WHERE id=?", (status, _now(), _now() if status == "rejected" else None, int(hid)))
        self.conn.commit()
        if reason:
            self.note(hid, f"status -> {status}: {reason}")
        return True

    def record_test(self, hid: int, kind: str, params: dict[str, Any], result: dict[str, Any], verdict: str, reasons: list[str], session_id: int | None = None) -> int:
        w = result.get("window") or [None, None]
        cur = self.conn.execute("INSERT INTO hypothesis_tests(hypothesis_id, session_id, created_at, kind, params_json, window_start, window_end, result_json, verdict, reasons) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (int(hid), session_id, _now(), kind, json.dumps(params), w[0], w[1], json.dumps(result, default=str)[:60000], verdict, "; ".join(reasons)))
        # lifecycle: pass -> tested; fail -> rejected; two inconclusives -> rejected
        h = self.get(hid)
        prior = [t["verdict"] for t in (h or {}).get("tests", [])]
        if verdict == "pass" or (h and h["status"] == "tested"):
            new = "tested"            # a later non-pass never demotes a claim that already passed; the record shows it
        elif verdict == "error":
            new = h["status"] if h else "specified"   # fix the test and run it again; nothing moved
        elif verdict == "fail" or prior.count("inconclusive") >= 2:
            new = "rejected"
        else:
            new = "specified"
        if h and h["status"] in ("proposed", "specified", "testing", "tested"):
            self.conn.execute("UPDATE hypotheses SET status=?, updated_at=? WHERE id=?", (new, _now(), int(hid)))
        self.conn.commit()
        return cur.lastrowid

    # ---- text for prompts / tools
    def index_text(self, limit: int = 30) -> str:
        rows = self.list(limit=limit)
        if not rows:
            return "(no hypotheses yet)"
        out = []
        for h in rows:
            t = self.conn.execute("SELECT verdict, created_at FROM hypothesis_tests WHERE hypothesis_id=? ORDER BY id DESC LIMIT 1", (h["id"],)).fetchone()
            out.append(f"- #{h['id']} [{h['status']}{', ' + t['verdict'] if t else ''}] {h['title']} ({h['origin']})")
        return "\n".join(out)

    def describe(self, hid: int) -> str:
        h = self.get(hid)
        if not h:
            return f"no hypothesis #{hid}"
        lines = [f"#{h['id']} {h['title']} [{h['status']}] origin {h['origin']}{' ' + str(h['origin_ref']) if h['origin_ref'] else ''}", h["statement"],
                 "spec: " + (json.dumps(h["spec"]) if h["spec"] else "(not specified yet)")]
        for t in h["tests"]:
            r = t.get("result") or {}
            c = r.get("control") or {}
            lines.append(f"test #{t['id']} {t['kind']} {t['created_at'][:16]}: verdict {t['verdict']} ({t['reasons']}) | n={r.get('n')} mean {r.get('mean')} hit {r.get('hit')} worst {r.get('worst')} "
                         f"| control n={c.get('n')} mean {c.get('mean')} hit {c.get('hit')}" + (f" | halves {[x.get('mean') for x in r.get('halves', [])]}" if r.get('halves') else "") + (f" | error {r['error']}" if r.get("error") else ""))
        if h.get("shadow"):
            closed = [s for s in h["shadow"] if s["status"] == "closed"]
            lines.append(f"shadow: {len(h['shadow'])} signals, {len(closed)} closed, mean {np.mean([s['pnl_pct'] for s in closed]):+.2f}%" if closed else f"shadow: {len(h['shadow'])} signals open")
        if h.get("notes"):
            lines.append("notes: " + h["notes"][-1500:])
        return "\n".join(lines)


def run_test(lab: Lab, market: Any, hid: int, kind: str, params: dict[str, Any] | None, session_id: int | None = None,
             custom_output: str | None = None) -> dict[str, Any]:
    """Run a template (or parse a custom run's output), judge it, record it, advance the status. Returns the record."""
    h = lab.get(hid)
    if not h:
        return {"error": f"no hypothesis #{hid}"}
    if not h.get("spec"):
        return {"error": f"#{hid} has no spec yet; call specify_hypothesis first"}
    if kind not in KINDS:
        return {"error": f"kind must be one of {KINDS}"}
    params = dict(params or {})
    spec = h["spec"]
    if kind == "event_study":
        result = run_event_study(market, spec, params)
    elif kind == "screen_backtest":
        result = run_screen_backtest(market, spec, params)
    else:
        result = parse_custom_result(custom_output or "")
    if result.get("error"):
        verdict, reasons = "error", [result["error"]]     # a harness or format failure says nothing about the claim
    else:
        verdict, reasons = judge(result, kind, spec)
    tid = lab.record_test(hid, kind, params, result, verdict, reasons, session_id)
    return {"test_id": tid, "verdict": verdict, "reasons": reasons, "result": result, "status": lab.get(hid)["status"]}
