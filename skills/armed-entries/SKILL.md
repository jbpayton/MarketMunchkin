---
name: armed-entries
description: Express an idea as an armed entry instead of chasing it: trigger, chase limit, time and tape filters, stock or option expression, and when to disarm.
tags: execution, entries
---
# Armed entries

Use whenever an idea is good but the price is not there yet, or a catalyst has a known time. Arming is how this book seeks opportunity without paying up: the watcher executes within a minute of the trigger, in any session, without waiting for the agent.

## Anatomy (arm_entry)
- `direction` + `trigger_price`: "above" for breakouts and reclaim levels, "below" for pullbacks into support. Use a level that means something on the chart you actually looked at (prior high, VWAP, the pre-spike low), never a round number for its own sake.
- Chase limit: the fill is refused if price has already run more than the chase percent past the trigger. Fast names get a wider chase; do not set it so wide that you buy the top of the spike.
- `not_before`: any entry whose thesis depends on a scheduled print goes live only after the print (see print-day). Also use it to skip the first 5 minutes of the open.
- `spy_min_chg_pct`: the tape filter. A breakout on a day the index is down 1% is a different trade. Default -1% for longs; tighten to -0.5% for high-beta names.
- `expression`: stock, or call / put / call_spread / put_spread with `dte_target`, `opt_stop` and `opt_target`. Contracts resolve at fire time, so the arm survives the chain changing overnight. Choose the expression with option-expression.
- `stop_price` / `target_price`: required. They are the stock-level invalidation and objective even for an option expression.
- `catalyst_grade`: the same grade rules as any entry; it scales the size.

## When NOT to arm
- If the setup is valid at the current price, buy the probe now. Arming a limit 1-2% below the market turns a backtested signal-close entry into an untested one that mostly fills when the thesis is breaking. Two days of this book's history: 13 of 14 arms were "below", median 1.3% away, three touched, none filled.
- Keep triggers within one daily ATR (the tool reports the distance and rough odds). Beyond that, arm_entry refuses unless the arm is event-conditioned with `not_before`.
- When an arm expires or is disarmed the journal gets an ARM REVIEW note (touched or not, closest approach, drift). Read them in post-market; repeated "never reached" is a placement habit to fix.

## Rules of thumb
- Arm two or three, not eight. Each arm is a promise to take the trade; if you would hesitate when it fires, do not arm it.
- Re-check every arm at the start of each session (list_entries): still valid thesis, still the right level, still the right tape filter. Disarm anything stale, and say why in the disarm reason.
- If price runs through the trigger while the filter blocks it, let it go. The filter was the point.
- One arm per name. A second idea on the same name replaces the first.
- Arms count toward the per-underlying cap when they fire; the engine sizes them then, not now.

## After it fires
The watcher journals the fill, rests the stop and wakes the agent with an event. Confirm the resting stop is present (get_positions shows it), then leave it alone unless the thesis changes.
