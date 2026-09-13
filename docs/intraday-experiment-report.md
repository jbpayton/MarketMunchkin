# Completion report: intraday experiment brief (2026-09-13)

## 1. Repository state inspected and discrepancies

Inspected at commit 729d0bb (2026-09-13). Discrepancies with the brief's reading of the repository:

- The brief says the lab spec "labels itself a proposal with nothing implemented". Phase one of the lab (ledger,
  spec validation, gates, event-study and screen-backtest templates with controls, night lab sessions, Brain card,
  Telegram `/hypo`) was implemented and live on 2026-09-11/12 (commits 064c7a5, b932d8d). Shadow runs, detectors and
  promotion were not, which is what this work adds for one concrete strategy.
- Entry paths converge on `RiskEngine.check_*`: the agent's buy tools and the watcher's `execute_entry` both call it.
  There was no cash reservation, so a session and the watcher thread could both pass the settled-cash check in the
  same second. Fixed (journal reservations, immediate transactions, counted by the engine).
- The option multiplier was hard-coded to 100 in four places in the risk engine. Fixed (read from the contract).
- No migration mechanism existed. A minimal idempotent column-add migration was added; legacy rows carry NULL
  attribution.
- Quotes carried the feed's price time but not a receipt time; the experiment stores both. The general snapshot path
  still does not, outside the experiment.
- Options data is Alpaca's indicative feed (delayed). Stock bars: SIP with a 16-minute lag by design, IEX in real time.
  The experiment uses IEX completed bars and IEX-vs-IEX relative volume.
- Settled cash: the risk engine derives a virtual settled figure from the broker's cash and unsettled activity; PDT
  counting was already off. No date-based regulatory switch is encoded.

## 2. Implemented, and deviations from the brief

Implemented (shadow-only): immutable experiment versions; signal recorder with feature snapshots, dedupe keys and
receipt times; the opening-range continuation detector; bounded structured classification with first-wins
immutability, abstain/error/late states and latency capture; variants A/B/C with reason-coded decisions; deterministic
contract policy that never relaxes for affordability (`no_suitable_contract_within_budget`); conservative shadow fills
after a latency window at ask/bid plus slippage, never on the triggering quote; structural stop, time and forced exits;
censored outcomes and trades when quotes are missing; fixed-horizon analytical outcomes with bearish returns signed
and never a holding; persisted $500/$750 books per variant with a sticky daily breaker; evaluation with day-clustered
bootstrap intervals and feasibility counters; the experiment envelope that only tightens style limits; a daemon
thread, CLI, API, Brain card, and 20 acceptance tests.

Deviations, all in the direction of less capability rather than more:

- **Live routing is absent, not merely disabled.** `set_status(..., "live")` returns False. The brief's phase 5
  (route promoted experimental entries through the broker with fakes) is not built; the envelope function exists and is
  tested so the wiring is a small later change.
- **Passive limit fills are outside version one**, as the brief allowed. All shadow fills are marketable.
- **Stop/target ordering inside a poll interval** is resolved stop-first; only a stop and a time exit exist in this
  version (no profit target), so the ambiguity does not arise yet.
- **Correlated-signal joint budgeting** is moot with `max_concurrent = 1`.
- **Deposits** cannot touch the shadow books (they only move on shadow P&L). The live account's equity curve already
  uses a virtual-account offset; no additional deposit accounting was added.
- **Historical LLM evaluation** was not attempted; only forward, timestamped classification is recorded.

## 3. Effective defaults and precedence

See `ExperimentConfig` in `munchkin/experiments.py` and the table in `docs/intraday-experiment.md`. Mode defaults to
`shadow` on the registered version, and the daemon registers the default version if none exists; the operator toggled v1
to shadow on 2026-09-13 so it observes from the next session. The envelope (`tighten_limits`) takes the minimum of
each cap against the active style and disables spreads; a more permissive style cannot loosen it (tested). Version
identity excludes the operational mode; any parameter change is a new version and cohort.

## 4. Running one shadow session and inspecting the comparison

```bash
munchkin experiment --enable     # already done for v1
munchkin experiment --tick       # one tick now (during market hours it detects; off-session it resolves outcomes)
munchkin experiment --report     # A/B/C: analytical outcomes, p500/p750 books, feasibility and rejection counters
munchkin experiment --signal N   # a signal's features, classification, decisions, quotes, outcomes
```

The Brain page shows the same report, recent signals with their decision trail, and buttons for shadow/disable and
clearing breakers. Poll cadence: 30 seconds in session.

## 5. Test results and remaining limitations

`pytest tests/`: 52 passed (20 for the experiment, with fakes and a fixed clock; no network, no broker). Scenarios
covered from the acceptance table: shadow-only install; a permissive style cannot loosen the envelope; $500 with a 2%
cap rejects a $25 contract and keeps the signal; a $0.50 premium is $50 plus fees of exposure; two entry paths racing
for cash; dedupe across ticks and restarts; a late model abstains and never fills retroactively; classification is
immutable; stale, fallback or missing quotes never produce a price; expiry and delta policy is never relaxed for
affordability; the early-close session blocks entries that cannot complete; the breaker persists across restarts and
a marked-equity recovery; missing outcome data is censored and counted; bearish signals never become a holding;
parameter changes create a new cohort; a legacy database migrates in place; multipliers come from the contract.

Not covered by tests: partial-fill and cancellation races (no live routing exists to race), forced-close failure
alerts (existing watcher behaviour, unchanged), deposits (see §2).

Limitations: indicative options quotes (variant C is exploratory); IEX bars and volume; 30-second polling; no option
price history; the classifier is a 27B local model whose calibration is unknown, which is precisely what variant B
measures.

## 6. Evidence still required before a live experiment

A predeclared review window with enough unique trading days that the day-clustered interval on net expectancy for
the cash-constrained books excludes zero; the B-accepted versus B-rejected gap measured on the common decision-time
price; feasibility counters showing the strategy actually trades under the envelope rather than being starved by it;
executable option quotes for any options variant; and a deliberate operator decision on budget and mode through the
existing controls. Passing tests establish that the machinery behaves as specified. They say nothing about whether
the strategy makes money.
