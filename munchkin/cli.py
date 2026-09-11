"""Command-line interface: `munchkin --help`."""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

from .config import DATA_DIR, HALT_FILE, LOG_DIR, SETTINGS

app = typer.Typer(help="MarketMunchkin: autonomous LLM trading agent on Alpaca (paper).", no_args_is_help=True)
screener_app = typer.Typer(help="Technical screener over the universe.")
app.add_typer(screener_app, name="screener")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_DIR / "munchkin.log"), logging.StreamHandler()] if verbose
                        else [logging.FileHandler(LOG_DIR / "munchkin.log")])
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _printer(quiet: bool):
    def on_event(kind: str, d: dict) -> None:
        if quiet:
            return
        if kind == "status":
            console.print(f"[dim]· {d['text']}[/dim]")
        elif kind == "reasoning":
            txt = d["text"].strip()
            console.print(f"[dim italic]{txt[:600]}{'…' if len(txt) > 600 else ''}[/dim italic]")
        elif kind == "assistant":
            console.print(Panel(d["text"], title="agent", border_style="green"))
        elif kind == "tool":
            console.print(f"[bold cyan]▶ {d['name']}[/bold cyan] [cyan]{d['args'][:300]}[/cyan] [dim]({d['secs']}s)[/dim]")
            res = d["result"]
            console.print(f"[white]{res[:700]}{'…' if len(res) > 700 else ''}[/white]")
    return on_event


@app.command()
def status() -> None:
    """Account, risk state, positions, open orders, trade stats."""
    _setup_logging(False)
    from .agent import make_context
    ctx = make_context()
    ctx.risk.sync_fills()
    st = ctx.risk.state()
    console.print(Panel(json.dumps(st.as_dict(), indent=1, default=str), title="risk state"))
    from .tools import ToolRegistry
    reg = ToolRegistry(ctx)
    console.print(Panel(reg.call("get_positions", {}), title="positions"))
    console.print(Panel(reg.call("get_orders", {}), title="open orders"))
    console.print(Panel(json.dumps(ctx.journal.trade_stats()), title="closed-trade stats"))
    plan = ctx.journal.get_plan()
    if plan:
        console.print(Panel(plan[:2000], title="current plan"))


@app.command()
def run(phase: str = typer.Option("intraday", help="premarket | intraday | event | postmarket | research | reflect | study | adhoc"),
        task: Optional[str] = typer.Option(None, help="extra instruction for the agent"),
        dry_run: bool = typer.Option(False, help="validate + journal orders but do not submit"),
        no_trade: bool = typer.Option(False, help="disable order tools entirely"),
        refresh: bool = typer.Option(False, help="force screener refresh first"),
        quiet: bool = typer.Option(False), verbose: bool = typer.Option(False)) -> None:
    """Run one agent session."""
    _setup_logging(verbose)
    from .agent import run_session
    t0 = time.time()
    res = run_session(phase=phase, task=task, dry_run=dry_run, allow_trading=not no_trade,
                      on_event=_printer(quiet), refresh_screener=True if refresh else None)
    console.print(Panel(res.final_text, title=f"session summary ({phase})", border_style="magenta"))
    console.print(f"[dim]{res.tool_calls} tool calls, {res.steps} steps, prompt {res.prompt_tokens} tok (last), "
                  f"completion {res.completion_tokens} tok (reasoning {res.reasoning_tokens}), {time.time()-t0:.0f}s[/dim]")


@app.command()
def chat(question: str, trade: bool = typer.Option(False, help="allow order tools"),
         quiet: bool = typer.Option(False)) -> None:
    """Ask the agent a question with full tool access (read-only unless --trade)."""
    _setup_logging(False)
    from .agent import run_session
    res = run_session(phase="adhoc", task=question, allow_trading=trade, on_event=_printer(quiet), refresh_screener=False, memory_writes=trade)
    console.print(Panel(res.final_text, title="answer", border_style="magenta"))


