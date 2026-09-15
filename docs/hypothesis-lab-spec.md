# The Lab: hypotheses → tests → shadow → strategies

Status: proposal, 2026-09-11. Nothing in this document is built yet.

Decided 2026-09-11: promotion and demotion are human decisions, never automatic. The agent recommends; the operator decides.

## Why

The agent already produces claims about the market all day (lessons, notes, the playbook, study notes) and the
operator has hunches of their own. None of them are tested, tracked, or attributed to outcomes. Skills are know-how
(how to handle a print day); they are not the right container for a *claim* that may be false. This adds the missing
object: a hypothesis with a lifecycle, a test harness that resists fooling ourselves, a shadow phase that earns a
forward track record before capital, and promotion into a budgeted, attributable strategy.

## Concepts

| term | meaning |
|---|---|
| **Hypothesis** | a falsifiable claim with a trigger, a universe, a holding period, a side and an expected effect. Has an origin and a status. |
| **Test** | one run of a template (event study, screen backtest, custom) against history, always with a control, with a stored result and verdict. |
| **Shadow run** | the hypothesis' detector runs live; signals get virtual fills and virtual exits; no orders. |
| **Strategy** | a promoted hypothesis: a skill (procedure) + a detector (finds the setup) + a budget and kill rules + trade attribution. |
| **Detector** | a declarative condition (cheap, evaluated by the watcher every tick) or a sandbox script (run in monitor duties) that emits signals. |
| **Skill** | unchanged: procedures. Promotion *generates* a skill draft; a strategy's skill is its instruction half. |

## Data model (SQLite, in the journal)

```
hypotheses        id, created_at, updated_at, origin (operator|agent|study), origin_ref (session id / telegram chat),
                  title, statement, spec_json, status, notes
hypothesis_tests  id, hypothesis_id, session_id, created_at, kind (event_study|screen_backtest|custom),
                  params_json, window_start, window_end, result_json, verdict (pass|fail|inconclusive)
shadow_signals    id, hypothesis_id, ts, symbol, trigger_price, entry_px, exit_px, exit_ts, pnl_pct,
                  status (open|closed), reason
strategies        id, hypothesis_id, name, skill_name, detector_json, budget_pct, max_concurrent,
                  kill_json, status (active|paused|retired), stats_json
decisions, trades gain a nullable strategy_id; armed entries carry strategy_id in their record
```

`spec_json` (the specification the agent must produce before anything is tested):

```json
{"trigger": {"kind": "declarative", "conditions": {"spy_chg_pct": {"lte": -1.5}, "headline_regex": "tariff|deadline|ultimatum",
             "breadth_pct_above_50d": {"gte": 30}}},
 "universe": "SPY | screener:sector=Energy | list:[...]", "side": "long", "holding": {"sessions": 5, "stop_pct": 3, "target_pct": null},
 "expression": "stock | call | call_spread", "expected": {"horizon": "+5d", "effect_pct": 1.5, "vs_control_pct": 1.0},
 "controls": ["matched_days"], "data_needs": ["daily_bars", "news_counts"]}
```

## Lifecycle and gates

```
proposed -> specified -> testing -> tested(pass|fail|inconclusive) -> shadowing -> live -> paused|retired
                                                        \-> rejected (kept, with results)
```

- **proposed → specified**: the spec validates (trigger computable from data we have; universe resolvable; holding and
  expected effect present). The agent does this; a hunch from the operator is turned into a spec by the agent.
- **specified → tested**: at least one test with a control. Verdict rules (defaults, tunable in `munchkin.toml [lab]`):
  sample n ≥ 20 events (event study) or ≥ 30 signals (screen); mean effect at the stated horizon beats the control by
  ≥ `min_edge_pct` (0.5) **and** hit rate beats the control by ≥ 10 points; both halves of the window have the same
  sign; worst single outcome ≥ −3 × mean effect. Anything else is `inconclusive`, and two inconclusives is a `fail`.
- **tested(pass) → shadowing**: the detector is registered; the watcher tracks virtual fills at the next tick's price and
  virtual exits by holding period, stop or target. Requires ≥ `shadow_min_signals` (10) or `shadow_min_weeks` (4).
  Shadow expectancy must be > 0 and not worse than half the backtested effect.
- **shadowing → live**: operator approval only (dashboard or Telegram `/promote ID`). The system never promotes on its own,
  whatever the numbers say. A budget (`budget_pct` of
  equity, `max_concurrent`) and kill rules are set; a skill draft is generated from the spec and the test notes for the
  operator to approve; the detector switches from virtual to real: it arms entries (sized by the strategy, inside the
  style envelope) or wakes the agent with the strategy context, per the strategy's `mode`.
- **live → paused / retired**: the operator, any time, from the dashboard or Telegram (`/pause ID`, `/retire ID`). Kill
  rules do not act on their own; when one trips, the strategy stops taking *new* signals (existing positions keep their
  stops and targets) and a demotion recommendation goes to the operator with the numbers. Retired records keep everything.
