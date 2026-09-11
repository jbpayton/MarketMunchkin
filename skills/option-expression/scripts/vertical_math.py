# Payoff arithmetic for a bought single or a debit vertical.
# ARGS: {"type": "C", "long_strike": 72, "short_strike": 74, "long_mid": 1.99, "short_mid": 1.06, "spot": 72.99, "expected_move_pct": 8.4, "contracts": 1}
t = str(ARGS.get("type", "C")).upper()[0]
K1 = float(ARGS["long_strike"]); K2 = ARGS.get("short_strike"); K2 = float(K2) if K2 not in (None, "", 0) else None
p1 = float(ARGS["long_mid"]); p2 = float(ARGS.get("short_mid") or 0.0)
spot = float(ARGS.get("spot") or K1); em = float(ARGS.get("expected_move_pct") or 0.0); n = int(ARGS.get("contracts") or 1)
debit = round(p1 - (p2 if K2 else 0.0), 2)
mult = 100 * n
if K2:
    width = abs(K2 - K1); max_gain = round((width - debit) * mult, 2); max_loss = round(debit * mult, 2)
    be = K1 + debit if t == "C" else K1 - debit
    rr = round(max_gain / max_loss, 2) if max_loss else None
    print(f"{t} vertical {K1}/{K2}: debit {debit} = ${max_loss} risk, max gain ${max_gain} (at {K2} or beyond), reward:risk {rr}")
    print(f"debit is {round(debit / width * 100)}% of the width (under ~45% is the usual sweet spot for a directional vertical)")
else:
    max_loss = round(debit * mult, 2); be = K1 + debit if t == "C" else K1 - debit
    print(f"{t} single {K1}: premium {debit} = ${max_loss} at risk; breakeven {round(be, 2)}; +100% target needs the option worth {round(debit * 2, 2)}")
print(f"breakeven {round(be, 2)} is {round((be / spot - 1) * 100, 2)}% from spot {spot}")
if em:
    edge = abs(be / spot - 1) * 100
    print(f"expected move is +/-{em}%: the breakeven sits at {round(edge / em * 100)}% of one expected move" + (" (comfortably inside)" if edge < em * 0.5 else " (needs most of the expected move)" if edge < em else " (OUTSIDE the expected move: low probability)"))
    if K2:
        tgt = abs(K2 / spot - 1) * 100
        print(f"the short strike is {round(tgt, 2)}% away = {round(tgt / em * 100)}% of one expected move")
