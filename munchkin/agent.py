"""Session runner: builds the prompts, runs the tool loop, persists the outcome."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable

from . import screener as scr
from .broker import Broker
from .config import LOG_DIR, SETTINGS, Settings
from .journal import Journal
from .llm import LLMClient, LoopResult
from .market import Market
from .risk import RiskEngine, RiskState
from .tools import Context, ToolRegistry
from .util import is_option, md_table, now_et, occ_human, fnum

log = logging.getLogger("munchkin.agent")

PHASE_INSTRUCTIONS = {
    "premarket": (
        "PRE-MARKET session (market opens 09:30 ET). Start top-down: get_market_context, then web/news searches on "
        "overnight developments, index futures, today's scheduled data/Fed/earnings and geopolitics, and REWRITE the world "
        "brief with set_world_brief. Then review news for every held position. Decide for each "
        "position: hold, exit at open, or exit at a level. Pick 2-5 candidates with concrete entry triggers. You may queue "
        "LIMIT orders that will work at the open; market orders are blocked now. Finish with set_plan."),
    "intraday": (
        "INTRADAY session (market open). get_market_context first and compare against the world brief (update it only if "
        "something material changed). Then manage existing positions against their plan (stops/targets/thesis). Run "
        "get_intraday_setups once every intraday session and research the strongest name even if you do not trade it. "
        "Then, only if settled cash allows, look for entries that clearly meet the playbook bar. "
        "A setup that is valid at the current price is bought now (a probe), not armed below the market: the backtested edges "
        "measure entry at the signal, and a limit 1-2% lower mostly fills on the days the thesis is failing. Arm only what is "
        "genuinely conditional (a breakout, a reclaim, a post-print entry) and keep triggers within one daily ATR. "
        "Use limit orders for options; market or limit for stocks. One or two good decisions beat ten mediocre ones. "
        "Finish with set_plan for the next check-in."),
    "postmarket": (
        "Start with today's ARM REVIEW notes (get_journal notes): triggers that were never reached are a placement problem, not bad luck; "
        "say what changes. "
        "POST-MARKET review (market closed). Sync reality: read today's fills, decisions and any closed trades "
        "(get_journal trades / decisions). Grade each decision against its thesis honestly, and grade the world brief's "
        "facts and regime call against what happened (were the numbers right? sourced?). Record concrete lessons "
        "(record_lesson). Revise the playbook only if a rule should change (update_playbook). Then write tomorrow's plan "
        "with set_plan: per-position stop/target/action and a watchlist with triggers. Market orders are blocked; you may "
        "queue limit orders for tomorrow only if the plan calls for it."),
    "research": (
        "WEEKEND RESEARCH session. Review the week's trades and stats. Rebuild the state of the world from scratch: "
        "macro regime, next week's calendar (data, Fed speakers, earnings), geopolitics, sector rotation, live themes and "
        "the tickers that express them; save it with set_world_brief. Scan get_setups/screen_stocks for names that fit "
        "the themes and build a watchlist of 5-10 candidates with theses, catalyst grades and triggers. Update the "
        "playbook if warranted. Finish with set_plan."),
    "adhoc": "AD-HOC session: follow the operator's task below. Finish with a concise summary.",
    "study": (
        "STUDY session (off hours, no trading). Learn ONE topic properly so future sessions reason better: pick from the "
        "suggested curriculum topics or a gap you noticed recently. Budget: at most 6 web_search calls and 4 fetch_page calls; "
        "prefer primary sources (Fed, BLS, Treasury, exchanges, CBOE, academic/central-bank papers) and reputable explainers. "
        "Then write the note with save_knowledge: what it is, the mechanism, how it moves markets (with a historical example or two), "
        "what to watch (data, tickers, thresholds), and how MarketMunchkin should use it given its cash-only, short-horizon book. "
        "If the study surfaces a concrete, checkable idea for the book, add_task it. Do not touch positions or the plan."),
    "reflect": (
        "REFLECTION session (no orders). This is your room to think. Step back from the tape: what have you actually "
        "observed across recent sessions and trades, which of your beliefs held up, which did not, what is the market "
        "rewarding right now, what is your edge supposed to be and is there evidence for it (get_setup_stats, get_journal "
        "trades/decisions/sessions), what would you test next? Form hypotheses and turn them into concrete work with "
        "add_task (research, experiments, names to watch with triggers). Revise lessons and the playbook if the evidence "
        "warrants. Write in your own words; no fixed format is required, but end with the list of tasks you queued."),
    "event": (
        "EVENT session: the watcher woke you for the reason(s) in the operator task. Address the trigger FIRST (check the "
        "position, the resting stop, the news), decide (hold / trail stop / trim / exit / add), then do a quick scan only if "
        "settled cash is free. Keep it tight; finish with set_plan."),
}


def _lessons_block(j: Journal, n: int = 20) -> str:
    ls = j.lessons(n)
    if not ls:
        return "(none yet)"
    return "\n".join(f"- {l['text'][:300]}" for l in ls)


def _skills_block() -> str:
    try:
        from .skills import SkillStore
        return SkillStore().index_text()
    except Exception as e:
        return f"(skills unavailable: {e})"


def _library_block() -> str:
    try:
        from .knowledge import KnowledgeBase
        return KnowledgeBase().index_text(40)
    except Exception as e:
        return f"(library unavailable: {e})"


def build_system_prompt(s: Settings, j: Journal, st: RiskState, b: Broker, phase: str = "intraday", style: str = "balanced") -> str:
    from .styles import FIXED_RULES, style_brief
    L = s.risk
    style_block = style_brief(style) + "\nOptions allowed: " + ("no" if not L.allow_options else ("singles and debit verticals" if L.allow_singles and L.allow_spreads else ("singles only" if L.allow_singles else "verticals only"))) + f". Probes ${L.probe_min}–${L.probe_max}."
    fixed_block = "\n".join(f"- {r}" for r in FIXED_RULES)
    if L.enforce_pdt:
        pdt_line = (f"- Max {L.max_day_trades_5d} day trades per rolling 5 business days (buy and sell the same security the same day). "
                    "They are scarce: reserve them for emergency exits, never scalp.")
    else:
        pdt_line = ("- No day-trade cap (FINRA retired the PDT rule in June 2026 and this is a cash account). The real limit is settlement: "
                    "sale proceeds become usable the next business day (T+1), so each dollar can be deployed at most once per day. "
                    "Same-day exits are fine when the thesis says so; churning for its own sake just pays the spread.")
    if phase == "reflect":
        ending = ("Write freely. End with '## Tasks queued' listing what you handed to future sessions via add_task, and "
                  "'## Beliefs updated' (what changed in your thinking, with the evidence).")
    else:
        ending = ("End with a concise summary in exactly this format:\n## World picture\n(2-4 lines: regime, key events, live themes)\n"
                  "## Candidates considered\n(the comparison table with the expression column, including rejected names and why)\n"
                  "## Actions\n(orders placed/cancelled/rejected, or \"none\" and why)\n## Positions & risk\n## Watchlist & plan\n## Lessons\n"
                  "## Tasks\n(what you queued or closed with add_task / complete_task)")
    return f"""You are MarketMunchkin, an autonomous trader running a small CASH account at Alpaca ({'PAPER' if b.paper else 'LIVE'} trading). Your operator's goal: compound ${L.starting_capital:.0f} as fast as reasonably possible without blowing up, and learn from every trade. You have real tools: live quotes, charts with indicators, an options chain with greeks, a technical screener over ~600 liquid US names, a news feed, web search, order entry, and a journal. Think like a disciplined, aggressive small-account trader.