@app.command()
def daemon(once: bool = typer.Option(False, help="one loop iteration and exit")) -> None:
    """Scheduler + watcher: fixed pre/post-market sessions, a 30-min intraday baseline, and event-driven wakes."""
    _setup_logging(False)
    from .agent import make_context, run_session
    from .exits import ExitManager
    from .util import now_et
    from .watch import Watcher
    import signal
    S, W = SETTINGS.schedule, SETTINGS.watch
    ctx = make_context()
    j = ctx.journal
    exits = ExitManager(ctx.broker, ctx.market, j)
    watcher = Watcher(ctx.broker, ctx.market, j, exits, W, risk=ctx.risk)
    n_orphans = j.close_orphans()
    if n_orphans:
        j.add_event("error", f"closed {n_orphans} orphaned session(s) left by a previous daemon stop")
    stop_requested = {"flag": False}
    degraded = {"flag": False}
    import queue
    import threading
    wq: "queue.Queue[tuple[str, str]]" = queue.Queue()
    watcher_thread: dict = {"t": None}
    market_state = {"open": False}

    def _watcher_loop() -> None:
        """Own context (own SQLite connection, broker and risk engine); ticks every poll interval no matter what the main loop is doing."""
        from .styles import effective_limits as _el, effective_watch as _ew, get_style as _gs
        wctx = make_context()
        wj = wctx.journal
        wex = ExitManager(wctx.broker, wctx.market, wj)
        ww = Watcher(wctx.broker, wctx.market, wj, wex, W, risk=wctx.risk)
        cur_style = None
        while not stop_requested["flag"]:
            t0 = time.time()
            try:
                style = _gs(wj)
                if style != cur_style:
                    wctx.risk.L = _el(SETTINGS.risk, style)
                    ww.cfg = _ew(SETTINGS.watch, style)
                    cur_style = style
                mo = market_state["open"]
                events, actions = ww.tick(mo)
                for a in actions:
                    logging.info("exits: %s", a)
                    wj.add_event("action", a)
                    if not a.startswith("chore:") and "dropped" not in a and "expired" not in a:
                        TG.notify(a, kind="fills")
                for e in events:
                    logging.info("event: %s", e)
                    wj.add_event("event", e)
                    TG.notify(e, kind="events")
                    wq.put(("event", e))
            except Exception as e:
                logging.warning("watcher tick failed: %s", e)
            sleep_s = (ww.cfg.poll_seconds if market_state["open"] else 300) - (time.time() - t0)
            for _ in range(int(max(1.0, sleep_s))):
                if stop_requested["flag"]:
                    return
                time.sleep(1)

    def _on_term(signum, frame):  # let the current session finish, then exit the loop
        stop_requested["flag"] = True
        console.print("[yellow]stop requested; finishing the current session[/yellow]")
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    failures = {"n": 0}
    console.print(f"daemon started {now_et():%Y-%m-%d %H:%M} ET; premarket={S.premarket} postmarket={S.postmarket} "
                  f"intraday every {W.intraday_interval_min}m {W.intraday_start}-{W.intraday_end} + events (cooldown {W.event_cooldown_min}m)")
    trading_day_cache: dict[str, bool] = {}
    pending: list[str] = []
    _prev = j.get("watch:last_session_end")
    last_session_end: dt.datetime | None = dt.datetime.fromisoformat(_prev) if _prev else None
    last_intraday: dt.datetime | None = last_session_end   # restarts respect the cadence instead of re-running immediately
    last_watch: dt.datetime | None = None

    def _hm(t: str) -> tuple[int, int]:
        hh, mm = t.split(":")
        return int(hh), int(mm)

    def _in_offhours(now: dt.datetime, is_td: bool) -> bool:
        t = now.time()
        windows = W.offhours_windows if is_td else [W.weekend_window]
        for w in windows:
            a, b_ = w.split("-")
            ah, am = _hm(a)
            bh, bm = _hm(b_)
            if dt.time(ah, am) <= t <= dt.time(bh, bm):
                return True
        return False

    def _in_study(now: dt.datetime) -> bool:
        if not W.study_enabled:
            return False
        sh, sm = _hm(W.study_start)
        eh, em = _hm(W.study_end)
        t, start, end = now.time(), dt.time(sh, sm), dt.time(eh, em)
        return (start <= t <= end) if start <= end else (t >= start or t <= end)

    def _study_done_tonight() -> int:
        cutoff = (dt.datetime.now() - dt.timedelta(hours=12)).isoformat(timespec="minutes")
        return sum(1 for r in j.sessions(12) if r.get("phase") == "study" and (r.get("started_at") or "") >= cutoff)

    def _lab_done_tonight() -> int:
        cutoff = (dt.datetime.now() - dt.timedelta(hours=12)).isoformat(timespec="minutes")
        return sum(1 for r in j.sessions(20) if r.get("phase") == "lab" and (r.get("started_at") or "") >= cutoff)

    def _lab_task() -> str | None:
        """Oldest hypothesis that needs work: proposed (needs a spec + test) or specified (needs a test)."""
        from .lab import Lab
        lab = Lab(j)
        for status in ("specified", "proposed"):
            rows = lab.list(status, 50)
            if rows:
                h = rows[-1]
                return (f"LAB: hypothesis #{h['id']} '{h['title']}' is {status} (origin {h['origin']}). Statement: {h['statement'][:600]}. "
                        + ("Run the matching test." if status == "specified" else "Write the spec, then run the matching test."))
        return None

    def _rotation_task() -> str:
        """State-aware duty for a session with no event and an empty queue. The opportunity board is the default;
        housekeeping items run only when they are actually stale."""
        now = now_et()
        def age_h(key: str) -> float:
            v = j.get(key)
            if not v:
                return 1e9
            try:
                return (now - dt.datetime.fromisoformat(v)).total_seconds() / 3600
            except Exception:
                return 1e9
        board = ("DUTY: OPPORTUNITY BOARD. Rank the 5 best trade candidates for the next 1-3 sessions with exact entry (price or trigger), "
                 "stop, target, size (size_position), expression (stock / call / put / debit spread with the IV read) and catalyst grade with source. "
                 "If a setup is valid at the current price, BUY the probe now (buy_stock / buy_option) and, if you want more on weakness, arm the add-on; "
                 "arm-only is for triggers that are genuinely not met yet (breakouts, reclaims, event-conditioned entries) and they must sit within one "
                 "daily ATR of the price. Setups whose backtest measured entry at the signal close are bought at the signal, not below it. A board that "
                 "ends with zero new exposure while candidates qualify needs a one-line reason per candidate.")
        if age_h("duty:brief_verified") > 3 and age_h("world_brief_ts") > 3:
            j.set("duty:brief_verified", now.isoformat(timespec="seconds"))
            return "DUTY: re-verify every number in the world brief with get_macro_data / get_macro_release; rewrite it with sources. Then do the opportunity board."
        if age_h("duty:grade") > 2 and j.fills(since=now - dt.timedelta(hours=6)):
            j.set("duty:grade", now.isoformat(timespec="seconds"))
            return "DUTY: grade the decisions behind the last fills against what happened; record at most one rule-style lesson. Then do the opportunity board."
        if age_h("duty:new_dossiers") > 1.5:
            j.set("duty:new_dossiers", now.isoformat(timespec="seconds"))
            return "DUTY: find 2 NEW names (not in today's dossiers) from get_setups / get_intraday_setups / movers that fit the regime; research_symbol each; then do the opportunity board including them."
        if age_h("duty:board") > 0.5:
            j.set("duty:board", now.isoformat(timespec="seconds"))
            return board
        return ("DUTY: MONITOR. Check every held and armed name against the tape (get_quotes, screen_intraday) and fresh news; adjust a stop, "
                "target or arm only if its thesis changed, and say in one line per name why it stands. Do not re-run the full opportunity board "
                "(it runs every 30 minutes) and do not re-arm entries that are already armed. If nothing changed, say so in three lines and stop.")

    def _chores(now: dt.datetime, is_td: bool) -> None:
        """No-LLM housekeeping: IV history snapshots daily, earnings + backtest weekly."""
        from . import screener as scr
        today = now.date().isoformat()
        if is_td and now.time() >= dt.time(10, 5) and not j.get(f"ran:{today}:chore:iv"):
            j.set(f"ran:{today}:chore:iv", now.isoformat(timespec="seconds"))
            try:
                table, _ = scr.load()
                plan = (j.get_plan() or "") + " " + (j.get("world_brief") or "")
                import re as _re
                syms = {s_ for s_ in _re.findall(r"\b[A-Z]{2,5}\b", plan) if table is not None and s_ in table.index}
                syms |= {"SPY", "QQQ", "IWM"} | {(__import__("munchkin.util", fromlist=["parse_occ"]).parse_occ(p["symbol"]) or {}).get("underlying", p["symbol"]) for p in ctx.broker.positions()}
                n = 0
                for s_ in sorted(syms)[:15]:
                    try:
                        rv = float(table.loc[s_]["rvol20_pct"]) if table is not None and s_ in table.index else None
                        a = ctx.market.option_analytics(s_, None, rv)
                        if "error" not in a:
                            j.record_iv(s_, a.get("iv30_pct"), rv)
                            n += 1
                    except Exception:
                        pass
                j.add_event("action", f"chore: recorded IV snapshots for {n} names")
            except Exception as e:
                logging.warning("iv chore failed: %s", e)
        if now.weekday() == 5 and now.time() >= dt.time(9, 0) and not j.get(f"ran:{today}:chore:weekly"):
            j.set(f"ran:{today}:chore:weekly", now.isoformat(timespec="seconds"))
            try:
                scr.refresh_earnings(scr.build_universe(force=True), force=True)
                scr.backtest(ctx.market, years=3)
                j.add_event("action", "chore: weekly earnings cache + setup backtest refreshed")
            except Exception as e:
                logging.warning("weekly chore failed: %s", e)

    from . import telegram as TG

    def _operator_task(nt: dict) -> str:
        return (f"OPERATOR REQUEST #{nt['id']} (sent from the operator's phone; answer it directly and concisely, under 1500 characters of plain "
                f"text; do only what it asks; close it with complete_task): {nt['text']}")

    def _run(phase: str, task: str | None = None, task_id: int | None = None) -> None:
        nonlocal last_session_end, last_intraday
        console.print(f"[bold]{now_et():%H:%M} running {phase}{' (' + task[:80] + ')' if task else ''}[/bold]")
        j.add_event("session", f"{phase} session started" + (f": {task[:300]}" if task else ""))
        try:
            res = run_session(phase=phase, task=task, on_event=_printer(False))
            console.print(Panel(res.final_text, title=f"{phase} summary", border_style="magenta"))
            j.add_event("session", f"{phase} session finished ({res.tool_calls} tool calls)")
            failures["n"] = 0
            if task_id is not None:
                chat = j.get(f"telegram:task:{task_id}")
                j.complete_task(task_id, "answered")
                if chat is not None:
                    TG.notify(f"Request #{task_id}:\n{res.final_text}", kind="replies", chat_id=int(chat), force=True)
            if phase in ("premarket", "postmarket"):
                TG.notify(f"{phase.capitalize()} summary\n{res.final_text[:3500]}", kind="sessions")
        except Exception as e:
            logging.exception("session failed")
            console.print(f"[red]session failed: {e}[/red]")
            j.add_event("error", f"{phase} session failed: {str(e)[:300]}")
            failures["n"] += 1
            j.close_orphans("session failed: " + str(e)[:200])
            TG.notify(f"Session failed ({phase}): {str(e)[:300]}\nBacking off {min(30, 5 * failures['n'])} min.", kind="errors")
        last_session_end = now_et()
        j.set("watch:last_session_end", last_session_end.isoformat(timespec="seconds"))
        if phase in ("intraday", "event"):
            last_intraday = last_session_end
        try:
            watcher.mark_session_done()
        except Exception as e:
            logging.warning("baseline reset failed: %s", e)

    from .styles import effective_limits, effective_watch, get_style
    current_style = None
    while True:
        now = now_et()
        today = now.date().isoformat()
        style = get_style(j)
        W = effective_watch(SETTINGS.watch, style)
        watcher.cfg = W
        if style != current_style:
            # the style switch applies to the watcher too: armed entries that fire between sessions are checked
            # against the new caps, instrument flags and breaker, not the ones the daemon booted with
            ctx.risk.L = effective_limits(SETTINGS.risk, style)
            ctx.settings = SETTINGS.model_copy(update={"risk": ctx.risk.L})
            ctx.style = style
            if current_style is not None:
                logging.info("trading style changed %s -> %s; watcher limits updated", current_style, style)
            current_style = style
        if today not in trading_day_cache:
            try:
                trading_day_cache[today] = ctx.broker.is_trading_day(now.date())
            except Exception as e:
                logging.warning("calendar check failed: %s", e)
                trading_day_cache[today] = now.weekday() < 5
        is_td = trading_day_cache[today]
        t = now.time()
        market_open = is_td and dt.time(9, 30) <= t < dt.time(16, 0)

        # fixed phases
        fixed: list[tuple[str, str]] = []
        if is_td:
            fixed += [("premarket", x) for x in S.premarket] + [("postmarket", x) for x in S.postmarket]
        elif now.weekday() == S.research_weekday:
            fixed.append(("research", S.research_time))
        catchup = {"premarket": 45, "postmarket": 420, "research": 600}  # minutes after the slot in which a missed phase still runs
        for phase, ts in fixed:
            hh, mm = _hm(ts)
            sched = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            key = f"ran:{today}:{phase}:{ts}"
            if sched <= now < sched + dt.timedelta(minutes=catchup.get(phase, 10)) and not j.get(key):
                j.set(key, now.isoformat(timespec="seconds"))
                _run(phase + ("" if now < sched + dt.timedelta(minutes=10) else ""), None)

        # chores (no LLM)
        try:
            _chores(now, is_td)
        except Exception as e:
            logging.warning("chores failed: %s", e)

        # watcher: runs in its own thread (sessions block this loop for minutes; a trigger touched mid-session must not be missed)
        market_state["open"] = market_open
        if watcher_thread["t"] is None:
            watcher_thread["t"] = threading.Thread(target=_watcher_loop, name="watcher", daemon=True)
            watcher_thread["t"].start()
        while True:
            try:
                kind, text = wq.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                pending.append(text) if text not in pending else None
        poll = W.poll_seconds if market_open else 300
        if last_watch is None or (now - last_watch).total_seconds() >= poll:
            last_watch = now
            events = []
            degraded_now = bool(getattr(ctx.market, "breaker", None) and ctx.market.breaker.open) or bool(ctx.broker.clock().get("degraded"))
            if degraded_now and not degraded["flag"]:
                TG.notify("Alpaca is degraded (clock or data feed errors). Watching on fallback quotes; armed entries will not fire until the broker feed is back. Resting stops live at the broker and keep working.", kind="broker")
            elif degraded["flag"] and not degraded_now:
                TG.notify("Alpaca feed recovered; normal operation resumed.", kind="broker", force=True)
            degraded["flag"] = degraded_now
            pending += [e for e in events if e not in pending]
            try:
                acct = ctx.broker.account()
                j.add_equity(float(acct["equity"]), float(acct["cash"]), market_open)
            except Exception as e:
                logging.warning("equity snapshot failed: %s", e)

        # market hours: continuous evaluation (events preempt, then the agent's own queue, then the rotation)
        if market_open:
            sh, sm = _hm(W.intraday_start)
            eh, em = _hm(W.intraday_end)
            in_window = dt.time(sh, sm) <= t <= dt.time(eh, em)
            since_end = (now - last_session_end).total_seconds() if last_session_end else 1e9
            cooled = since_end >= (W.min_gap_seconds if W.continuous else W.event_cooldown_min * 60)
            due_base = in_window and (W.continuous or last_intraday is None or (now - last_intraday).total_seconds() >= W.intraday_interval_min * 60)
            if in_window and cooled and (pending or due_base):
                if pending:
                    task, phase = "EVENT: " + " | ".join(pending), "event"
                    pending = []
                else:
                    nt = j.next_task()
                    if nt and nt["kind"] == "operator":
                        _run("intraday", _operator_task(nt), task_id=nt["id"])
                        continue
                    if nt and nt["kind"] == "reflect":
                        j.complete_task(nt["id"], "reflect session run")
                        task, phase = f"SELF-ASSIGNED TASK #{nt['id']}: {nt['text']}", "reflect"
                    elif nt:
                        task, phase = f"SELF-ASSIGNED TASK #{nt['id']} (close it with complete_task when done): {nt['text']}", "intraday"
                    else:
                        task, phase = _rotation_task(), "intraday"
                _run(phase, task)

        # off hours: work the queue at any hour; reflect only inside the off-hours windows (bounded per day)
        else:
            since_end = (now - last_session_end).total_seconds() if last_session_end else 1e9
            backoff = min(1800, 300 * failures["n"]) if failures["n"] else 0
            nt0 = j.next_task()
            if nt0 and nt0["kind"] == "operator" and since_end >= max(60, backoff):
                _run("adhoc", _operator_task(nt0), task_id=nt0["id"])
            elif since_end >= max(W.offhours_interval_min * 60, backoff) and (nt0 or _in_offhours(now, is_td) or _in_study(now)):
                nt = j.next_task()
                if nt:
                    if nt["kind"] == "reflect":
                        j.complete_task(nt["id"], "reflect session run")
                        _run("reflect", f"SELF-ASSIGNED TASK #{nt['id']}: {nt['text']}")
                    else:
                        _run("research", f"SELF-ASSIGNED TASK #{nt['id']} (close it with complete_task when done): {nt['text']}")
                elif _in_study(now) and _study_done_tonight() < W.study_per_night:
                    from .knowledge import KnowledgeBase
                    picks = KnowledgeBase().next_topics(3)
                    _run("study", "STUDY: pick ONE topic — " + "; ".join(f"{p['title']} [{p['slug']}; {p['why']}; last studied {p['last'][:10]}]" for p in picks)
                         + " — or a gap you noticed in recent sessions. Budget: at most 6 searches and 4 page fetches. Finish with save_knowledge.")
                elif _lab_done_tonight() < SETTINGS.lab.lab_sessions_per_night and _lab_task():
                    _run("lab", _lab_task())
                elif _in_offhours(now, is_td) and j.sessions_today("reflect") < W.max_reflect_per_day:
                    _run("reflect", None)
                else:
                    last_session_end = now  # nothing to do; check again after the interval
        if once or stop_requested["flag"]:
            console.print("daemon exiting cleanly")
            return
        time.sleep(5)


