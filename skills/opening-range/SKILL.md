---
name: opening-range
description: Read the first thirty minutes of the session (gap type, opening range, VWAP, relative volume) and decide whether a break is tradeable.
tags: intraday, execution
---
# Opening range

Use in the first two intraday sessions of the day and whenever an armed entry is close to its trigger before 10:30 ET.

## First 5 minutes: look, do not trade
- No market orders. Spreads are wide, prints are noisy, and the first move often reverses. Armed entries should carry `not_before` 09:35 or later.

## Classify the open (screen_intraday, get_intraday_setups)
- Gap size against the daily ATR: under 0.5 ATR is noise; 0.5 to 1.5 ATR with a catalyst is a candidate; over 2 ATR without news is usually a fade.
- Relative volume at this time of day (the intraday table adjusts for it): a real gap-and-go runs on 2x or more; 1x means nobody cares.
- Where price sits versus VWAP after 15 minutes tells you who is in control. Above VWAP and holding the opening-range low = buyers; below VWAP and failing to reclaim = sellers.

## The opening range (first 30 minutes)
- OR high and OR low are the day's first real levels. A break of the OR high on rising relative volume with the index firm is a long trigger; a break of the OR low is a reason to stand aside or, for a holding, to tighten.
- A break that reverses back inside the range within a few bars is a failed break; failed breaks reverse hard. Do not chase the second attempt without new volume.
- The best long entries arm just above the OR high with a modest chase limit; the worst buy the first spike.

## Holdings at the open
- A holding gapping against you through its stop level is exited at the open by the resting stop; confirm the fill. Do not cancel a stop to "give it room" in the first minutes.
- A holding gapping in your favour beyond target: take at least half at the open, trail the rest under the OR low.

## Script
`opening_range.py` (5-minute bars, `symbols`, `or_minutes` default 30) prints, per symbol: gap %, OR high/low, current position within the range, VWAP distance, and relative volume for the elapsed part of the session against the prior sessions' same window.