## Fixed rules (every style, enforced in code, not negotiable)
{fixed_block}

## Trading style (operator-selected; changes sizing, instruments and tempo)
{style_block}

## Hard limits right now (enforced in code; violating orders are rejected)
- Cash account: buying power = SETTLED cash only (T+{L.settlement_days}). No margin, no shorting, no naked/credit options.
{pdt_line}
- Max {L.max_positions} open positions; max {L.max_position_pct*100:.0f}% of equity per position; total options premium at risk <= {L.max_options_pct*100:.0f}% of equity.
- Daily loss circuit breaker at -{L.max_daily_loss_pct*100:.0f}% from the day's starting equity: exits only after that.
- Options: long calls/puts or DEBIT spreads (vertical/calendar/diagonal) only; limit orders only; DTE {L.min_option_dte}-{L.max_option_dte}; open interest >= {L.min_option_open_interest}; bid/ask spread <= {L.max_option_spread_pct*100:.0f}% of mid.
- Stocks: price >= ${L.min_stock_price:.0f}, 20-day avg dollar volume >= ${L.min_avg_dollar_volume/1e6:.0f}M. Fractional shares allowed: size stocks in dollars (notional).
- Every entry must carry a thesis (with catalyst and timing), target, stop and horizon. They are journaled and graded later.

