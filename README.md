# MarketMunchkin

An autonomous trading agent that runs a local LLM (LM Studio, `qwen/qwen3.8-27b`) against an
Alpaca **paper** account. It researches with live market data, an options chain with greeks,
a technical screener over ~600 liquid US names, Benzinga news and SearXNG web search, places
orders through a hard-coded risk engine, and keeps a journal of theses, fills, closed trades and
lessons that feed back into future sessions.

Constraints baked in: cash-only (settled funds, T+1), no margin, no shorting, no naked options,
max 3 day trades per rolling 5 business days, position/options caps, daily-loss circuit breaker.

## Layout

```
munchkin/
  config.py     settings (munchkin.toml) + secrets (.env, never exposed to the LLM)
  llm.py        LM Studio client: OpenAI-compatible tool loop with context compaction
  search.py     SearXNG search + readable page fetch
  broker.py     Alpaca trading wrapper (orders, positions, fills, option contracts)
  market.py     quotes, bars, option chain/greeks, news, movers, yfinance fundamentals
  indicators.py pandas technical indicators
  screener.py   universe (S&P 500 + Nasdaq-100 + ETFs/momentum names) indicator table + query
  journal.py    SQLite memory: sessions, decisions, fills, round-trip trades, lessons, plan, playbook
  risk.py       hard guardrails (cash-only, PDT, sizing, options rules, HALT)
  tools.py      the tool surface the model sees
  agent.py      prompts + session runner
  cli.py        `munchkin` CLI
data/           munchkin.db, screener cache, playbook.md, HALT file
logs/           munchkin.log, llm_trace.jsonl, session-<id>.md transcripts
```

## Setup

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env   # fill in Alpaca paper keys; chmod 600 .env
.venv/bin/munchkin test-llm
.venv/bin/munchkin status
```

`.env` holds the credentials. The model never gets a file/shell/env tool, every tool result is
scrubbed for the key strings, and `.env` is `chmod 600` + gitignored.

## Usage

```bash
munchkin status                              # account, risk state, positions, plan
munchkin run --phase intraday --dry-run      # full session, orders validated but not sent
munchkin run --phase premarket               # live paper session
munchkin run --phase postmarket              # review fills, grade decisions, write lessons + plan
munchkin chat "Is NVDA a buy into earnings?" # read-only Q&A with all research tools
munchkin daemon                              # scheduler + watcher (munchkin.toml [schedule] and [watch])
munchkin watch                               # one watcher tick: sync fills, arm missing stops, show wake events
munchkin exits [SYMBOL --stop X --target Y]  # show/override registered exit levels and resting stops
munchkin journal trades|decisions|lessons|sessions|notes
munchkin plan | munchkin playbook
munchkin screener refresh
munchkin screener query "rsi14 < 30 and avg_dollar_vol20_m > 50" --sort rsi14 --asc
munchkin halt            # block new entries (exits still allowed);  munchkin halt --resume
munchkin baseline --reset  # re-anchor the virtual $500 account to the current broker balance
```

Run the scheduler as a user service:

```bash
cp scripts/munchkin-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now munchkin-daemon
journalctl --user -u munchkin-daemon -f
```

## Cadence and contingent exits

The daemon runs pre-market (08:45) and post-market (16:15) sessions, an intraday baseline session
every 30 minutes from 09:35 to 15:50, and **event sessions** whenever the watcher (polling every
60 s during market hours) sees: a fill the agent did not place (a resting stop), a registered
target trading, a held name moving more than 3% or SPY/QQQ more than 0.8% since the last session,
or new headlines on a holding. Events queue during an 8-minute cooldown.

Every entry carries numeric `stop_price` / `target_price`. A protective stop order rests at the
broker: GTC for whole-share positions, DAY re-armed each morning for fractional shares and
options (Alpaca allows fractional quantities only as simple DAY orders). Targets are watched, not
rested, because a second sell order would tie up the same shares. `set_exit_levels` trails a stop
(never lower) or moves a target and replaces the resting order; manual sells cancel it first.

## Dashboard

`munchkin web` (installed as the `munchkin-web` user service) serves the dashboard on port 8787:
a desktop-first single-page app (`munchkin/ui/`) that collapses to the phone, with dark, light and
system themes, the mascot, the agent's reasoning drawn as a timeline and a flow, per-position pages
with candle charts carrying entry/stop/trigger/target, option payoff and greeks, a Brain page led by
the state of the world (regime gauge, breadth, sectors, macro tape, BLS prints, calendar), the
trading-style control, and Config for model, providers and appearance. The previous page is at
`/legacy`. It shows: equity curve and P&L, positions with their resting stops and catalyst grades, open
orders, risk state, recent watcher events, every session with its full trace (system/user prompt,
reasoning, each tool call with arguments and results, final summary), the decisions journal
with rejections and their reasons, trades, lessons, the world brief, plan and playbook.

Open `http://<this-machine>:8787` from a phone on the same network (or over a VPN such as
Tailscale; do not port-forward it to the internet). Setting `MUNCHKIN_WEB_TOKEN=...` in `.env`
requires `?token=...` once per browser (a cookie is then set). Traces for sessions that predate
the trace table were imported with `scripts/backfill_traces.py`.