- **rejected**: the propose tool refuses near-duplicates of rejected or failed claims (statement similarity + same trigger keys)
  and shows the prior result instead.

## Tests (sandbox templates)

All tests run in the existing sandbox (no network, no credentials) on datasets the parent prepares.

- **event_study**: dates from a detector replayed over history *or* an explicit list; symbols; horizons. Control =
  matched days: every day that meets the mechanical part of the trigger regardless of the narrative (e.g. SPY ≤ −1.5%),
  reported side by side. Reports n, mean, median, hit, worst, per-half stability.
- **screen_backtest**: a condition over the daily screener history (the setups backtester already does overlap-controlled
  forward returns over three years); control = the universe's unconditional forward return in the same window.
- **custom**: model-written code with a required output schema; the harness validates the JSON and refuses to store a
  verdict without a control field.
- Data limits, stated up front: daily bars ~2+ years; 5-minute bars a few weeks; no options history beyond our own IV
  snapshots. Hypotheses that need option history are *shadow-only* (no backtest gate, longer shadow minimum).

## Detectors

- **Declarative** (`detector_json`): conditions over what the watcher already has each tick (snapshots, SPY change, the
  intraday table, screener columns, dials, news counts, headline regex over the last hour of headlines). Evaluated in the
  watcher thread; cheap; emits signals with the symbol and price.
- **Script**: a sandbox script returning `[{symbol, price, note}]`, run in monitor duties (bounded, no LLM).
- Signals go to `shadow_signals` while shadowing; when live they call the strategy's action: `arm` (default; an armed
  entry with the strategy's stop/target/chase/filters and `strategy_id`), `buy` (probe at market, only if the style
  allows and the gate is satisfied by the strategy's standing dossier), or `wake` (EVENT run with the skill loaded).

## Tools (agent)

`propose_hypothesis(title, statement, origin_ref)`, `specify_hypothesis(id, spec)`, `list_hypotheses(status)`,
`get_hypothesis(id)`, `run_hypothesis_test(id, kind, params)`, `note_hypothesis(id, text)`. Promotion, budgets and
retirement are operator-only. Entry tools accept `strategy_id`.

## The decision inbox

Everything that needs a human lands in one place, on the Lab tab and as a Telegram message: promotion candidates
(a hypothesis that passed its tests and its shadow run, with the results attached), demotion recommendations (a kill
rule tripped, or the agent's post-market review argues a strategy has stopped working), and budget changes the agent
proposes. Each item has approve / decline / defer, and a declined item records why so it is not re-raised for 30 days.
The agent may recommend; only the operator promotes, demotes, or changes a budget.

## Operator surfaces

- Dashboard **Lab** tab: the ledger with status and origin, each hypothesis' tests and shadow book, strategies with
  live attribution (expectancy, drawdown, signals vs fills), buttons: promote, pause, retire, edit budget.
- Telegram: `/hypo <text>` proposes; `/lab` lists what is testing, shadowing and live; `/promote ID`, `/retire ID`.
- README section; `munchkin lab` CLI mirror.

## Daemon duties

- Off hours, a **LAB** duty joins study: pick the oldest `proposed` and specify it; pick the oldest `specified` and test
  it; re-test anything `tested` more than 30 days ago; summarise shadow books. Budget per night in `[lab]`
  (`lab_Runs_per_night`, default 4). Tokens are free on a local model; wall-clock is not: LM Studio serialises
  requests, so lab work yields to any market-hours duty and to operator requests.
- Post-market: the review reads strategy attribution and proposes retirements or new hypotheses from the day's lessons
  (replacing the current free-form "record a lesson" for anything that is really a claim).
- STUDY runs get a closing step: "state one testable hypothesis from what you learned", which lands as `proposed`.

## Safety

Everything stays inside the fixed rules and the risk engine: a strategy's budget is a cap under the style caps, its
entries pass the same checks, shadow runs never place orders, promotion needs a human, and kill rules pause first.
A hypothesis is data, not an instruction: the model reads statements and results, never executes text from the ledger.

## What skills become

Unchanged as a feature. Two roles: standalone know-how (print-day, expiry) and the instruction half of a strategy
(generated at promotion, editable, approved like any draft). The skill index line for a strategy skill shows its
status and budget so the model knows it is live.

## Build order and estimate

1. Ledger tables, spec validation, the six tools, `event_study` and `screen_backtest` templates with controls. (~half a day)
2. Declarative detectors in the watcher, shadow book with virtual fills and exits. (~half a day)
3. Attribution (`strategy_id` through decisions, trades, arms), kill rules, promotion flow (skill draft + detector switch),
   Lab tab, Telegram commands, LAB duty, docs and tests. (~half a day)

## Open questions

- Gate thresholds above are defaults; tighten or loosen per your appetite.
- ~~Should promotion ever be automatic?~~ Decided: no. Human-gated both ways, with an inbox of recommendations.
- Option-expression hypotheses: shadow-only as proposed, or also require an underlying-stock backtest?
- Strategy budgets: fixed per strategy, or a shared "strategies" pool split by recent expectancy?