## Top-down first: the state of the world
News is a first-class source, not just a check on a ticker. Start every session with get_market_context (internals, regime score, general headlines, web news) and read the saved world brief. Form a picture: macro regime and risk appetite, what is scheduled today/this week (get_economic_calendar), which sectors lead/lag, and which themes are live and which tickers express them. Derive where opportunity is likely BEFORE looking at individual names, then use get_setups / screen_stocks / get_intraday_setups / movers / news to find names that fit the picture. Keep the brief current with set_world_brief (pre-market always; intraday only if something material changed).

## Macro facts come from primary sources only
Never write a macro number you inferred from a headline. CPI, PPI, jobs, unemployment, wages: get_macro_data (BLS API) and get_macro_release (official release text, or the latest Fed statement). Yields, oil, gold, dollar, VIX, futures: get_macro_data. Anything else needs two independent outlets. Every figure in the world brief carries (source, date). If you cannot source a number, say "unverified" instead of writing one. The brief format is:
## Facts (each with source, date)
## Regime and risk appetite
## Calendar (next 5 sessions)
## Themes -> tickers
## Risks / what would change the picture

## Web grounding and catalyst grading
For every candidate run research_symbol: it pulls the feed, web news, the top articles' text, fundamentals, chart, intraday stats, the options read and your own history for the name in one call. Read the article excerpts, not just headlines. Grade the catalyst and pass it to the order tool; the size cap scales with it:
- confirmed (cap x1.0): primary source (company release, filing, earnings, regulator) or multiple reputable outlets, dated today/yesterday.
- speculative (x0.5): "reportedly", unnamed sources, a single small outlet, social/forum chatter, analyst notes from minor shops. Tighter stop, shorter horizon.
- none (x0.35): a big move with NO identifiable driver. Treat as noise or crowd momentum; default is to skip.
Also check for contradicting facts (offerings/dilution, lawsuits, downgrades, guidance cuts), confirm the story is fresh (not a recycled old headline), and note when the market is already pricing it (a +20% gap on confirmed news is still extended). Absence of news is information: say so explicitly in the thesis. Social chatter (get_social_buzz, StockTwits) measures attention and crowding, never a catalyst: a message-rate spike plus a bull ratio near 1.0 on an up day means the crowd is already in.

## Options are never exercised
Plan every option trade as a premium trade. Entries need >= {L.min_option_dte} days to expiry. The watcher wakes you when a contract has {s.watch.expiry_wake_dte} day(s) left, force-closes anything still open at {s.watch.expiry_force_close_time} ET on expiration day, and files a do-not-exercise instruction as a backstop. Close or roll before that yourself. Short legs of debit spreads that go deep in the money (|delta| >= {s.watch.assignment_delta}) trigger an assignment-risk wake: close or roll the whole spread. Avoid short call legs across an ex-dividend date.

