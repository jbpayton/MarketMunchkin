"""Mobile dashboard: FastAPI JSON API + a single-page UI. Read-only; never touches credentials."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import HALT_FILE, SETTINGS, redact
from .util import ET, fnum, is_option, now_et, occ_human

app = FastAPI(title="MarketMunchkin")
_TOKEN = os.environ.get("MUNCHKIN_WEB_TOKEN", "")
_cache: dict[str, tuple[float, Any]] = {}
_ctx = None


def ctx():
    global _ctx
    if _ctx is None:
        from .agent import make_context
        from .exits import ExitManager
        _ctx = make_context()
        _ctx.exits = ExitManager(_ctx.broker, _ctx.market, _ctx.journal)
    return _ctx


def cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


@app.middleware("http")
async def auth(request: Request, call_next):
    if _TOKEN:
        tok = request.query_params.get("token") or request.cookies.get("mm_token")
        if tok != _TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        resp = await call_next(request)
        if request.query_params.get("token"):
            resp.set_cookie("mm_token", _TOKEN, max_age=90 * 86400, httponly=True, samesite="lax")
        return resp
    return await call_next(request)


def _daemon_status() -> str:
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", "munchkin-daemon"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ------------------------------------------------------------------ API
@app.get("/api/overview")
def api_overview():
    c = ctx()
    j = c.journal

    def build():
        st = c.risk.state().as_dict()
        pos = c.broker.positions()
        oo = c.broker.open_orders()
        positions = []
        for p in pos:
            sym = p["symbol"]
            ex = c.exits.describe(sym, oo)
            th = j.thesis_for(sym) or {}
            positions.append({
                "symbol": occ_human(sym) if is_option(sym) else sym, "raw": sym, "qty": fnum(p["qty"], 4),
                "avg_entry": fnum(p["avg_entry_price"], 3), "last": fnum(p["current_price"], 3),
                "value": fnum(p["market_value"]), "pnl": fnum(p["unrealized_pl"]), "pnl_pct": fnum(float(p["unrealized_plpc"]) * 100, 2),
                "today_pct": fnum(float(p["change_today"]) * 100, 2), "stop": ex["stop_level"], "target": ex["target_level"],
                "resting_stop": ex["resting_stop"], "thesis": th.get("thesis"), "horizon": th.get("horizon"),
                "grade": (json.loads(th.get("meta") or "{}").get("catalyst_grade") if th else None),
            })
        orders = [{"id": o.get("id"), "symbol": occ_human(o.get("symbol") or "") if o.get("symbol") else "MLEG", "side": o.get("side"),
                   "type": o.get("type"), "qty": o.get("qty") or o.get("notional"), "limit": o.get("limit_price"),
                   "stop": o.get("stop_price"), "tif": o.get("time_in_force"), "status": o.get("status")} for o in oo]
        from .entries import EntryBook
        armed = list(EntryBook(j).all().values())
        today = now_et().date().isoformat()
        by_day = j.realized_by_day()
        realized_today = by_day.get(today, 0.0)
        realized_total = round(sum(by_day.values()), 2)
        return {
            "now": now_et().strftime("%Y-%m-%d %H:%M:%S"), "risk": st, "positions": positions, "orders": orders,
            "stats": j.trade_stats(), "realized_today": realized_today, "realized_total": realized_total, "realized_by_day": by_day,
            "daemon": _daemon_status(), "halted": HALT_FILE.exists(), "armed": armed,
            "last_session_end": j.get("watch:last_session_end"), "world_brief_ts": j.get("world_brief_ts"),
            "events": j.events(12), "starting_capital": SETTINGS.risk.starting_capital, "max_positions": SETTINGS.risk.max_positions,
            "cadence": {"interval_min": SETTINGS.watch.intraday_interval_min, "poll_s": SETTINGS.watch.poll_seconds},
        }

    return cached("overview", 20, build)


@app.get("/api/equity")
def api_equity(range: str = "1d"):
    j = ctx().journal
    now = now_et()
    if range == "1d":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        pts = j.equity_series(since)
        ours = [{"t": p["ts"][11:16], "ts": p["ts"], "equity": p["equity"]} for p in pts]
        base: list[dict] = []
        try:  # broker 5-minute history for the day, then our finer samples after its last point
            hist = cached("hist1d", 120, lambda: ctx().broker.portfolio_history(period="1D", timeframe="5Min"))
            base = [{"t": dt.datetime.fromtimestamp(ts, tz=ET).strftime("%H:%M"), "ts": dt.datetime.fromtimestamp(ts, tz=ET).isoformat(), "equity": e}
                    for ts, e in zip(hist.get("timestamp", []), hist.get("equity", [])) if e]
            base = [b for b in base if b["ts"][:10] == now.date().isoformat()]
        except Exception:
            pass
        last_ts = base[-1]["ts"] if base else ""
        out = base + [o for o in ours if o["ts"] > last_ts]
        return {"range": range, "points": out}
    days = {"1w": 7, "1m": 31, "3m": 93}.get(range, 31)
    since = (now - dt.timedelta(days=days)).isoformat()
    pts = j.equity_series(since, limit=20000)
    # thin to at most ~400 points, always keeping the last
    step = max(1, len(pts) // 400)
    thin = pts[::step]
    if pts and (not thin or thin[-1] is not pts[-1]):
        thin.append(pts[-1])
    out = [{"t": p["ts"][5:16].replace("T", " "), "ts": p["ts"], "equity": p["equity"]} for p in thin]
    if len(out) < 2:
        try:  # fall back to broker history for older days
            hist = ctx().broker.portfolio_history(period="1M", timeframe="1D")
            out = [{"t": dt.datetime.fromtimestamp(ts, tz=ET).strftime("%m-%d"), "ts": dt.datetime.fromtimestamp(ts, tz=ET).isoformat(), "equity": e}
                   for ts, e in zip(hist.get("timestamp", []), hist.get("equity", [])) if e]
        except Exception:
            pass
    return {"range": range, "points": out}


@app.get("/api/sessions")
def api_sessions(limit: int = 40):
    j = ctx().journal
    out = []
    for s in j.sessions(limit):
        summ = s.get("summary") or ""
        actions = ""
        if "## Actions" in summ:
            actions = summ.split("## Actions", 1)[1].split("##", 1)[0].strip()
        out.append({"id": s["id"], "phase": s["phase"], "task": s.get("task"), "started_at": s["started_at"], "ended_at": s.get("ended_at"),
                    "tool_calls": s.get("tool_calls"), "dry_run": bool(s.get("dry_run")), "actions": actions[:400],
                    "has_summary": bool(summ)})
    return out


@app.get("/api/sessions/{sid}")
def api_session(sid: int):
    j = ctx().journal
    s = j.session(sid)
    if not s:
        raise HTTPException(404)
    rows = j.trace(sid)
    decisions = [d for d in j.decisions(200) if d.get("session_id") == sid]
    return {"session": s, "trace": [{k: (redact(v) if isinstance(v, str) else v) for k, v in r.items()} for r in rows], "decisions": decisions}


@app.get("/api/journal")
def api_journal(what: str = "decisions", limit: int = 50):
    j = ctx().journal
    if what == "trades":
        return {"rows": j.trades(limit), "stats": j.trade_stats()}
    if what == "lessons":
        return {"rows": j.lessons(limit)}
    if what == "notes":
        return {"rows": j.notes(limit)}
    if what == "events":
        return {"rows": j.events(limit)}
    rows = j.decisions(limit)
    for r in rows:
        try:
            r["meta"] = json.loads(r.get("meta") or "{}")
        except Exception:
            r["meta"] = {}
        r["symbol_h"] = occ_human(r["symbol"]) if r.get("symbol") and is_option(r["symbol"]) else r.get("symbol")
    return {"rows": rows}


@app.get("/api/brain")
def api_brain():
    j = ctx().journal
    return {"world_brief": j.get("world_brief") or "", "world_brief_ts": j.get("world_brief_ts"),
            "plan": j.get_plan(), "plan_ts": j.get("plan_ts"), "playbook": j.playbook(), "lessons": j.lessons(40),
            "tasks_open": j.open_tasks(30), "tasks_recent": [t for t in j.tasks_history(30) if t["status"] != "open"][:15]}


@app.post("/api/halt")
async def api_halt(request: Request):
    """Kill switch: HALT blocks all new entries (exits, resting stops and the watcher keep working). Resume lifts it."""
    if request.headers.get("x-requested-with") != "munchkin":
        raise HTTPException(403, "bad request origin")
    body = await request.json()
    action = (body or {}).get("action")
    if action == "halt":
        HALT_FILE.write_text(f"halted from dashboard {now_et().isoformat(timespec='seconds')}\n")
        ctx().journal.add_event("halt", "HALT set from the dashboard: no new entries until resumed")
    elif action == "resume":
        HALT_FILE.unlink(missing_ok=True)
        ctx().journal.add_event("halt", "HALT lifted from the dashboard")
    else:
        raise HTTPException(400, "action must be halt or resume")
    _cache.pop("overview", None)
    return {"halted": HALT_FILE.exists()}


# ------------------------------------------------------------------ config (providers / keys)
@app.get("/api/config")
def api_config():
    from . import providers as P
    st = P.status()
    st["token_protected"] = bool(_TOKEN)
    return st


@app.post("/api/config")
async def api_config_set(request: Request):
    from . import providers as P
    if request.headers.get("x-requested-with") != "munchkin":
        raise HTTPException(403, "bad request origin")
    body = await request.json()
    action = body.get("action")
    cfg = P.load_config()
    if action == "set_key":
        prov, val = body.get("provider"), (body.get("value") or "").strip()
        if prov not in P.KEY_NAMES:
            raise HTTPException(400, "unknown provider")
        if val and (len(val) < 8 or any(ch.isspace() for ch in val)):
            raise HTTPException(400, "that does not look like an API key")
        P.set_key(prov, val or None)
        if val:
            cfg["enabled"][prov] = True
            P.save_config(cfg)
        ctx().journal.add_event("config", f"{prov} API key {'set' if val else 'cleared'} from the dashboard")
    elif action == "toggle":
        prov = body.get("provider")
        if prov not in ("tavily", "finnhub", "brave", "searxng"):
            raise HTTPException(400, "unknown provider")
        cfg["enabled"][prov] = bool(body.get("enabled"))
        P.save_config(cfg)
        ctx().journal.add_event("config", f"{prov} {'enabled' if cfg['enabled'][prov] else 'disabled'} from the dashboard")
    elif action == "order":
        order = [x for x in body.get("order", []) if x in ("tavily", "searxng", "brave")]
        if not order:
            raise HTTPException(400, "order must list at least one search provider")
        cfg["order"] = order
        P.save_config(cfg)
    elif action == "budget":
        prov, val = body.get("provider"), body.get("value")
        if prov not in cfg["budgets"] and prov not in ("tavily", "finnhub", "brave"):
            raise HTTPException(400, "unknown provider")
        cfg["budgets"][prov] = None if val in (None, "", 0) else int(val)
        P.save_config(cfg)
    elif action == "merge_news":
        cfg["merge_news"] = bool(body.get("value"))
        P.save_config(cfg)
    else:
        raise HTTPException(400, "unknown action")
    return P.status()


@app.post("/api/config/test")
async def api_config_test(request: Request):
    from . import providers as P
    if request.headers.get("x-requested-with") != "munchkin":
        raise HTTPException(403, "bad request origin")
    body = await request.json()
    return P.test_provider(body.get("provider", ""))


# ------------------------------------------------------------------ UI
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="theme-color" content="#0f1115">
<title>MarketMunchkin</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e8ee;--dim:#8b93a7;--up:#3ecf8e;--dn:#ff5c7a;--acc:#6ea8fe;--warn:#ffb347;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
header{position:sticky;top:0;z-index:5;background:rgba(15,17,21,.95);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:calc(env(safe-area-inset-top) + 10px) 14px 8px}
h1{font-size:17px;margin:0;display:flex;align-items:center;gap:8px}h1 small{color:var(--dim);font-weight:400;font-size:12px}
nav{display:flex;gap:6px;margin-top:8px;overflow-x:auto}nav button{flex:1;background:var(--card);color:var(--dim);border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:14px}
nav button.on{color:var(--fg);border-color:var(--acc);background:#1b2233}
main{padding:12px 12px calc(env(safe-area-inset-bottom) + 24px);max-width:900px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px}
.card h2{font-size:13px;color:var(--dim);margin:0 0 8px;text-transform:uppercase;letter-spacing:.04em;display:flex;justify-content:space-between;align-items:center}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.kpi{background:#11141a;border:1px solid var(--line);border-radius:12px;padding:10px}
.kpi .l{font-size:11px;color:var(--dim)}.kpi .v{font-size:19px;font-weight:600;margin-top:2px}.kpi .s{font-size:11px;color:var(--dim)}
.up{color:var(--up)}.dn{color:var(--dn)}.warn{color:var(--warn)}.dim{color:var(--dim)}.mono{font-family:var(--mono);font-size:12.5px}
.row{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-top:1px solid var(--line)}.row:first-of-type{border-top:0}
.pos .sym{font-weight:600;font-size:16px}.pos .sub{font-size:12px;color:var(--dim);margin-top:2px}
.pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim);margin-right:4px}
.pill.ok{border-color:var(--up);color:var(--up)}.pill.bad{border-color:var(--dn);color:var(--dn)}.pill.acc{border-color:var(--acc);color:var(--acc)}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font-family:var(--mono);font-size:12.5px;line-height:1.45}
.md{white-space:pre-wrap;word-break:break-word;font-size:14px}.md b{color:#fff}
.seg{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.seg button{background:#11141a;color:var(--dim);border:1px solid var(--line);border-radius:999px;padding:5px 11px;font-size:13px}.seg button.on{color:var(--fg);border-color:var(--acc)}
.sess{cursor:pointer}.sess .t{font-weight:600}.sess .a{font-size:13px;color:#c9cfdb;margin-top:4px}
.tr{border-left:3px solid var(--line);padding:6px 10px;margin:8px 0;border-radius:6px;background:#11141a}
.tr.tool{border-color:var(--acc)}.tr.reasoning{border-color:#7a5cff}.tr.assistant{border-color:var(--up)}.tr.final{border-color:var(--warn)}.tr.prompt,.tr.system{border-color:#444}
.tr .h{font-size:12px;color:var(--dim);display:flex;justify-content:space-between;cursor:pointer}.tr .h b{color:var(--fg)}
.tr .body{margin-top:6px;display:none}.tr.open .body{display:block}
.back{background:none;border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:6px 12px;margin-bottom:10px}
svg{width:100%;height:150px;display:block}.axis{font-size:10px;fill:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px 4px;border-top:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase}
.ev{font-size:13px;padding:6px 0;border-top:1px solid var(--line)}.ev .ts{color:var(--dim);font-size:11px;margin-right:6px}
.err{color:var(--dn)}
.btn{background:#1b2233;color:var(--fg);border:1px solid var(--acc);border-radius:12px;padding:12px 16px;font-size:15px;width:100%}
.btn.danger{border-color:var(--dn);color:var(--dn);background:#2a1620}
.confirm{margin-top:10px;border:1px solid var(--warn);border-radius:12px;padding:10px;background:#221c12}
</style></head><body>
<header><h1>🐣 MarketMunchkin <small id="now"></small><small id="daemon" class="pill"></small></h1>
<nav><button data-tab="overview" class="on">Overview</button><button data-tab="sessions">Sessions</button><button data-tab="journal">Journal</button><button data-tab="brain">Brain</button><button data-tab="config">Config</button></nav></header>
<main id="main"></main>
<script>
const $=s=>document.querySelector(s);const esc=s=>(s??'').toString().replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const money=v=>(v==null?'–':(v<0?'-':'')+'$'+Math.abs(v).toFixed(2));const pct=v=>v==null?'':(v>=0?'+':'')+Number(v).toFixed(2)+'%';
const cls=v=>v>0?'up':v<0?'dn':'';const md=s=>esc(s).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/^(#+ .*)$/gm,'<b>$1</b>');
let tab='overview',range='1d',jwhat='decisions',sessionId=null,timer=null;
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error(u+' '+r.status);return r.json();}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;sessionId=null;document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));render();});
function chart(points,base){if(!points.length)return '<div class="dim">no equity samples yet</div>';const w=600,h=150,p=28;const ys=points.map(q=>q.equity);let lo=Math.min(...ys,base),hi=Math.max(...ys,base);if(hi-lo<1){lo-=1;hi+=1}const x=i=>p+i*(w-p-6)/Math.max(1,points.length-1),y=v=>h-16-(v-lo)*(h-24)/(hi-lo);const d=points.map((q,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(q.equity).toFixed(1)).join(' ');const last=ys[ys.length-1];const col=last>=base?'#3ecf8e':'#ff5c7a';const yb=y(base);return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line x1="${p}" x2="${w-6}" y1="${yb}" y2="${yb}" stroke="#333a48" stroke-dasharray="4 4"/><path d="${d}" fill="none" stroke="${col}" stroke-width="2"/><text class="axis" x="2" y="${y(hi)+4}">${hi.toFixed(0)}</text><text class="axis" x="2" y="${y(lo)+4}">${lo.toFixed(0)}</text><text class="axis" x="${p}" y="${h-3}">${esc(points[0].t)}</text><text class="axis" x="${w-80}" y="${h-3}">${esc(points[points.length-1].t)}</text></svg>`;}
async function render(){const m=$('#main');try{if(tab==='overview')await overview(m);else if(tab==='sessions')await sessions(m);else if(tab==='journal')await journal(m);else if(tab==='config')await config(m);else await brain(m);}catch(e){m.innerHTML='<div class="card err">'+esc(e.message)+'</div>';}}
async function cfgPost(body){const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'munchkin'},body:JSON.stringify(body)});if(!r.ok){throw new Error((await r.json()).detail||('HTTP '+r.status));}return r.json();}
async function setKey(p){const v=$('#key-'+p).value.trim();if(!v){return;}try{await cfgPost({action:'set_key',provider:p,value:v});$('#key-'+p).value='';render();}catch(e){alert(e.message);}}
async function clearKey(p){if(!confirm('Clear the '+p+' key?'))return;await cfgPost({action:'set_key',provider:p,value:''});render();}
async function toggleProv(p,en){await cfgPost({action:'toggle',provider:p,enabled:en});render();}
async function testProv(p){const el=$('#test-'+p);el.textContent='testing…';const r=await fetch('/api/config/test',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'munchkin'},body:JSON.stringify({provider:p})});const d=await r.json();el.innerHTML=(d.ok?'<span class="up">ok</span> ':'<span class="dn">fail</span> ')+esc(d.detail);}
async function setOrder(){const v=$('#order').value.split(',').map(x=>x.trim()).filter(Boolean);await cfgPost({action:'order',order:v});render();}
async function setBudget(p){const v=$('#budget-'+p).value.trim();await cfgPost({action:'budget',provider:p,value:v?parseInt(v):null});render();}
async function config(m){const c=await j('/api/config');const provs=[['tavily','Tavily','web + news search (LLM-oriented; returns page content). Free tier ≈1,000 calls/month.'],['finnhub','Finnhub','company news, earnings calendar, basic metrics. Free tier 60 calls/min.'],['brave','Brave Search','web + news search (client not wired yet; key stored for later).'],['searxng','SearXNG','self-hosted meta-search, free fallback. '+esc(c.searxng.url||'')]];
m.innerHTML=`<div class="card"><h2>Providers</h2><div class="dim" style="font-size:13px;margin-bottom:8px">${c.token_protected?'Dashboard token is set; key changes require it.':'<span class="warn">No dashboard token set: anyone on this network can change these. Set MUNCHKIN_WEB_TOKEN in .env to lock it.</span>'} Keys are stored in <code>.env</code> (mode 600), shown masked, never exposed to the model. Changes apply immediately, no restart.</div>
${provs.map(([p,name,desc])=>{const s=c[p];return `<div class="row" style="display:block"><div><b>${name}</b> <span class="pill ${s.active?'ok':s.enabled_flag?'bad':''}">${s.active?'active':s.enabled_flag?(s.has_key?'active':'no key'):'disabled'}</span> <span class="dim" style="font-size:12px">${desc}</span></div>
${p!=='searxng'?`<div class="sub" style="margin-top:6px">key: <span class="mono">${esc(s.key_masked||'not set')}</span> · used this month: ${s.usage_month}${s.budget?' / '+s.budget:''}</div>
<div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap"><input id="key-${p}" type="password" placeholder="paste API key" style="flex:1;min-width:160px;background:#11141a;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px"><button class="btn" style="width:auto" onclick="setKey('${p}')">Save</button>${s.has_key?`<button class="btn" style="width:auto" onclick="clearKey('${p}')">Clear</button>`:''}</div>
<div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;align-items:center"><span class="dim" style="font-size:12px">monthly budget</span><input id="budget-${p}" value="${s.budget??''}" style="width:90px;background:#11141a;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px"><button class="btn" style="width:auto;padding:6px 10px" onclick="setBudget('${p}')">Set</button></div>`:''}
<div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap"><button class="btn" style="width:auto;padding:8px 12px" onclick="toggleProv('${p}',${!s.enabled_flag})">${s.enabled_flag?'Disable':'Enable'}</button><button class="btn" style="width:auto;padding:8px 12px" onclick="testProv('${p}')">Test</button><span id="test-${p}" class="dim" style="font-size:12px;align-self:center"></span></div></div>`;}).join('')}</div>
<div class="card"><h2>Search chain</h2><div class="dim" style="font-size:13px">Order of providers for web/news queries. The first with results wins; news queries merge the first two (${c.merge_news?'on':'off'}).</div><div style="display:flex;gap:6px;margin-top:8px"><input id="order" value="${esc(c.order.join(', '))}" style="flex:1;background:#11141a;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px"><button class="btn" style="width:auto" onclick="setOrder()">Save</button></div><div style="margin-top:8px"><button class="btn" style="width:auto;padding:8px 12px" onclick="cfgPost({action:'merge_news',value:${!c.merge_news}}).then(render)">${c.merge_news?'Turn news merge off':'Turn news merge on'}</button></div></div>`;}
async function overview(m){const [o,eq]=await Promise.all([j('/api/overview'),j('/api/equity?range='+range)]);const r=o.risk;$('#now').textContent=o.now.slice(5,16);const d=$('#daemon');d.textContent='daemon '+o.daemon+(o.halted?' · HALT':'');d.className='pill '+(o.daemon==='active'&&!o.halted?'ok':'bad');
const day=r.daily_pnl,tot=r.virtual_equity-o.starting_capital;
m.innerHTML=`<div class="card"><div class="kpis">
<div class="kpi"><div class="l">Equity</div><div class="v">${money(r.virtual_equity)}</div><div class="s ${cls(tot)}">${money(tot)} (${pct(tot/o.starting_capital*100)}) all-time</div></div>
<div class="kpi"><div class="l">Today</div><div class="v ${cls(day)}">${money(day)}</div><div class="s">${pct(r.daily_pnl_pct)} · realized ${money(o.realized_today)}</div></div>
<div class="kpi"><div class="l">Buying power</div><div class="v">${money(r.buying_power_now)}</div><div class="s">settled · cash ${money(r.virtual_cash)}</div></div></div>
<div style="margin-top:10px" class="seg">${['1d','1w','1m','3m'].map(x=>`<button class="${x===range?'on':''}" onclick="range='${x}';render()">${x}</button>`).join('')}<span class="dim" style="font-size:12px;align-self:center">${r.market_open?'market open':'market closed'} · ${eq.points.length} pts</span></div>${chart(eq.points,o.starting_capital)}</div>
<div class="card"><h2>Positions <span class="dim">${r.positions_count} of ${o.max_positions}</span></h2>${o.positions.length?o.positions.map(p=>`<div class="row pos"><div><div class="sym">${esc(p.symbol)} <span class="pill ${p.grade==='confirmed'?'ok':p.grade?'acc':''}">${esc(p.grade||'–')}</span></div><div class="sub">${p.qty} @ ${p.avg_entry} · last ${p.last} · ${esc(p.horizon||'')}</div><div class="sub">stop <b class="${p.resting_stop==='NONE'?'dn':''}">${p.stop??'–'}</b> ${p.resting_stop==='NONE'?'<span class="pill bad">no resting stop</span>':'<span class="pill ok">resting</span>'} · target <b>${p.target??'–'}</b></div><div class="sub">${esc((p.thesis||'').slice(0,180))}</div></div><div style="text-align:right"><div class="${cls(p.pnl)}" style="font-weight:600">${money(p.pnl)}</div><div class="sub ${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</div><div class="sub">${money(p.value)}</div></div></div>`).join(''):'<div class="dim">flat</div>'}</div>
<div class="card"><h2>Risk</h2><div class="mono">day trades (5d): ${r.day_trades_used_5d} · positions ${r.positions_count} · per-position cap ${money(r.max_position_notional)} · options budget ${money(r.max_new_options_premium)}<br>loss breaker: ${r.daily_loss_breached?'<span class="dn">TRIPPED</span>':'ok'} · unsettled ${money(r.unsettled_proceeds)}<br>${r.restrictions.length?'<span class="warn">'+esc(r.restrictions.join('; '))+'</span>':'no restrictions'}</div></div>
<div class="card"><h2>Armed entries <span class="dim">watcher executes on trigger</span></h2>${(o.armed||[]).length?o.armed.map(a=>`<div class="row"><div><b>${esc(a.symbol)}</b> buy $${a.notional} when ${esc(a.direction)} <b>${a.trigger_price}</b><div class="sub">stop ${a.stop_price} · target ${a.target_price} · ${esc(a.catalyst_grade)} · expires ${esc((a.expires||'never').slice(5,16).replace('T',' '))}</div><div class="sub">${esc((a.thesis||'').slice(0,140))}</div></div></div>`).join(''):'<div class="dim">none armed</div>'}</div>
<div class="card"><h2>Open orders</h2>${o.orders.length?o.orders.map(x=>`<div class="row"><div>${esc(x.symbol)} <span class="pill">${esc(x.side)} ${esc(x.type)}</span> ${x.qty} ${x.stop?'stop '+x.stop:''} ${x.limit?'limit '+x.limit:''}</div><div class="dim">${esc(x.tif)} · ${esc(x.status)}</div></div>`).join(''):'<div class="dim">none</div>'}</div>
<div class="card"><h2>Track record</h2><div class="mono">realized (incl. partial exits): <b class="${cls(o.realized_total)}">${money(o.realized_total)}</b><br>${o.stats.closed_trades?`${o.stats.closed_trades} round trips · win ${o.stats.win_rate_pct}% · P&L ${money(o.stats.total_pnl)} · avg win ${money(o.stats.avg_win)} / loss ${money(o.stats.avg_loss)}`:'no completed round trips yet'}</div></div>
<div class="card" id="kill"><h2>Kill switch</h2>${o.halted?`<div class="warn" style="margin-bottom:8px">HALTED: the agent will not open new positions. Exits, resting stops and the watcher still run.</div><button class="btn" onclick="armKill('resume')">Resume trading</button>`:`<div class="dim" style="margin-bottom:8px">Blocks new entries immediately. Existing positions keep their resting stops and the agent can still exit.</div><button class="btn danger" onclick="armKill('halt')">Halt new entries</button>`}<div id="killconfirm"></div></div>
<div class="card"><h2>Recent activity <span class="dim">last session ${esc((o.last_session_end||'').slice(11,16))} · every ${o.cadence.interval_min}m + events</span></h2>${o.events.map(e=>`<div class="ev"><span class="ts">${esc(e.ts.slice(5,16).replace('T',' '))}</span><span class="pill ${e.kind==='event'?'acc':e.kind==='error'?'bad':''}">${esc(e.kind)}</span> ${esc(e.text)}</div>`).join('')||'<div class="dim">nothing yet</div>'}</div>`;}
async function sessions(m){if(sessionId!==null)return session(m);const list=await j('/api/sessions?limit=60');m.innerHTML=list.map(s=>`<div class="card sess" onclick="sessionId=${s.id};render()"><div class="t">#${s.id} ${esc(s.phase)} ${s.dry_run?'<span class="pill">dry run</span>':''} <span class="dim" style="font-weight:400">${esc(s.started_at.slice(5,16).replace('T',' '))} · ${s.tool_calls??'?'} tools${s.ended_at?'':' · <span class=warn>running</span>'}</span></div>${s.task?`<div class="a dim">task: ${esc(s.task.slice(0,200))}</div>`:''}<div class="a">${md(s.actions||(s.has_summary?'':'(no summary)'))}</div></div>`).join('')||'<div class="card dim">no sessions</div>';}
async function session(m){const d=await j('/api/sessions/'+sessionId);const s=d.session;m.innerHTML=`<button class="back" onclick="sessionId=null;render()">← sessions</button><div class="card"><h2>#${s.id} ${esc(s.phase)} · ${esc(s.started_at.slice(0,16).replace('T',' '))} → ${esc((s.ended_at||'').slice(11,16))} · ${s.tool_calls} tools · ${s.prompt_tokens} prompt tok</h2>${s.task?`<div class="md dim">task: ${esc(s.task)}</div>`:''}</div>
${d.decisions.length?`<div class="card"><h2>Decisions this session</h2>${d.decisions.map(x=>`<div class="row"><div><b>${esc(x.kind)}</b> ${esc(x.symbol)} ${esc(x.side||'')} ${x.qty??''} @ ${x.price??''} <span class="pill ${x.status==='filled'?'ok':x.status==='blocked'?'bad':''}">${esc(x.status)}</span><div class="dim" style="font-size:12px">${esc(x.thesis||'')}</div>${x.stop?`<div class="dim" style="font-size:12px">stop ${esc(x.stop)} · target ${esc(x.target||'')}</div>`:''}</div></div>`).join('')}</div>`:''}
<div class="card"><h2>Trace <span class="dim">tap to expand</span></h2>${d.trace.map(t=>{const k=t.kind;let title=k,body='';if(k==='tool'){title='▶ '+t.name;body='<div class="dim mono">'+esc(t.args)+'</div><pre>'+esc(t.result)+'</pre>';}else if(k==='reasoning'){title='thinking';body='<pre class="dim">'+esc(t.result)+'</pre>';}else{body='<div class="md">'+md(t.result)+'</div>';}const open=(k==='assistant'||k==='final')?' open':'';return `<div class="tr ${k}${open}"><div class="h" onclick="this.parentElement.classList.toggle('open')"><span><b>${esc(title)}</b> <span class="dim">${k==='tool'?esc((t.args||'').slice(0,70)):''}</span></span><span>${t.secs?t.secs+'s · ':''}${esc(t.ts.slice(11,19))}</span></div><div class="body">${body}</div></div>`;}).join('')}</div>`;}
async function journal(m){const d=await j('/api/journal?what='+jwhat+'&limit=80');let body='';if(jwhat==='decisions')body='<table><tr><th>when</th><th>what</th><th>status</th></tr>'+d.rows.map(r=>`<tr><td class="dim">${esc(r.ts.slice(5,16).replace('T',' '))}</td><td><b>${esc(r.kind)}</b> ${esc(r.symbol_h)} ${esc(r.side||'')} ${r.qty??''} @ ${r.price??''}${r.meta.catalyst_grade?' <span class="pill acc">'+esc(r.meta.catalyst_grade)+'</span>':''}<div class="dim" style="font-size:12px">${esc(r.thesis||r.meta.reason||'')}</div>${r.stop||r.target?`<div class="dim" style="font-size:12px">stop ${esc(r.stop||'')} · target ${esc(r.target||'')}</div>`:''}${r.meta.violations?`<div class="dn" style="font-size:12px">${esc(r.meta.violations.join('; '))}</div>`:''}</td><td><span class="pill ${r.status==='filled'?'ok':r.status==='blocked'?'bad':''}">${esc(r.status)}</span></td></tr>`).join('')+'</table>';
else if(jwhat==='trades')body=`<div class="mono dim" style="margin-bottom:8px">${esc(JSON.stringify(d.stats))}</div><table><tr><th>symbol</th><th>in → out</th><th>P&L</th></tr>`+d.rows.map(r=>`<tr><td>${esc(occ(r.symbol))}<div class="dim" style="font-size:11px">${esc(r.opened_at.slice(5,16))} → ${esc(r.closed_at.slice(5,16))}</div></td><td>${r.qty} @ ${r.entry} → ${r.exit}</td><td class="${cls(r.pnl)}">${money(r.pnl)}<div class="dim" style="font-size:11px">${pct(r.pnl_pct)}</div></td></tr>`).join('')+'</table>';
else if(jwhat==='lessons')body=d.rows.map(r=>`<div class="ev"><span class="ts">${esc(r.ts.slice(0,10))}</span>${esc(r.text)}</div>`).join('');
else if(jwhat==='notes')body=d.rows.map(r=>`<div class="ev"><span class="ts">${esc(r.ts.slice(5,16))}</span>${esc(r.text)}</div>`).join('');
else body=d.rows.map(e=>`<div class="ev"><span class="ts">${esc(e.ts.slice(5,16).replace('T',' '))}</span><span class="pill ${e.kind==='event'?'acc':e.kind==='error'?'bad':''}">${esc(e.kind)}</span> ${esc(e.text)}</div>`).join('');
m.innerHTML=`<div class="seg">${['decisions','trades','lessons','notes','events'].map(x=>`<button class="${x===jwhat?'on':''}" onclick="jwhat='${x}';render()">${x}</button>`).join('')}</div><div class="card">${body||'<div class=dim>nothing yet</div>'}</div>`;}
function occ(s){const m=/^([A-Z]{1,6})(\d{6})([CP])(\d{8})$/.exec(s||'');return m?`${m[1]} 20${m[2].slice(0,2)}-${m[2].slice(2,4)}-${m[2].slice(4)} ${(+m[4]/1000)}${m[3]}`:s;}
async function brain(m){const d=await j('/api/brain');m.innerHTML=`<div class="card"><h2>Task queue <span class="dim">${d.tasks_open.length} open</span></h2>${d.tasks_open.map(t=>`<div class="ev"><span class="pill acc">p${t.priority} ${esc(t.kind)}</span> #${t.id} ${esc(t.text)}</div>`).join('')||'<div class=dim>queue empty</div>'}${d.tasks_recent.length?`<div class="dim" style="margin-top:8px;font-size:12px">recently closed:</div>`+d.tasks_recent.map(t=>`<div class="ev dim"><span class="ts">${esc((t.done_at||'').slice(5,16).replace('T',' '))}</span>#${t.id} ${esc(t.status)}: ${esc((t.result||t.text).slice(0,160))}</div>`).join(''):''}</div><div class="card"><h2>State of the world <span class="dim">${esc((d.world_brief_ts||'').slice(5,16).replace('T',' '))}</span></h2><div class="md">${md(d.world_brief||'(not written yet)')}</div></div><div class="card"><h2>Plan <span class="dim">${esc((d.plan_ts||'').slice(5,16).replace('T',' '))}</span></h2><div class="md">${md(d.plan||'(none)')}</div></div><div class="card"><h2>Lessons</h2>${d.lessons.map(l=>`<div class="ev"><span class="ts">${esc(l.ts.slice(0,10))}</span>${esc(l.text)}</div>`).join('')||'<div class=dim>none yet</div>'}</div><div class="card"><h2>Playbook</h2><div class="md">${md(d.playbook)}</div></div>`;}
let killTimer=null;
function armKill(action){const box=$('#killconfirm');clearTimeout(killTimer);const verb=action==='halt'?'halt new entries':'resume trading';box.innerHTML=`<div class="confirm"><div><b>Are you sure you want to ${verb}?</b><div class="dim" style="font-size:12px">This tap window closes in 8 seconds.</div></div><div style="display:flex;gap:8px;margin-top:8px"><button class="btn ${action==='halt'?'danger':''}" onclick="doKill('${action}')">Yes, ${verb}</button><button class="btn" onclick="$('#killconfirm').innerHTML=''">Cancel</button></div></div>`;killTimer=setTimeout(()=>{box.innerHTML='';},8000);}
async function doKill(action){clearTimeout(killTimer);const box=$('#killconfirm');box.innerHTML='<div class="dim">working…</div>';try{const r=await fetch('/api/halt',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'munchkin'},body:JSON.stringify({action})});if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json();box.innerHTML=`<div class="${d.halted?'warn':'up'}">${d.halted?'Halted.':'Resumed.'}</div>`;setTimeout(render,800);}catch(e){box.innerHTML='<div class="err">'+esc(e.message)+'</div>';}}
render();setInterval(()=>{if(tab==='overview'&&sessionId===null&&!$('#killconfirm')?.innerHTML)render();},60000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
