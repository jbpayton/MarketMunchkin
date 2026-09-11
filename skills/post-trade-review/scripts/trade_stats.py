# Book statistics from closed round trips. ARGS: {"days": 30}
days = int(ARGS.get("days", 30))
t = trades if trades is not None else pd.DataFrame()
if t is None or t.empty or "pnl" not in t:
    print("no closed trades yet")
else:
    t = t.copy(); t["closed_at"] = pd.to_datetime(t["closed_at"], errors="coerce")
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    t = t[t["closed_at"] >= cutoff] if t["closed_at"].notna().any() else t
    def block(g, label):
        if g.empty:
            return
        wins = g[g["pnl"] > 0]; losses = g[g["pnl"] <= 0]
        wr = len(wins) / len(g); aw = wins["pnl"].mean() if len(wins) else 0.0; al = -losses["pnl"].mean() if len(losses) else 0.0
        exp = wr * aw - (1 - wr) * al
        print(f"{label}: n={len(g)} win rate {wr*100:.0f}% avg win ${aw:.2f} avg loss ${al:.2f} expectancy ${exp:.2f}/trade total ${g['pnl'].sum():.2f}")
    block(t, f"all (last {days}d)")
    if "is_option" in t:
        block(t[t["is_option"] == 1], "options"); block(t[t["is_option"] != 1], "stock")
    if "review" in t:
        block(t[t["review"].fillna("").str.contains("forced|expiry", case=False)], "forced closes")
    print("\nlargest win / loss:")
    print(t.sort_values("pnl").iloc[[0, -1]][["symbol", "pnl", "pnl_pct", "closed_at"]].to_string(index=False))
