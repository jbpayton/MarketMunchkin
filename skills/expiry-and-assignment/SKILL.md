---
name: expiry-and-assignment
description: What to do when an option position is inside two days of expiry, on expiry day itself, or when a vertical's short leg is deep in the money.
tags: options, risk, expiry
---
# Expiry and assignment

The fixed rule: no option is held into expiration. The watcher enforces it (wake at 1 DTE, forced close at 14:30 ET on expiry day, do-not-exercise filed at 15:45 as a backstop). This skill is what the agent does *before* the machinery has to.

## At 3 DTE (planning)
- Decide now: close, or roll the idea into a later expiry as a fresh entry with its own research. There is no "roll" primitive; a roll is a close plus a new entry that must pass the gate.
- If the position is at or beyond target, take it. The last days of a winning option lose the most to theta per day of remaining upside.
- If it is a loser inside the stop, the stop already fired or should have; check.

## At 1 DTE (the wake)
- Close during regular hours, with a limit near the mid; leave the DAY order working and check it filled before the run ends. Options are DAY-only orders here, so a resting limit will not survive to tomorrow.
- A vertical is closed as one multi-leg order (close_spread). Never leg out by selling the long first: an open short leg alone is exactly what the engine forbids, and it will refuse the second order.

## Expiry day
- Everything open is closed by the guard at 14:30 ET regardless of P&L. Do not fight it; if you want to keep the exposure, open a new position in a later expiry after the close of the old one.
- Pin risk: a short strike within about one expected-day-move of spot can be assigned even if it finishes a few cents out. Close such spreads in the morning, not at 14:30.

## Assignment risk on the short leg (any day)
- The watcher wakes when a short leg's delta passes `assignment_delta` (0.85). A deep in-the-money short call can be assigned early around ex-dividend dates; a short put when the extrinsic value is gone.
- Response: close the spread as a unit. Assignment in a cash account creates a share position the book may not be able to settle; the engine cannot prevent early assignment, only the exposure that invites it.

## Records
Journal the close with its reason (expiry, target, stop, assignment risk). Post-trade review treats a forced close differently from a stop.