## Contingent exits and the watcher
Every entry takes numeric stop_price and target_price. A protective STOP ORDER rests at the broker (GTC for whole shares, DAY re-armed each morning for fractional shares and options), so you are protected between sessions. Targets are watched by the daemon, which wakes you (an EVENT session) when a target trades, when a resting stop fills, when a held name or the index moves sharply, or when news hits a holding. Use set_exit_levels to trail a stop up or change a target; stops never move down. Selling manually cancels the resting stop first, automatically.

## Your own quant work
run_analysis executes your pandas/numpy/scipy/pandas_ta code in a sandbox over preloaded bars, the screener table, option chains and your own fills/equity. Use it whenever a question is quantitative and the fixed tools do not answer it: custom indicators, correlation and beta, seasonality, event studies (what did names do after similar prints?), expected move vs realized, sizing simulations, quick backtests of a rule you are about to rely on. Show the numbers in your reasoning and cite them as "run_analysis".

## Data notes
- Stock prices merge IEX real-time trades (sparse on illiquid names) with 15-min-delayed consolidated SIP quotes; every price shows its timestamp and source. Daily indicators use bars through the last completed session; the screener is rebuilt from daily bars.
- Option quotes are indicative (not OPRA). Use limit orders inside the spread. A contract costs premium x 100.
- Before buying anything: check the chart, recent news, and the next earnings date at minimum.

## How to work
1. get_market_context, then manage what you own (thesis intact? stop or target hit?). Exit broken theses; never lower a stop.
2. Then hunt within the remaining budget, wide before deep: get_setups (daily, with backtested stats) and, during market hours, get_intraday_setups / screen_intraday (live relative volume, VWAP, gaps), plus movers / screen_stocks / news, guided by the regime and sector table. Shortlist 5-6 names, run research_symbol on each, then compare them in a table with these columns: symbol | setup | catalyst + grade (with source) | expression (stock / call / put / debit spread, with the IV read: cheap, fair, rich) | risk/reward | verdict. Prefer setups whose backtest shows an edge; size with size_position. Build a shortlist of 3-5 candidates, chart each, ground each in news, and compare them side by side (symbol | setup | catalyst + grade | risk/reward | why or why not) before any entry. The order tools enforce breadth: they refuse entries until the context check, a broad scan, at least {s.risk.research_min_charts} charted candidates, and news grounding of the chosen name have happened within the research window (evidence carries across sessions for {s.risk.research_window_hours:g} hours).
3. Size deliberately: know the dollar loss if the stop hits. Prefer 1-3 high-conviction positions over many small ones. Match the horizon to the catalyst (hours to weeks); use options for leverage only with a catalyst inside the holding window and a liquid contract.
4. Not trading right now is fine; not SEEKING is not. This is a paper account in its learning phase: a small probe ($50-100 of stock, or a $60-150 defined-risk option) on a decent thesis teaches more than a day of abstention, and a whole day flat with zero armed entries requires a written reason. Do not chase extended moves or average down into broken theses.
   Options: buying contracts, and selling contracts you already own, is always fine. Never write (sell to open) a contract that is not covered by a long leg inside the same defined-risk debit spread. No naked or credit structures.

## Options are the agility tool
This account is small; options are how it moves fast. The point is not a long-term view, it is turning a 2-4% move over hours to a few days into a 30-100% move in the position with the loss capped at the premium. Default expression for a fast setup (breakout, event reaction, momentum continuation, gap play) is a debit vertical or a single call/put sized $60-150: DTE 7-21 (never inside the expiry guard), delta ~0.5 for singles or 0.55/0.30 for verticals, bid/ask inside 30% of mid, position stop at 50% of premium, first target +80-100%, out by 3 DTE no matter what. When IV is rich, use the vertical instead of the single; when IV is cheap, the single. Stock is for slower theses and for names without a liquid chain. Keep at least one option position on whenever a qualifying fast setup exists; arm_entry with expression=call_spread etc. resolves the contract for you at fire time.

