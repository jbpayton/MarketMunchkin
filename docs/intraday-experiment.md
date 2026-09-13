# Intraday experiment: opening-range continuation (shadow-only)

Implements the "MarketMunchkin: intraday experiment and implementation brief" of 2026-09-13. Everything here runs in
shadow mode: it never places broker orders. Live routing is deliberately not a code path in this version
(`set_status(..., "live")` returns False). Promotion, budgets and demotion stay with the operator.

## What runs

- **Experiment versions** are immutable: the configuration (detector parameters, entry window, thresholds, horizon,
  contract policy, costs, risk envelope) is hashed; a changed parameter registers a new version and a new cohort. The
  operational `mode` is not part of the identity. `munchkin experiment` registers the default version if none exists.
- **Detector** (`orb-continuation/1`): completed 5-minute IEX bars; the first 30 minutes of the regular session form
  the range; inside the entry window (10:00–14:00 ET) a completed close outside the range, no more than 1.0% past the
  edge, with time-of-day-adjusted relative volume ≥ 1.5 (IEX cumulative volume against the prior 10 sessions' cumulative
  IEX volume at the same time; never IEX against SIP) and relative strength versus SPY since the open ≥ 0.3 points in
  the direction of the break. Freshness 180 s, average dollar volume ≥ $20M, and the 120-minute horizon plus a
  30-minute close buffer must fit before the session close from the broker calendar (early closes respected). One
  signal per symbol, direction and session (dedupe key), across ticks and restarts.
- **Every eligible candidate is recorded before the model sees it**, with its feature snapshot, the bar end, receipt
  time, and the detector version. Rejections are counted by reason.
- **Classifier**: a bounded structured call to the configured model (`catalyst-classifier/1`): event type, direction,
  novelty, substantive, source quality, published time, URL, excerpt, thesis, confidence, abstain. Inputs are the
  headlines available at decision time; the first classification is immutable. Latency and model id are stored. A
  failed or late model means variant B and C **abstain**; they never fall back to the baseline.
- **Variants**: A mechanical (analytical signed underlying returns at 60/120/180 minutes, and cash-constrained stock
  books); B mechanical plus the classifier filter (accepted versus rejected, same measurements); C the filtered signal
  expressed with a bought call or put under a deterministic contract policy (DTE 7–21, |delta| 0.45–0.65, two-sided
  fresh quote, spread ≤ 15%, cost including the $0.65 fee within budget; ranked by delta closeness, spread, expiry).
  Bearish signals are analytical only for A/B; nothing ever simulates a short stock holding.
- **Shadow fills** happen at the first eligible observation after a 90-second decision latency, at the ask (buy) or bid
  (sell) plus adverse slippage (5 bps stock, 1% options), never on the quote that triggered the decision. Fallback,
  crossed, stale or missing quotes mean no fill or a censored exit, not a mid-price. Options are marked on the
  **indicative** feed, which is delayed: variant C results are exploratory, not executable validation.
- **Exits**: stock stops at the broken range edge, time exit at the horizon, forced exit at the close buffer. Missing
  exit quotes leave a trade censored and counted.
- **Portfolios**: `p500` and `p750`, per variant, persisted. Stock cap 10% of equity, intended stop-loss budget 0.5%
  (the note on each trade says slippage can exceed it), single option premium 2% of equity including fees, aggregate
  4%, one concurrent position, a 2% daily-loss breaker that blocks new entries and stays tripped across restarts until
  the operator clears it. Under these defaults a $500 book admits almost no contract; that infeasibility is surfaced,
  not tuned away.

## Effective defaults and precedence

`ExperimentConfig` in `munchkin/experiments.py` holds the defaults above. The experiment envelope can only tighten the
active style: `tighten_limits(style_limits, cfg)` takes the minimum of each cap and never re-enables spreads. The live
path that would use it is not wired in this version.

## Running one shadow session

```bash
munchkin experiment --enable          # latest version -> shadow; the daemon thread then runs it during market hours
munchkin experiment --tick            # run one runner tick now (detect, classify, fill, mark, resolve)
munchkin experiment --report          # the A/B/C comparison: analytical books, cash-constrained books, feasibility counters
munchkin experiment --signal 12       # one signal: features, classification, decisions, quotes, outcomes
munchkin experiment --disable
```

The daemon runs the experiment in its own thread every 30 seconds while the market is open (outcome resolution and
forced exits continue off-session). The Brain page shows the same report and the recent signals; the dashboard can
switch shadow/disabled and clear breakers. The API is `/api/experiments`.

## Data limitations

- Options quotes are Alpaca's indicative feed (delayed). Executable options validation needs real-time OPRA, a paid
  subscription the operator has not chosen.
- Stock bars are IEX; IEX volume is a fraction of consolidated volume, hence IEX-versus-IEX relative volume.
- Polling is 30 s; ordering of events within a poll interval is unknown and stop/target ambiguity is resolved
  conservatively (stop first).
- No historical option prices exist beyond internal IV snapshots; variant C has no backtest, only forward shadow evidence.

## Evidence required before a live experiment

A predeclared review window with enough unique trading days; positive expectancy after costs on the cash-constrained
books, with the block-bootstrap interval reported by day; the classifier's accepted-versus-rejected gap on paired
decision-time prices; feasibility counters that show the strategy actually gets to trade under the envelope; and, for
options, executable quotes. None of that is claimed by passing tests.
