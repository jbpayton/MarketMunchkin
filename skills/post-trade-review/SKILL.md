---
name: post-trade-review
description: Grade a closed trade against its thesis, separate luck from process, and turn it into a lesson or a skill without overfitting to one outcome.
tags: review, learning
---
# Post-trade review

Use in post-market and REFLECT runs for every trade closed since the last review (get_journal trades, unreviewed first), and weekly on the whole book with `trade_stats.py`.

## Per trade: five questions
1. **Was the thesis right?** Compare the journaled thesis and catalyst grade with what actually happened to the name and the catalyst. Right thesis / wrong outcome and wrong thesis / lucky outcome are both common; name which.
2. **Was the entry the plan?** Trigger, chase, filter, size. A good trade taken 1% late is a process error even if it won.
3. **Was the exit the plan?** Stop hit, target hit, forced close, time stop, or discretion. Discretionary exits that beat the plan twice in a row are a sign the plan is wrong, not that discretion is good.
4. **What was knowable?** Only grade on information available at entry. Do not learn from noise: a stop hit by a 2-sigma wick is not a lesson about stops.
5. **Would the rule have helped across the sample?** Before writing a lesson, look at the same setup's history (get_setup_stats, or trade_stats.py). One trade is an anecdote.

## Writing it down
- `record_lesson` only for a rule that changes behaviour, in the form "When X, do Y, because Z (n trades)". Under 400 chars. Do not record outcomes, hopes or restatements of existing rules.
- If the rule is procedural and reusable, promote it with `save_skill` (name, one-line description, the procedure with its trigger and thresholds). It stays a draft until the operator approves it.
- Update the playbook only when a rule is retired or added, never to narrate the day.

## Weekly (trade_stats.py)
Expectancy, win rate, average win / loss, and the same split by stock vs option and by whether the trade was a forced close. Expectancy is the number that matters; a 40% win rate with 3:1 average win/loss is a good book. Compare the stats against the previous week's before drawing a conclusion.
