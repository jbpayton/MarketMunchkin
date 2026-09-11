---
name: option-expression
description: Choose between a bought single and a debit vertical, pick expiry and strikes, size the premium and set the exits for an option expression of a stock idea.
tags: options, sizing
---
# Option expression

Use when a thesis has a catalyst and a time window and you want leverage or a defined-risk expression instead of stock. Only bought calls, bought puts and debit verticals exist here; the risk engine refuses anything else.

## 1. Read the volatility first (get_option_analytics)
- `iv_over_rv` below 1.0: premium is cheap relative to how the name actually moves -> a **single** is fine.
- 1.0 to 1.3: fair -> prefer a **debit vertical**; sell the wing to pay for the premium you are overpaying.
- above 1.3 or a fat 25-delta skew against you: rich -> vertical only, or stay in stock.
- Compare the straddle-implied expected move with your target. If the target sits inside the expected move, a single is paying for more than you need; a vertical whose short strike sits at the target captures the same path for less.

## 2. Expiry and strikes
- Expiry: your horizon plus a buffer, then round up to the next listed expiry, and never inside `min_option_dte`. Theta and the expiry guard both work against holding to the last days: plan to be out by 3 DTE.
- Single: long delta 0.45 to 0.60 (near the money). Deep out-of-the-money lottery tickets are refused by the sizing rules and lose anyway.
- Vertical: long strike near the money, short strike at or just past the target. Width 1 to 2 expected-move units.
- Check open interest and the bid/ask: the engine filters thin contracts, but a wide spread means give up more on exit.

## 3. Size
- Premium at risk is the position: it counts against the per-underlying cap and the options budget. A single can go to zero; treat 100% of premium as the stop distance for sizing.
- Under Balanced, an option expression of a speculative idea is half size; under Defensive, options are off.

## 4. Exits (set at entry, via set_exit_levels or the armed entry's opt_stop/opt_target)
- Stop: premium down 50% for singles, or the thesis invalidated on the stock, whichever first.
- Target: premium up 100% for singles; for a vertical, 70 to 80% of max value (the last 20% costs the most time).
- Time stop: if the stock has not moved by half the horizon, close; theta is now the dominant risk.

## Script
`vertical_math.py` with `type` (C/P), `long_strike`, `short_strike` (omit for a single), `long_mid`, `short_mid`, `spot`, `expected_move_pct`, `contracts`: prints debit, max loss, max gain, breakeven, reward:risk and where the breakeven sits inside the expected move. Use it before open_spread or buy_option.
