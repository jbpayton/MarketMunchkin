# Opening-range read from 5-minute bars. ARGS: {"or_minutes": 30}
orm = int(ARGS.get("or_minutes", 30)); n_or = max(1, orm // 5)
rows = []
for sym, df in bars.items():
    if "close" not in df or df.empty:
        continue
    d = df.copy(); d["day"] = [i.strftime("%Y-%m-%d") for i in d.index]
    days = sorted(set(d["day"])); today = days[-1]; prior = days[:-1][-5:]
    t = d[d["day"] == today]
    t = t[[i.strftime("%H:%M") >= "09:30" for i in t.index]]
    if t.empty:
        continue
    orng = t.iloc[:n_or]; hi, lo = float(orng["high"].max()), float(orng["low"].min())
    last = float(t["close"].iloc[-1]); o = float(t["open"].iloc[0])
    prev = d[d["day"] == (prior[-1] if prior else today)]
    prev_close = float(prev["close"].iloc[-1]) if not prev.empty else o
    vwap = float((t["close"] * t["volume"]).sum() / max(1.0, t["volume"].sum()))
    elapsed = len(t)
    same = [float(d[(d["day"] == p)][[i.strftime("%H:%M") >= "09:30" for i in d[d["day"] == p].index]]["volume"].iloc[:elapsed].sum()) for p in prior] if prior else []
    relvol = round(float(t["volume"].sum()) / (sum(same) / len(same)), 2) if same and sum(same) > 0 else None
    pos = round((last - lo) / (hi - lo) * 100) if hi > lo else None
    rows.append({"symbol": sym, "gap_%": round((o / prev_close - 1) * 100, 2), "OR_high": round(hi, 2), "OR_low": round(lo, 2), "last": round(last, 2),
                 "in_range_%": pos, "vs_OR": "above" if last > hi else "below" if last < lo else "inside", "vwap_dist_%": round((last / vwap - 1) * 100, 2), "rel_vol": relvol, "bars": elapsed})
print(pd.DataFrame(rows).to_string(index=False) if rows else "no bars for today; load 5Min bars for the symbols (bars=400 covers about a week)")