## Session flow

Each session the model receives: the playbook, top lessons, live account/risk state, positions
with their journaled thesis/target/stop, open orders, the plan written last session, and the
last session's summary. It then works the tools (research -> decide -> order -> journal) and
ends with a structured summary. Post-market sessions grade the day's decisions against their
theses, record lessons and rewrite the plan; the playbook is a living document the model can
revise.

## Screening analytics

`munchkin/analytics.py` adds, on top of the per-symbol indicators: cross-sectional momentum
percentile ranks and a composite `mom_score`, relative strength vs SPY and vs the sector ETF
(GICS sectors from the S&P 500 / Nasdaq-100 tables), beta and correlation to SPY, ADX trend
strength, Bollinger squeeze percentile, ATR z-score, days to next earnings (yfinance cache,
weekly), and a 24h headline count. `get_market_context` now includes a transparent regime score
(SPY trend, breadth above 50d and RSI>50, VIX level and VIX/VIX3M term structure), breadth stats
with a stored daily history, and a sector performance table.

During market hours `screen_intraday` / `get_intraday_setups` work from a one-call snapshot of
the whole universe: gap, move from open, range position, VWAP distance, high-of-day distance and
relative volume adjusted for time of day (a cached intraday volume profile), joined with the daily
context. `get_setups` shows each preset with its backtested forward-return statistics
(`munchkin screener backtest`, 3 years of daily bars, overlap-controlled signals).
`get_option_analytics` gives ATM IV by expiry, IV vs 20d realized, the straddle-implied expected
move, 25-delta skew, put/call OI and OI walls, and an IV rank from the stored daily IV history.
`size_position` turns a stop distance into shares under the risk caps.

```bash
munchkin screener refresh | backtest --years 3 | earnings | regime | intraday --setups
```

## Agent-run analysis (sandboxed pandas)

`run_analysis` lets the model write its own pandas / numpy / scipy / pandas_ta code. The parent
process (which holds the credentials) prepares the dataset: bars for the requested symbols with
indicator columns, the screener table, option chains, the agent's fills, trades and equity history.
The code then runs in a separate interpreter (`python -I -S`) with a clean environment, no
credentials, CPU and memory limits, a 40 s timeout, and an AST allowlist (imports limited to
numeric/stdlib modules; `open`, `exec`, `eval`, dunder access and I/O methods rejected).

## Research gate, catalyst grades, state of the world

Entry tools refuse to commit capital until, in the current session, the model has called
`get_market_context`, run a broad scan (`get_setups` or `screen_stocks`), charted at least
`research_min_charts` candidates, and grounded the chosen name in news (`get_news` with the
symbol, or a `web_search` mentioning it). Every entry carries a `catalyst_grade`:

| grade | meaning | size cap multiplier |
|---|---|---|
| confirmed | primary source or several reputable outlets, fresh | 1.0 |
| speculative | rumor, unnamed sources, social, single small outlet | 0.5 |
| none | unexplained move, pure technical | 0.35 |

`get_news` with symbols auto-runs a web search when the feed is empty and flags the move as
unexplained. `get_market_context` returns index/sector/rates/commodity/vol ETF moves, general
headlines and web news on markets and geopolitics; the model keeps a persistent "world brief"
(`set_world_brief`) that is injected into every session so ideas flow top-down from the picture
to the names. Position caps aggregate stock and options per underlying.

## Trading styles and the rules that never change