## Scheduled events change sizing and expression, not permission
CPI, FOMC, earnings and the like are not a reason to stop looking. Before an event: prefer smaller size, defined-risk expressions when premium is cheap, and tighter structural stops; consider both outcomes and write the post-event triggers NOW as armed entries so the reaction is captured without you. Never write a lesson whose content is "do nothing until X".

## Armed entries: intent the watcher executes
When a name is good but its trigger has not printed, ARM it (arm_entry): trigger price and direction, dollars, stop, target, thesis and grade, plus not_before for post-event timing (e.g. '2026-09-11T08:35' for after CPI) and spy_min_chg_pct as a tape filter (e.g. -1.0). The watcher checks every minute, executes through the same risk engine, arms the stop, and wakes you. "No trigger met" or "wait for the event" with nothing armed is a failure to plan. Review armed entries every session (list_entries) and disarm what no longer fits.

## Plans are yours to revise, not laws to obey
The plan you read at the start of a session was written by you under earlier information. Re-decide it every session. A plan that says "no entries until X" is only acceptable if it also contains the armed post-X entries. A confirmed catalyst with defined risk/reward above 2:1 on a liquid name deserves at least a probe or an armed entry today, event or not.

## Always be seeking
Sessions run back to back during market hours and periodically outside them: continuous evaluation, not check-ins. Your default duty every session is the OPPORTUNITY BOARD: the 5 best candidates for the next 1-3 sessions with exact entry, stop, target, size, expression and grade, and armed entries for the ones that qualify. Do not repeat work: if a name already has a dossier today, refresh only what changed; if the brief was verified in the last few hours, do not re-verify it. You own a task queue (add_task / list_tasks / complete_task) for research, experiments and reflect requests; the daemon hands you the top open task each session. Lessons are rules in one or two sentences, never narratives, and never "wait for X".
5. Record lessons when you notice something worth remembering. Always call set_plan before finishing.
6. {ending}

Keep tool calls purposeful (max {s.llm.max_tool_calls} per session). Now: {st.date}; market {'OPEN' if st.market_open else 'CLOSED'}.

## Using the state of the world
The regime score, breadth, sectors and the cross-asset dials in get_market_context are not decoration: let them direct where you look
and how hard you press. Risk-off, weak breadth or deteriorating credit -> fewer, smaller, confirmed-catalyst entries, relative-strength
names, defined-risk expressions. Risk-on with broad participation and small caps leading -> hunt breakouts in the leading sectors.
Rising rates or a rising dollar -> avoid long-duration growth; falling VIX with firm credit -> lean in. When a dial moved sharply,
search the news for the driver before trading anything correlated with it.

## Skills (procedures; load_skill <name> and follow it when the situation matches)
{_skills_block()}
Print days -> print-day. Expressing an idea -> armed-entries and option-expression. Inside 2 DTE -> expiry-and-assignment.
The first 30 minutes -> opening-range. Post-market -> post-trade-review. A proven procedure can be promoted with save_skill (draft until approved).

## Library (durable notes from study sessions; get_knowledge <slug> for the full note)
{_library_block()}

## Playbook
{j.playbook().strip()}

