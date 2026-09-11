<p align="center">
  <img src="assets/mascot-256.png" width="150" alt="MarketMunchkin mascot">
</p>

<h1 align="center">MarketMunchkin</h1>

<p align="center">
  An autonomous trading agent that runs on your own machine, on your own model, in a cash account.<br>
  It reads the world first, trades second, and never uses margin or writes contracts.
</p>

<p align="center">
  <b>Local LLM</b> · <b>Alpaca, paper or live</b> · <b>Cash only</b> · <b>Free data sources</b> · <b>Every decision traced</b>
</p>

![Overview: equity curve with axes, state-of-the-world dials, positions with resting stops, armed entries, the last session's reasoning as a timeline](docs/screenshots/overview.png)

## What it is

MarketMunchkin is a daemon plus a dashboard. The daemon runs an LLM through a tool loop against live market data
many times a day: it rebuilds a picture of the world from primary sources, screens roughly 600 liquid US names,
grades every catalyst by how well it is sourced, and only then places small, stop-protected trades through a
risk engine it cannot talk its way past. Everything it thinks, calls and decides is journaled and drawn on the
dashboard, so you can see why it did what it did.

It started as a "$500 to something" project with a 27B model in LM Studio, wired to an Alpaca paper account to
begin with; the same code drives a live account with one flag. It works with any OpenAI-compatible server that
supports tool calling (LM Studio, Ollama, vLLM, OpenRouter, OpenAI, Anthropic).

## What you see

<table>
  <tr>
    <td width="50%">
      <img src="docs/screenshots/brain.png" alt="Brain: regime gauge, twelve cross-asset dials, upcoming prints, the world brief">
      <p><b>The Brain.</b> A regime gauge, breadth, and twelve cross-asset dials scored from the 20-day tape (credit, size, participation, rates, dollar, oil, crypto and more), the next scheduled prints and FOMC dates, and the sourced world brief the agent wrote for itself.</p>
    </td>
    <td width="50%">
      <img src="docs/screenshots/position.png" alt="Position page: candles with entry, stop, armed trigger and target, order ladder, option payoff and IV read, thesis">
      <p><b>A position.</b> Candles with the journaled entry, resting stop, armed trigger and target drawn on the chart, the order ladder, the option payoff and volatility read, and the thesis with its catalyst grade. Held versus merely armed is always spelled out.</p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <img src="docs/screenshots/sessions.png" alt="Sessions: every run as a ribbon of thinking, tool calls, decisions and blocks, with the full flow underneath">
      <p><b>Sessions.</b> Every run is a ribbon: thinking, tool calls, decisions, rejections, summary. Expand any node for the exact arguments and results. The research gate's refusals show up in red.</p>
    </td>
    <td width="50%">
      <img src="docs/screenshots/light.png" alt="Light theme">
      <p><b>Light, dark, or system.</b> Palettes validated for both themes. The style switch is colour-coded: <b>Defensive blue, Balanced green, Aggressive red</b>. The kill switch needs a second click.</p>
    </td>
  </tr>
</table>

<p align="center">
  <img src="docs/screenshots/mobile.png" width="820" alt="The same pages on a phone">
</p>

## The rules that never change

These hold in every style and cannot be changed from the dashboard, the prompt, or by the model. They live in
`munchkin/styles.py` and the risk engine, which every order tool calls first.

- **Cash account only.** Buys use settled cash. Never margin, never unsettled proceeds.
- **No shorting stock.**
- **Never writes contracts.** No naked or credit options. A short leg exists only as the covered half of a debit
  vertical opened in the same order, and only where the style allows spreads.
- **No option is held into expiration.** Wake at 1 DTE, forced close at 14:30 ET on expiry day, do-not-exercise filed as a backstop.
- **Position caps, the daily-loss breaker and the kill switch apply everywhere.**
- **Every entry needs a dossier, a graded and sourced catalyst, a stop and a target.** The research gate refuses
  to commit capital until the session has read the market context, run a broad scan, charted several candidates
  and grounded the name in news.

## Trading styles

Three operator-selected styles change the risk envelope, the instruments, the cadence and the agent's brief.
The active style applies from the next session.

| | Defensive | Balanced (default) | Aggressive |
|---|---|---|---|
| instruments | stock only | stock, bought calls/puts, debit verticals | bought calls/puts first for fast setups; verticals when IV is rich |
| probes | $50–75 | $50–100 | $100–150 |
| per position / positions | 25% / 4 | 40% / 5 | 50% / 6 |
| catalyst grades | confirmed only | speculative ×0.5, unexplained ×0.35 | speculative ×0.75, unexplained ×0.5 |
| options budget | 0% | 60% of equity | 75% of equity |
| daily loss breaker | −8% | −15% | −20% |
| cadence / reasoning | 3 min gap, high | 60 s gap, medium | 60 s gap, events preempt, medium |

![Config: the three styles as cards, the fixed rules, the model card with the detected context window](docs/screenshots/config.png)

## How a day goes

- **Pre-market (08:45 ET).** Top-down: market context, overnight developments, today's data and Fed calendar,
  earnings, geopolitics. The agent rewrites its world brief with sources, reviews news on every holding, and
  sets a plan with concrete triggers.
- **Market hours.** Continuous sessions, one starting a minute after the last one ends. A watcher polls every
  60 seconds and preempts with an **event session** when a resting stop fills, a target trades, a holding or the
  index moves, headlines land on a position, or an option nears expiry.
- **Armed entries.** Instead of chasing, the agent arms instructions: "UBER call spread, $120, above 73.50, not
  before 08:35, only if SPY is not down more than 1%". The watcher executes them within a minute of the trigger
  and the option contracts are resolved at fire time.
- **Post-market (16:15 ET).** Grades the day's decisions against their theses, records lessons, revises the
  playbook if a rule should change, writes tomorrow's plan.
- **Off hours.** Works its own task queue (research it assigned itself), reflects when the queue is empty, and
  after 20:00 runs at most two bounded **study sessions** a night: one curriculum topic (inflation prints, the
  Fed's reaction function, credit spreads, options market structure, event-day patterns, cash-account
  mechanics, and twenty more), six searches and four page fetches at most, written up into a library note that
  every future session can read.

## Under the hood

- **Research gate and catalyst grades.** `confirmed` (primary source or several reputable outlets) sizes at
  1.0, `speculative` at 0.5, `none` (an unexplained move) at 0.35. Grades multiply the position cap.
- **State of the world.** A transparent regime score (SPY trend, breadth, VIX level and term structure), sector
  table, macro tape from yfinance, BLS prints from the BLS API, Fed releases from the Fed's RSS, twelve
  cross-asset dials, and the agent's own sourced brief. The prompt tells it to let these direct where it looks.
- **Screener.** S&P 500, Nasdaq-100 and liquid ETFs with indicators, cross-sectional momentum ranks, relative
  strength versus SPY and sector, beta, ADX, squeeze percentile, days to earnings, headline counts, and a
  one-call intraday snapshot with time-of-day-adjusted relative volume. Setup presets carry three-year backtest
  statistics so the model knows which patterns have edge.
- **Options.** Chains with greeks, ATM IV by expiry, IV versus realized, the straddle-implied expected move,
  25-delta skew, open-interest walls, and a stored IV history. Budget-aware contract resolution for singles and
  verticals; spreads are managed on net value.
- **Sandboxed analysis.** `run_analysis` lets the model write pandas / numpy / scipy / pandas_ta code that runs
  in a separate `python -I -S` interpreter with no credentials, CPU and memory limits, a timeout and an AST
  allowlist.
- **Journal.** SQLite memory: sessions with full traces, decisions and rejections, fills, round-trip trades,
  lessons (capped in length), notes, tasks, the plan, the playbook, exits, equity, breadth and IV history.
- **Exits.** Every entry carries a numeric stop and target. A protective stop rests at the broker (GTC for whole
  shares, DAY re-armed each morning for fractional shares and options). Targets are watched, not rested. Stops
  only ever trail up.
- **Context window.** The client asks the serving stack for the context size (LM Studio, Ollama, OpenRouter,
  vLLM) and derives the transcript budget from it, so a smaller model is compacted harder instead of failing.
  Below 32k tokens the Config tab warns you. `LLM_CONTEXT_TOKENS` overrides.
- **Providers.** SearXNG for search, optional Tavily (search and a fetch fallback for sites that block bots),
  Finnhub (news, earnings calendar) and StockTwits (social buzz), all on free tiers with monthly budgets,
  managed from the Config tab. Keys go to `.env`, never to the model.

## Quick start

You need Python 3.12, an Alpaca account with paper trading, and an OpenAI-compatible model server with tool
calling. A SearXNG instance is recommended for search.

```bash
git clone https://github.com/jbpayton/MarketMunchkin && cd MarketMunchkin
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env && chmod 600 .env   # Alpaca paper keys, model server, SearXNG URL
.venv/bin/munchkin test-llm              # connectivity + tool calling + detected context window
.venv/bin/munchkin status                # account, risk state, positions, plan
.venv/bin/munchkin run --phase intraday --dry-run   # a full session with orders validated but not sent
```

Run the daemon and the dashboard as user services:

```bash
cp scripts/munchkin-daemon.service scripts/munchkin-web.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now munchkin-daemon munchkin-web
journalctl --user -u munchkin-daemon -f
```

The dashboard is at `http://<this-machine>:8787`. Open it from a phone on the same network or over a VPN such
as Tailscale. Do not port-forward it to the internet. Set `MUNCHKIN_WEB_TOKEN` in `.env` to require a token.

### CLI

```bash
munchkin run --phase premarket|intraday|postmarket|research|reflect|study [--dry-run]
munchkin chat "Is NVDA a buy into earnings?"      # read-only Q&A with every research tool
munchkin watch                                     # one watcher tick
munchkin exits [SYMBOL --stop X --target Y]        # show or override exit levels and resting stops
munchkin journal trades|decisions|lessons|sessions|notes
munchkin plan | munchkin playbook
munchkin screener refresh | backtest --years 3 | earnings | regime | intraday --setups
munchkin screener query "rsi14 < 30 and avg_dollar_vol20_m > 50" --sort rsi14 --asc
munchkin halt | munchkin halt --resume             # block new entries; exits keep working
munchkin baseline --reset                          # re-anchor the virtual account to the broker balance
```

## Configuration

| where | what |
|---|---|
| `.env` | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER`, `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_CONTEXT_TOKENS`, `SEARXNG_URL`, provider keys, `MUNCHKIN_WEB_TOKEN` |
| `munchkin.toml` `[llm]` | reasoning effort per phase, tool budget, transcript budget |
| `munchkin.toml` `[risk]` | starting capital, caps, catalyst multipliers, option DTE / OI / spread filters, research gate |
| `munchkin.toml` `[schedule]` `[watch]` | session times, cadence, wake thresholds, expiry guard, off-hours windows, study mode |
| `data/llm.json`, `data/search.json` | dashboard overrides for the model and the provider chain (no restart needed) |
| dashboard Config tab | trading style, model, appearance, providers, kill switch |

## Security notes

- The model has no file, shell or environment tool. Credentials live in `.env` (mode 600, gitignored) and every
  tool result is scrubbed for the key strings before the model sees it.
- The sandbox for model-written code runs in a separate interpreter with a clean environment and resource limits.
- The dashboard has no authentication beyond an optional token. Keep it on your LAN or VPN.

## Going live

Point `.env` at live keys with `ALPACA_PAPER=false` and review the limits in `munchkin.toml`. The same risk engine,
daemon and journal apply. Starting in the Defensive style and letting the journal fill up for a few weeks is a
sensible way in.

## Disclaimer

MarketMunchkin is software, not financial advice. Its author is not a registered investment adviser or
broker-dealer, and nothing in this repository is a recommendation to buy or sell anything.

- Trading stocks and options involves risk of loss. Options can expire worthless. Past performance of the
  software, the strategies it finds, or any backtest it shows is no guarantee of future results.
- The decisions are made by a language model. Language models make mistakes and can be confidently wrong. The
  risk engine bounds what a bad decision can cost; it does not make decisions good.
- Market data comes from free third-party sources that can be delayed, wrong or unavailable.
- The software is provided "as is", without warranty of any kind. The authors and contributors accept no
  liability for any loss arising from its use. You are responsible for any account you connect to it and for
  complying with your broker's terms and the laws that apply to you.