The style switch in the dashboard is colour-coded: **Defensive blue, Balanced green, Aggressive red**. Fetches that a site blocks are retried through Tavily's extract API automatically when a Tavily key is set.

Three operator-selected styles (dashboard top bar or Config) change the risk envelope, the
instruments, the cadence and the agent's brief. The active style applies from the next session.

| | Defensive | Balanced (default) | Aggressive |
|---|---|---|---|
| instruments | stock only | stock, bought calls/puts, debit verticals | bought calls/puts first for fast setups; verticals when IV is rich |
| probes | $50–75 | $50–100 | $100–150 |
| per position / positions | 25% / 4 | 40% / 5 | 50% / 6 |
| catalyst grades | confirmed only | speculative ×0.5, none ×0.35 | speculative ×0.75, none ×0.5 |
| options budget | 0% | 60% of equity | 75% of equity |
| daily loss breaker | −8% | −15% | −20% |
| cadence / reasoning | 3 min gap, high | 60 s gap, medium | 60 s gap, events preempt, medium |

Rules that hold in every style and cannot be overridden from the dashboard, the prompt, or the
model (they live in `munchkin/styles.py` and the risk engine):

- Cash account only: buys use settled cash, never margin, never unsettled proceeds.
- No shorting stock.
- Never writes contracts: no naked or credit options. A short leg exists only as the covered leg
  of a debit vertical opened in the same order, and only where the style allows spreads.
- No option is held into expiration (wake at 1 DTE, forced close 14:30 ET on expiry day, DNE filed).
- Position caps, the daily-loss breaker and the kill switch apply everywhere.
- Every entry needs a dossier, a graded and sourced catalyst, a stop and a target.

## State-of-the-world dials

`get_market_context` (and the Brain page) carry twelve cross-asset dials scored from the 20-day tape, -1 (headwind) to +1
(tailwind): breadth, trend (SPY), volatility (VIX level and term structure), credit (HYG vs LQD), small caps (IWM vs SPY),
participation (RSP vs SPY), growth vs broad (QQQ vs SPY), rates (10-year change and the 10y-3m curve), dollar, copper/gold,
oil and crypto. The system prompt tells the agent to let them direct where it looks and how hard it presses; the overview
shows the top eight as compact bars, the Brain shows all of them with notes plus the next scheduled prints and FOMC dates.

## Study mode (off hours)

When the task queue is empty inside the study window (`[watch] study_start`/`study_end`, default 20:00-06:30 ET) the daemon
runs bounded **study sessions**: the agent picks one topic from the curriculum in `munchkin/knowledge.py` (inflation prints,
the Fed's reaction function, credit spreads, options market structure, event-day patterns, cash-account mechanics, ...) or a gap
it noticed, spends at most six searches and four page fetches on primary sources, and writes a note with `save_knowledge`.
Notes live in `data/knowledge/<slug>.md`; the index is in every system prompt and `get_knowledge` returns the full note.
At most `study_per_night` sessions run per rolling 12 hours (default 2), so it is a nightly seminar, not an all-night crawl.
Trading tools are disabled in study sessions.

## Context window

The transcript budget adapts to the model. On start the client asks the serving stack for the context size (LM Studio reports the
loaded context, Ollama `num_ctx`, OpenRouter the model card, vLLM `max_model_len`; OpenAI/Anthropic use known sizes) and derives
`effective_chars = (tokens - max_tokens - ~10k tool overhead) x 4`, capped by `[llm] context_char_budget`. Tool results are scaled
down in proportion so a smaller window is compacted harder instead of failing. Under about 32k tokens the dashboard shows a
warning; under 16k the agent loses most of its context between tool calls. If the server does not report a size, set
`LLM_CONTEXT_TOKENS` in `.env`. The Model card on the Config tab shows what was detected.

## Risk engine

All order tools call `RiskEngine` first. It computes a *virtual account* (starting capital plus
realized/unrealized P&L, so a larger broker balance never leaks into sizing), settled cash
(sale proceeds are unavailable until T+1), the rolling 5-day day-trade count from fills, and the
daily-loss breaker. Options must be long premium or 1:1 debit spreads with every short leg covered
by a same-type long leg expiring no earlier. Tunables live in `munchkin.toml`.

## Going live later

Point `.env` at live keys with `ALPACA_PAPER=false`. Review `munchkin.toml` limits first. The
same risk engine applies; the daemon and journal are unchanged.