@app.command()
def watch(once: bool = typer.Option(True, help="single tick")) -> None:
    """Run one watcher tick: sync fills, arm missing stops, report would-be wake events."""
    _setup_logging(False)
    from .agent import make_context
    from .exits import ExitManager
    from .watch import Watcher
    ctx = make_context()
    exits = ExitManager(ctx.broker, ctx.market, ctx.journal)
    w = Watcher(ctx.broker, ctx.market, ctx.journal, exits, SETTINGS.watch)
    clk = ctx.broker.clock()
    events, actions = w.tick(bool(clk["is_open"]))
    console.print(Panel("\n".join(actions) or "(none)", title="exit housekeeping"))
    console.print(Panel("\n".join(events) or "(none)", title="wake events"))


@app.command()
def exits(symbol: str = typer.Argument(None), stop: float = typer.Option(None), target: float = typer.Option(None)) -> None:
    """Show registered exit levels and resting stops; optionally set levels for a symbol (operator override)."""
    from .agent import make_context
    from .exits import ExitManager
    ctx = make_context()
    X = ExitManager(ctx.broker, ctx.market, ctx.journal)
    if symbol and (stop is not None or target is not None):
        ok, msg = X.update(symbol.upper(), stop, target, allow_lower_stop=True)
        console.print(msg)
        if ok and stop is not None:
            X.cancel_exit_orders(symbol.upper())
        console.print("\n".join(X.ensure()) or "(no order changes)")
    oo = ctx.broker.open_orders()
    for p in ctx.broker.positions():
        console.print(p["symbol"], X.describe(p["symbol"], oo))


