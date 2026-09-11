# Event study: returns on past print dates and the following sessions, per symbol, versus SPY.
# ARGS: {"dates": ["2026-08-12", ...], "symbols": ["UBER", "AAPL"], "horizons": [1, 3]}
dates = [str(d)[:10] for d in ARGS.get("dates", [])]
horizons = [int(h) for h in ARGS.get("horizons", [1, 3])]
syms = [s for s in bars if s != "SPY"] or list(bars)
spy = bars.get("SPY")
if not dates:
    print("no dates given: pass ARGS.dates as ISO strings of past print dates")
rows = []
for sym in syms:
    df = bars[sym]
    if "close" not in df:
        continue
    closes = df["close"]
    idx = [d.strftime("%Y-%m-%d") for d in closes.index]
    for d in dates:
        if d not in idx:
            continue
        i = idx.index(d)
        if i == 0:
            continue
        rec = {"symbol": sym, "date": d, "day0_%": round((closes.iloc[i] / closes.iloc[i - 1] - 1) * 100, 2)}
        for h in horizons:
            if i + h < len(closes):
                rec[f"+{h}d_%"] = round((closes.iloc[i + h] / closes.iloc[i - 1] - 1) * 100, 2)
        if spy is not None and "close" in spy:
            sidx = [x.strftime("%Y-%m-%d") for x in spy.index]
            if d in sidx:
                j = sidx.index(d)
                if j > 0:
                    rec["spy_day0_%"] = round((spy["close"].iloc[j] / spy["close"].iloc[j - 1] - 1) * 100, 2)
                    rec["rel_day0_%"] = round(rec["day0_%"] - rec["spy_day0_%"], 2)
        rows.append(rec)
if rows:
    t = pd.DataFrame(rows)
    print(t.to_string(index=False))
    num = [c for c in t.columns if c.endswith("%")]
    print("\nmean by symbol:")
    print(t.groupby("symbol")[num].mean().round(2).to_string())
    print("\nhit rate (day0 > 0) by symbol:")
    print(t.groupby("symbol")["day0_%"].apply(lambda s: round((s > 0).mean() * 100)).to_string())
else:
    print("no overlap between the given dates and the loaded bars; load more bars or check the dates")