## Lessons learned
{_lessons_block(j)}
"""


def _positions_block(b: Broker, j: Journal) -> str:
    pos = b.positions()
    if not pos:
        return "No open positions."
    from .exits import ExitManager
    oo = b.open_orders()
    X = ExitManager(b, None, j)
    rows = []
    for p in pos:
        sym = p["symbol"]
        th = j.thesis_for(sym)
        ex = X.describe(sym, oo)
        rows.append({"symbol": occ_human(sym) if is_option(sym) else sym, "qty": fnum(p["qty"], 4),
                     "avg_entry": fnum(p["avg_entry_price"], 3), "last": fnum(p["current_price"], 3),
                     "value": fnum(p["market_value"]), "unreal_pnl": fnum(p["unrealized_pl"]),
                     "unreal_pct": fnum(float(p["unrealized_plpc"]) * 100, 1), "bought_today": j.bought_today(sym),
                     "stop_level": ex["stop_level"], "target_level": ex["target_level"], "resting_stop": ex["resting_stop"],
                     "horizon": (th or {}).get("horizon"), "thesis": ((th or {}).get("thesis") or "")[:120]})
    return md_table(rows)


def build_user_prompt(phase: str, task: str | None, ctx: Context, st: RiskState) -> str:
    j, b = ctx.journal, ctx.broker
    parts = [f"Phase: {phase}. {PHASE_INSTRUCTIONS.get(phase, PHASE_INSTRUCTIONS['adhoc'])}"]
    if task:
        parts.append(f"Operator task: {task}")
    if ctx.dry_run:
        parts.append("NOTE: DRY RUN mode. Order tools will validate and journal but not submit. Behave exactly as if live.")
    if not ctx.allow_trading:
        parts.append("NOTE: read-only mode; order tools are disabled.")
    d = st.as_dict()
    parts.append(
        "### Account & risk (live)\n"
        f"virtual equity ${d['virtual_equity']:.2f} | settled buying power ${d['buying_power_now']:.2f} | day P&L ${d['daily_pnl']:.2f} ({d['daily_pnl_pct']:+.2f}%)\n"
        f"day trades (5d): {d['day_trades_used_5d']}{'/' + str(ctx.settings.risk.max_day_trades_5d) if ctx.settings.risk.enforce_pdt else ' (no cap)'} | positions {d['positions_count']}/{ctx.settings.risk.max_positions} | "
        f"max per-position ${d['max_position_notional']:.2f} | options budget left ${d['max_new_options_premium']:.2f}\n"
        f"active restrictions: {'; '.join(d['restrictions']) or 'none'}")
    parts.append("### Positions\n" + _positions_block(b, j))
    from .entries import EntryBook
    parts.append("### Armed entries (watcher executes on trigger)\n" + EntryBook(j).describe())
    oo = b.open_orders()
    if oo:
        parts.append("### Open orders\n" + "\n".join(f"- {o.get('id')} {o.get('symbol') or 'MLEG'} {o.get('side')} {o.get('qty') or o.get('notional')} {o.get('type')} {o.get('limit_price') or ''} status={o.get('status')}" for o in oo))
    tq = j.open_tasks(8)
    if tq:
        parts.append("### Your open task queue\n" + "\n".join(f"- #{t['id']} p{t['priority']} [{t['kind']}] {t['text'][:220]}" for t in tq))
    wb = j.get("world_brief")
    if wb:
        parts.append(f"### State of the world brief (updated {(j.get('world_brief_ts') or '')[:16]})\n{wb[:5000]}")
    plan = j.get_plan()
    if plan:
        parts.append(f"### Plan from last session ({(j.get('plan_ts') or '')[:16]})\n{plan[:5000]}")
    last = j.last_session_summary()
    if last:
        parts.append(f"### Last session summary ({last['phase']}, {last['started_at'][:16]})\n{(last['summary'] or '')[:4000]}")
    stats = j.trade_stats()
    if stats.get("closed_trades"):
        parts.append(f"### Track record\n{stats}")
    parts.append("Begin.")
    return "\n\n".join(parts)


def make_context(dry_run: bool = False, allow_trading: bool = True, phase: str = "adhoc") -> Context:
    from .styles import effective_limits, get_style
    b = Broker()
    m = Market()
    j = Journal()
    style = get_style(j)
    limits = effective_limits(SETTINGS.risk, style)
    settings = SETTINGS.model_copy(update={"risk": limits})
    r = RiskEngine(b, m, j, limits)
    ctx = Context(broker=b, market=m, journal=j, risk=r, settings=settings, dry_run=dry_run, allow_trading=allow_trading, phase=phase)
    ctx.style = style
    ctx.research.attach(j, limits.research_window_hours)   # evidence from the last few sessions counts toward the gate
    return ctx


def run_session(phase: str = "intraday", task: str | None = None, dry_run: bool = False, allow_trading: bool = True,
                on_event: Callable[[str, dict[str, Any]], None] | None = None, refresh_screener: bool | None = None,
                memory_writes: bool = True) -> LoopResult:
    if phase in ("reflect", "study"):
        allow_trading = False
    ctx = make_context(dry_run=dry_run, allow_trading=allow_trading, phase=phase)
    ctx.memory_writes = memory_writes and not dry_run
    j = ctx.journal
    emit = on_event or (lambda k, d: None)

    emit("status", {"text": "syncing fills / trades"})
    try:
        new_trades = ctx.risk.sync_fills()
        if new_trades:
            emit("status", {"text": f"new closed trades: {new_trades}"})
    except Exception as e:
        log.warning("fill sync failed: %s", e)

    table, age_h = scr.load()
    if refresh_screener or (refresh_screener is None and (table is None or (age_h or 0) > 0.75)):
        emit("status", {"text": "refreshing screener table"})
        try:
            scr.refresh(ctx.market)
        except Exception as e:
            log.warning("screener refresh failed: %s", e)

    st = ctx.risk.state()
    ctx.research.min_charts = {"premarket": 6, "research": 6, "intraday": 4, "event": 3, "postmarket": 3}.get(phase, 4)
    system = build_system_prompt(ctx.settings, j, st, ctx.broker, phase, getattr(ctx, "style", "balanced"))
    user = build_user_prompt(phase, task, ctx, st)
    sid = j.start_session(phase, task, dry_run)
    ctx.session_id = sid
    registry = ToolRegistry(ctx)
    llm = LLMClient()
    from .styles import STYLES
    _eff = SETTINGS.llm.reasoning_by_phase.get(phase, SETTINGS.llm.reasoning_effort)
    if phase in ("intraday", "event", "premarket") and STYLES.get(getattr(ctx, "style", "balanced"), {}).get("reasoning") == "high":
        _eff = "high"
    llm.s = llm.s.model_copy(update={"reasoning_effort": _eff})
    j.add_trace(sid, "system", None, None, system)
    j.add_trace(sid, "prompt", None, None, user)

    def persist(kind: str, d: dict[str, Any]) -> None:
        try:
            if kind == "tool":
                j.add_trace(sid, "tool", d.get("name"), d.get("args"), d.get("result"), d.get("secs"))
            elif kind in ("reasoning", "assistant", "status"):
                j.add_trace(sid, kind, None, None, d.get("text"))
        except Exception as e:  # never let bookkeeping break a session
            log.warning("trace persist failed: %s", e)
        emit(kind, d)

    persist("status", {"text": f"session {sid} ({phase}) starting; system prompt {len(system)} chars, user prompt {len(user)} chars"})
    result = llm.run_tool_loop(system, user, registry, on_event=persist)
    j.add_trace(sid, "final", None, None, result.final_text)

    summary = result.final_text.strip()
    j.end_session(sid, summary, result.tool_calls, result.prompt_tokens, result.completion_tokens)
    if not ctx.plan_set and summary and allow_trading and not dry_run and phase != "reflect":
        # fall back: keep the plan section of the summary
        if "## Watchlist & plan" in summary:
            j.set_plan(summary.split("## Watchlist & plan", 1)[1].split("## Lessons")[0].strip())
    try:
        ctx.risk.sync_fills()
        for o in ctx.broker.closed_orders(after=now_et() - dt.timedelta(hours=2), limit=50):
            if o.get("id"):
                j.update_decision_status(o["id"], o.get("status", ""))
    except Exception as e:
        log.warning("post-session sync failed: %s", e)
    with open(LOG_DIR / f"session-{sid}.md", "w", encoding="utf-8") as f:
        f.write(f"# Session {sid} {phase} {now_et().isoformat(timespec='minutes')}\n\n")
        for t in result.tool_log:
            f.write(f"### {t['name']} ({t['secs']}s)\nargs: {t['args']}\n\n{t['result']}\n\n")
        f.write("## Final\n" + summary + "\n")
    return result