@app.command()
def journal(what: str = typer.Argument("sessions", help="sessions | decisions | trades | lessons | notes"),
            limit: int = typer.Option(15)) -> None:
    """Print journal contents."""
    from .agent import make_context
    from .tools import ToolRegistry
    ctx = make_context()
    console.print(ToolRegistry(ctx).call("get_journal", {"what": what, "limit": limit}))


@app.command()
def lessons(delete: int = typer.Option(None, help="lesson id to delete"), add: str = typer.Option(None, help="add an operator lesson")) -> None:
    """List, delete, or add lessons (they are injected into every session prompt)."""
    from .journal import Journal
    j = Journal()
    if delete is not None:
        j.conn.execute("DELETE FROM lessons WHERE id=?", (delete,)); j.conn.commit(); console.print(f"deleted {delete}")
    if add:
        console.print(f"added {j.add_lesson(add, source='operator', weight=2.0)}")
    for l in j.lessons(50):
        console.print(f"[dim]{l['id']:>3} {l['ts'][:10]} w={l['weight']}[/dim] {l['text']}")


@app.command()
def playbook() -> None:
    """Print the current playbook."""
    from .journal import Journal
    console.print(Journal().playbook())


@app.command()
def plan() -> None:
    """Print the current plan for the next session."""
    from .journal import Journal
    j = Journal()
    console.print(f"[dim]{j.get('plan_ts')}[/dim]\n{j.get_plan() or '(no plan yet)'}")


