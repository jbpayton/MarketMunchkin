---
name: print-day
description: How to handle a scheduled macro print (CPI, PPI, payrolls, FOMC) before, at and after the release, and how to run an event study on past prints.
tags: macro, events, risk
---
# Print day

Use when the calendar (get_economic_calendar, the Brain's "next events") shows CPI, PPI, payrolls, PCE, retail sales or an FOMC decision within the next run, or when one printed in the last two hours.

## The evening before / pre-market
1. Get the facts: consensus and prior for headline and core (web_search "<print> consensus <month>", prefer Reuters/Bloomberg/BLS). Write them into the world brief with the source.
2. Decide the base case in one line (hot / in line / cool) and what each does to rates, the dollar and the index. Note which held names are most and least sensitive (run the event study below if you have not this week).
3. Every armed entry that could fire around the print needs `not_before` set to at least 5 minutes after the release and a `spy_min_chg_pct` tape filter. Disarm anything whose thesis is the print itself unless the trigger already encodes the outcome.
4. Do not add exposure in the last 30 minutes before a print. Existing stops stay; do not widen them.

## At the release (first 15 minutes)
- The first move often reverses. No market orders in the first 5 minutes. Read: ES/NQ, the 10-year, the dollar, then breadth (get_market_context, screen_intraday).
- Classify: hot print + yields up + dollar up = tightening; cool + yields down = easing; a hot print the market shrugs off is the most bullish outcome; a cool print that sells off is the most bearish.
- Only entries that were armed with a tape filter are allowed to fire in this window.

## After 10:00 ET
- If the reaction agrees with the base case and breadth confirms, work the plan. If it disagrees, cut the theses that depended on it before adding anything new.
- Post-market: record one lesson only if the print taught something about the *procedure*, not about the outcome.

## Event study
Run `event_study.py` with `dates` (ISO dates of past prints, from BLS pages or a web search) and `symbols` (holdings and candidates plus SPY): it reports each name's return on the day and the next 1 and 3 runs, versus SPY. Use it to rank which names are robust to a hot print. Keep the dates in a note so the next study does not repeat the search.

## Resources
`upcoming_releases.txt` lists the next scheduled BLS releases and FOMC decision dates as fetched when the skill was written; verify with get_economic_calendar, which is live.