@app.command()
def telegram() -> None:
    """Long-poll Telegram for operator commands (run as the munchkin-telegram service). Needs TELEGRAM_BOT_TOKEN and a paired chat."""
    from .agent import make_context
    from .telegram import Bot, configured
    ctx = make_context()
    console.print("telegram poller " + ("started" if configured() else "waiting for TELEGRAM_BOT_TOKEN (set it on the Config tab)"))
    Bot(ctx.journal, ctx.broker, ctx.risk).poll_forever()


@app.command()
def notify(text: str, kind: str = typer.Option("fills", help="fills | errors | broker | sessions | events")) -> None:
    """Send a test message to the paired Telegram chats."""
    from .telegram import notify as _notify
    console.print("sent" if _notify(text, kind=kind, force=True) else "[red]not sent: token missing, no paired chat, or API error[/red]")


@app.command()
def lab(show: Optional[int] = typer.Option(None, help="print one hypothesis with its tests"),
        status: Optional[str] = typer.Option(None, help="filter the list by status"),
        reject: Optional[int] = typer.Option(None, help="reject a hypothesis (operator)"),
        reopen: Optional[int] = typer.Option(None, help="move a rejected/retired hypothesis back to proposed")) -> None:
    """The hypothesis ledger: list, show, reject, reopen. Promotion and demotion are operator decisions (dashboard / Telegram)."""
    from .journal import Journal
    from .lab import Lab
    lab = Lab(Journal())
    if reject:
        console.print("rejected" if lab.set_status(reject, "rejected", "operator (cli)") else "[red]no such hypothesis[/red]")
    if reopen:
        console.print("reopened" if lab.set_status(reopen, "proposed", "operator (cli)") else "[red]no such hypothesis[/red]")
    if show:
        console.print(Panel(lab.describe(show), title=f"hypothesis #{show}"))
        return
    console.print(lab.index_text(60) if not status else "\n".join(f"- #{h['id']} [{h['status']}] {h['title']}" for h in lab.list(status, 100)) or "(none)")


@app.command()
def skills(approve: Optional[str] = typer.Option(None, help="approve an agent-written draft by name"),
           enable: Optional[str] = typer.Option(None, help="enable a skill by name"),
           disable: Optional[str] = typer.Option(None, help="disable a skill by name"),
           show: Optional[str] = typer.Option(None, help="print a skill's full procedure")) -> None:
    """List installed skills (skills/ in the repo, data/skills/ for agent drafts) and manage them."""
    from .skills import SkillStore
    st = SkillStore()
    if approve:
        console.print("approved" if st.approve(approve) else f"[red]cannot approve '{approve}' (only agent drafts need approval)[/red]")
    if enable:
        console.print("enabled" if st.set_enabled(enable, True) else f"[red]no skill '{enable}'[/red]")
    if disable:
        console.print("disabled" if st.set_enabled(disable, False) else f"[red]no skill '{disable}'[/red]")
    if show:
        sk = st.get(show)
        console.print(Panel(sk.body, title=f"{sk.name} — {sk.description}") if sk else f"[red]no skill '{show}'[/red]")
        return
    rows = st.all()
    if not rows:
        console.print("no skills installed (add folders under skills/)")
    for sk in rows:
        flag = "[green]active[/green]" if sk.status == "active" else "[yellow]draft[/yellow]"
        if not sk.enabled:
            flag += " [red]disabled[/red]"
        extra = (f"  scripts: {', '.join(sk.scripts)}" if sk.scripts else "") + (f"  resources: {', '.join(sk.resources)}" if sk.resources else "")
        console.print(f"[bold]{sk.name}[/bold] ({sk.source}) {flag}  {sk.description}{extra}")
    for e in st.errors:
        console.print(f"[red]invalid: {e}[/red]")


@app.command()
def halt(resume: bool = typer.Option(False, help="remove the halt")) -> None:
    """Block all new entries (exits still allowed) until resumed."""
    if resume:
        HALT_FILE.unlink(missing_ok=True)
        console.print("halt removed")
    else:
        HALT_FILE.write_text(dt.datetime.now().isoformat())
        console.print(f"HALT set ({HALT_FILE}); new entries blocked")


@app.command()
def baseline(reset: bool = typer.Option(False)) -> None:
    """Show or reset the virtual-account baseline (starting capital offset)."""
    from .agent import make_context
    ctx = make_context()
    if reset:
        console.print(ctx.risk.reset_baseline())
    else:
        console.print(ctx.journal.get("baseline"))


@app.command("test-llm")
def test_llm() -> None:
    """Quick LLM connectivity/tool-call check."""
    from .llm import LLMClient
    llm = LLMClient()
    console.print(llm.probe())


@app.command()
def web(host: str = typer.Option("0.0.0.0"), port: int = typer.Option(8787)) -> None:
    """Serve the mobile dashboard (also run as the munchkin-web systemd unit)."""
    import uvicorn
    uvicorn.run("munchkin.web:app", host=host, port=port, log_level="warning")


@screener_app.command("refresh")
def screener_refresh() -> None:
    """Rebuild the indicator table (about 10-30s)."""
    from . import screener as scr
    from .market import Market
    t = scr.refresh(Market())
    console.print(f"{len(t)} symbols")


@screener_app.command("backtest")
def screener_backtest(years: int = typer.Option(3)) -> None:
    """Backtest the preset setups over N years of daily bars (about a minute)."""
    from . import analytics as A
    from . import screener as scr
    from .market import Market
    st = scr.backtest(Market(), years=years)
    base = st.get("baseline_all_days")
    console.print(f"{st['symbols']} symbols, {years}y; baseline {base}")
    for name, s_ in st["setups"].items():
        console.print(A.stats_line(name, s_, base))


@screener_app.command("earnings")
def screener_earnings(force: bool = typer.Option(False)) -> None:
    """Refresh the earnings-date cache for the universe (yfinance, ~1-2 min)."""
    from . import screener as scr
    d = scr.refresh_earnings(scr.build_universe(), force=force)
    console.print(f"{len(d)} earnings dates cached")


@screener_app.command("regime")
def screener_regime() -> None:
    """Print the regime score, breadth, and sector table."""
    from . import screener as scr
    from .journal import Journal
    from .market import Market
    console.print(scr.regime_report(Market(), Journal()))


@screener_app.command("intraday")
def screener_intraday(expr: str = typer.Argument(""), sort: str = typer.Option(""), asc: bool = typer.Option(False),
                      limit: int = typer.Option(20), setups: bool = typer.Option(False)) -> None:
    """Live intraday screener (market hours)."""
    from . import screener as scr
    from .market import Market
    m = Market()
    console.print(scr.intraday_setups(m) if setups else scr.intraday_query(expr, sort or None, asc, limit, m))


@screener_app.command("query")
def screener_query(expr: str = typer.Argument(""), sort: str = typer.Option(""), asc: bool = typer.Option(False),
                   limit: int = typer.Option(20)) -> None:
    """Query the table, e.g. 'rsi14 < 30 and avg_dollar_vol20_m > 50'."""
    from . import screener as scr
    console.print(scr.query(expr, sort or None, asc, limit))


if __name__ == "__main__":
    app()
