"""State watcher: polls fills, prices, targets and news between sessions and decides when to wake the agent."""
from __future__ import annotations

import datetime as dt
import re
import json
import logging
from typing import Any

from .broker import Broker
from .config import WatchSettings
from .entries import EntryBook, execute_entry, expired, index_ok, not_yet, trigger_hit
from .exits import ExitManager, expiry_plan, force_close_option
from .optentry import SpreadBook, close_spread_now, spread_value
from .journal import Journal
from .market import Market
from .util import is_option, now_et, occ_human, parse_occ

log = logging.getLogger("munchkin.watch")


class Watcher:
    def __init__(self, broker: Broker, market: Market, journal: Journal, exits: ExitManager, cfg: WatchSettings, risk=None) -> None:
        self.b, self.m, self.j, self.x, self.cfg = broker, market, journal, exits, cfg
        self.risk = risk
        self.entries = EntryBook(journal)
        self.notified: dict[str, dt.datetime] = {}
        self.last_news_check: dt.datetime = now_et() - dt.timedelta(minutes=30)
        self.baseline: dict[str, float] = self.j.get("watch:baseline", {}) or {}
        self.last_fill_ts: str | None = self.j.get("watch:last_fill_ts")

    # ------------------------------------------------------------------ helpers
    def _recently(self, key: str, minutes: int) -> bool:
        t = self.notified.get(key)
        return bool(t and (now_et() - t).total_seconds() < minutes * 60)

    def _mark(self, key: str) -> None:
        self.notified[key] = now_et()

    def mark_session_done(self, positions: list[dict[str, Any]] | None = None) -> None:
        """Reset move baselines after a session so the next wake is about NEW movement."""
        positions = self.b.positions() if positions is None else positions
        syms = {parse_occ(p["symbol"])["underlying"] if is_option(p["symbol"]) else p["symbol"] for p in positions} | {"SPY", "QQQ"}
        snaps = self.m.snapshots(sorted(syms))
        self.baseline = {s: float(v["price"]) for s, v in snaps.items() if v.get("price")}
        for p in positions:
            if is_option(p["symbol"]):
                self.baseline[p["symbol"]] = float(p["current_price"])
        self.j.set("watch:baseline", self.baseline)
        self.last_news_check = now_et()

    # ------------------------------------------------------------------ checks
    def check_fills(self) -> list[str]:
        events: list[str] = []
        after = None
        if self.last_fill_ts:
            after = dt.datetime.fromisoformat(self.last_fill_ts.replace("Z", "+00:00"))
        fills = self.b.fills(after=after)
        session_orders = {d["order_id"] for d in self.j.decisions(200) if d.get("order_id") and d["kind"] in ("open", "close")}
        newest = self.last_fill_ts
        for f in fills:
            if f.get("activity_type") != "FILL":
                continue
            ts = f["transaction_time"]
            if self.last_fill_ts and ts <= self.last_fill_ts:
                continue
            newest = max(newest or "", ts)
            if f.get("order_id") in session_orders:
                continue  # the agent placed it and already knows
            events.append(f"FILL not placed by the agent (likely a resting stop): {occ_human(f['symbol'])} {f['side']} {f['qty']} @ {f['price']}")
        if newest and newest != self.last_fill_ts:
            self.last_fill_ts = newest
            self.j.set("watch:last_fill_ts", newest)
        self.j.upsert_fills(fills)
        return events

    def check_prices(self, positions: list[dict[str, Any]], actions: list[str] | None = None) -> list[str]:
        actions_ref: list[str] = actions if actions is not None else []
        events: list[str] = []
        if not positions:
            syms = ["SPY", "QQQ"]
        else:
            syms = sorted({parse_occ(p["symbol"])["underlying"] if is_option(p["symbol"]) else p["symbol"] for p in positions} | {"SPY", "QQQ"})
        snaps = self.m.snapshots(syms)
        for s, v in snaps.items():
            px = v.get("price")
            base = self.baseline.get(s)
            if not px or not base:
                continue
            move = (px / base - 1) * 100
            thr = self.cfg.index_move_pct if s in ("SPY", "QQQ") else self.cfg.position_move_pct
            if abs(move) >= thr and not self._recently(f"move:{s}", self.cfg.target_renotify_min):
                events.append(f"MOVE: {s} {move:+.1f}% since last session (now {px})")
                self._mark(f"move:{s}")
        # thesis horizons: a position past its journaled horizon is a decision, not a drift
        from .book import parse_horizon_days, trading_days_between
        for p in positions:
            th = self.j.thesis_for(p["symbol"]) or {}
            hd = parse_horizon_days(th.get("horizon"))
            if hd is None or not th.get("ts"):
                continue
            try:
                age = trading_days_between(dt.datetime.fromisoformat(th["ts"]), now_et())
            except Exception:
                continue
            if age >= hd and not self._recently(f"hz:{p['symbol']}", 24 * 60):
                self._mark(f"hz:{p['symbol']}")
                events.append(f"HORIZON: {occ_human(p['symbol'])} thesis horizon {hd:g}d elapsed ({age}d, {float(p.get('unrealized_plpc') or 0) * 100:+.2f}%): exit or re-thesis in writing")
        # targets / option levels
        for p in positions:
            sym = p["symbol"]
            lv = self.j.exits(sym) or {}
            take = getattr(self.cfg, "target_mode", "take") == "take" and getattr(self, "_market_open", False)
            if is_option(sym):
                cur = float(p["current_price"])
                tgt, stp = lv.get("target_price"), lv.get("stop_price")
                if tgt and cur >= float(tgt) and not self._recently(f"tgt:{sym}", self.cfg.target_renotify_min):
                    self._mark(f"tgt:{sym}")
                    if take and not self._recently(f"took:{sym}", 30):
                        self._mark(f"took:{sym}")
                        self._take_option_target(sym, p, cur, float(tgt), actions_ref, events)
                    else:
                        events.append(f"TARGET: {occ_human(sym)} mark {cur} >= target premium {tgt}")
                if stp and cur <= float(stp) and not self._recently(f"stp:{sym}", 10):
                    events.append(f"STOP LEVEL: {occ_human(sym)} mark {cur} <= stop premium {stp} (check the resting stop / act)")
                    self._mark(f"stp:{sym}")
            else:
                px = snaps.get(sym, {}).get("price")
                tgt = lv.get("target_price")
                if px and tgt and px >= float(tgt) and not self._recently(f"tgt:{sym}", self.cfg.target_renotify_min):
                    self._mark(f"tgt:{sym}")
                    real_quote = snaps.get(sym, {}).get("price_src") != "yfinance-fallback"
                    if take and real_quote and not self._recently(f"took:{sym}", 30):
                        self._mark(f"took:{sym}")
                        self._take_stock_target(sym, p, float(px), float(tgt), actions_ref, events)
                    else:
                        events.append(f"TARGET: {sym} {px} >= target {tgt}")
        return events

    def check_news(self, positions: list[dict[str, Any]]) -> list[str]:
        if not self.cfg.news_wake or not positions:
            return []
        syms = sorted({parse_occ(p["symbol"])["underlying"] if is_option(p["symbol"]) else p["symbol"] for p in positions})
        events: list[str] = []
        try:
            items = self.m.news(syms, 10, hours=2)
        except Exception as e:
            log.warning("news check failed: %s", e)
            return []
        cutoff = self.last_news_check.strftime("%m-%d %H:%M")
        for it in items:
            if it["time"] and it["time"] > cutoff:
                key = f"news:{it['headline'][:40]}"
                if not self._recently(key, 240):
                    events.append(f"NEWS: [{it['symbols']}] {it['headline']}")
                    self._mark(key)
        self.last_news_check = now_et()
        return events

    def check_option_orders(self, open_orders: list[dict[str, Any]]) -> list[str]:
        """A resting option BUY limit is re-priced toward the ask at 5 and 10 minutes (capped at ask + 1% and the original limit
        + 3%), then cancelled at 15: a decision becomes a fill or a clean no, never a stale order into the close."""
        actions: list[str] = []
        now = now_et()
        for o in open_orders:
            sym = str(o.get("symbol") or "")
            if not is_option(sym) or str(o.get("side")) != "buy" or str(o.get("type") or "").lower() != "limit" or not o.get("limit_price"):
                continue
            key = "optwork:" + sym
            st = self.j.get(key) or {}
            if not st:
                st = {"orig_limit": float(o["limit_price"]), "first_ts": now.isoformat(timespec="seconds"), "reprices": 0, "order_id": o["id"]}
                self.j.set(key, st)
                continue
            try:
                age = (now - dt.datetime.fromisoformat(st["first_ts"])).total_seconds() / 60
            except Exception:
                age = 0.0
            limit = float(o["limit_price"]); orig = float(st["orig_limit"]); qty = int(float(o.get("qty") or 1))
            try:
                snap = self.m.option_snapshots([sym])[0]
                ask = float(snap.get("ask") or 0)
            except Exception:
                ask = 0.0
            if age >= 15:
                try:
                    self.b.cancel_order(o["id"])
                    self.j.add_decision(None, "cancel", sym, side="buy", qty=qty, price=limit, order_id=o["id"], status="canceled",
                                        meta={"reason": f"option buy unfilled after {age:.0f} min (ask {ask or '?'} vs limit {limit}); the setup is stale", "mechanical": True}, underlying=parse_occ(sym)["underlying"])
                    self.j.set(key, None)
                    actions.append(f"{occ_human(sym)}: buy limit {limit} cancelled after {age:.0f} min unfilled (ask {ask or '?'})")
                except Exception as e:
                    actions.append(f"{occ_human(sym)}: cancel FAILED: {str(e)[:80]}")
                continue
            step = 1 if age >= 5 and st["reprices"] == 0 else 2 if age >= 10 and st["reprices"] == 1 else 0
            if not step or not ask or ask <= limit:
                continue
            new = round(min(ask, orig * 1.02), 2) if step == 1 else round(min(ask * 1.01, orig * 1.03), 2)
            if new <= limit:
                continue
            try:
                self.b.cancel_order(o["id"])
                r = self.b.submit_option_order(sym, "buy", qty, new, "buy_to_open")
                st.update({"reprices": step, "order_id": r.get("id")})
                self.j.set(key, st)
                self.j.add_decision(None, "reprice", sym, side="buy", qty=qty, price=new, order_id=r.get("id"), status=r.get("status", "submitted"),
                                    meta={"reason": f"re-priced from {limit} toward the ask {ask} at {age:.0f} min", "mechanical": True, "from": limit}, underlying=parse_occ(sym)["underlying"])
                actions.append(f"{occ_human(sym)}: buy limit re-priced {limit} -> {new} (ask {ask}, {age:.0f} min)")
            except Exception as e:
                actions.append(f"{occ_human(sym)}: re-price FAILED: {str(e)[:80]}")
        live = {str(o.get("symbol")) for o in open_orders}
        for k in list(self.j.all_kv_keys("optwork:") if hasattr(self.j, "all_kv_keys") else []):
            if k[8:] not in live:
                self.j.set(k, None)
        return actions

    def check_world(self, positions: list[dict[str, Any]]) -> list[str]:
        """Hourly: a cross-asset dial changing sign, or a fresh headline on a theme a position depends on, wakes the agent
        for exactly the positions that declared the dependency."""
        if self._recently("world:tick", 60):
            return []
        self._mark("world:tick")
        events: list[str] = []
        deps: dict[str, str] = {}      # symbol -> lowercased dependency text (for dial matching)
        themes_of: dict[str, str] = {}  # symbol -> depends_on as written (for theme queries)
        for p in positions:
            th = self.j.thesis_for(p["symbol"]) or {}
            try:
                meta = json.loads(th.get("meta") or "{}")
            except Exception:
                meta = {}
            txt = " ".join(x for x in (meta.get("depends_on"), meta.get("invalidated_by")) if x).strip()
            if txt:
                deps[p["symbol"]] = txt.lower()
            if meta.get("depends_on"):
                themes_of[p["symbol"]] = str(meta["depends_on"])
        try:
            from . import macro as M
            dials = M.world_dials(M.market_dashboard(), None, None)
        except Exception:
            dials = []
        prev = self.j.get("watch:dials") or {}
        cur = {d["key"]: d["score"] for d in dials}
        words = {"rates": ["rate", "10y", "yield", "fomc", "fed", "hike", "cut"], "oil": ["oil", "wti", "brent", "hormuz", "energy"], "dollar": ["dollar", "dxy"],
                 "credit": ["credit", "spread", "hy"], "vol": ["vix", "vol"], "breadth": ["breadth"], "size": ["small cap", "iwm"], "growth": ["growth", "qqq", "ai"],
                 "trend": ["spy", "index", "trend"], "crypto": ["crypto", "btc"], "cycle": ["copper", "gold"], "participation": ["participation", "equal-weight"]}
        def sign(x: float) -> int:
            return 1 if x > 0.15 else -1 if x < -0.15 else 0
        label = {1: "tailwind", -1: "headwind", 0: "neutral"}
        for k, sc in cur.items():
            if k in prev and sign(prev[k]) != sign(sc):
                hit = [s for s, ds in deps.items() if any(w in ds for w in words.get(k, [k]))]
                events.append(f"DIAL FLIP: {k} went {label[sign(prev[k])]} -> {label[sign(sc)]} ({sc:+.2f})" + (f"; positions that depend on it: {', '.join(hit)} — re-thesis or act" if hit else ""))
        if cur:
            self.j.set("watch:dials", cur)
        themes: dict[str, list[str]] = {}
        for sym, ds in themes_of.items():
            for part in re.split(r"[;,/]| and ", ds):
                t = part.strip()
                if 4 <= len(t) <= 48 and not t.lower().startswith("sector"):
                    themes.setdefault(t, []).append(sym)
        if themes:
            try:
                from .search import web_search
                seen = set(self.j.get("watch:theme_seen") or [])
                for theme, syms in list(themes.items())[:5]:
                    for r in web_search(theme, "news", 4, "day")[:4]:
                        key = (r.get("title") or "")[:70]
                        if key and key not in seen:
                            seen.add(key)
                            events.append(f"THEME NEWS ({theme}; {', '.join(sorted(set(syms)))}): {r.get('title')} [{(r.get('date') or '')[:16]}] {r.get('url') or ''}")
                self.j.set("watch:theme_seen", sorted(seen)[-400:])
            except Exception as e:
                log.warning("theme news check failed: %s", e)
        return events[:8]

    def check_expiry(self, positions: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """Never hold an option into exercise: wake early, force-close on expiration day, file DNE as backstop."""
        opts = [p for p in positions if is_option(p["symbol"])]
        if not opts or not self.cfg.expiry_guard:
            return [], []
        deltas: dict[str, float] = {}
        shorts = [p["symbol"] for p in opts if float(p["qty"]) < 0]
        if shorts:
            try:
                for r in self.m.option_snapshots(shorts):
                    if r.get("delta") is not None:
                        deltas[r["symbol"]] = float(r["delta"])
            except Exception as e:
                log.warning("delta lookup failed: %s", e)
        plan = expiry_plan(opts, now_et(), self.cfg.expiry_wake_dte, self.cfg.expiry_force_close_time, self.cfg.expiry_dne_time,
                           self.cfg.assignment_delta, deltas)
        events: list[str] = []
        actions: list[str] = []
        for a in plan:
            key = f"{a['action']}:{a['symbol']}"
            if a["action"] in ("wake", "assignment_risk"):
                if not self._recently(key, 60):
                    events.append(("EXPIRY: " if a["action"] == "wake" else "ASSIGNMENT RISK: ") + a["reason"])
                    self._mark(key)
            elif a["action"] == "force_close":
                if not self._recently(key, 20):
                    self._mark(key)
                    try:
                        actions.append(force_close_option(self.x, a["symbol"], a["qty"]))
                        events.append("EXPIRY: " + a["reason"] + " (order placed by the watcher; verify the fill)")
                    except Exception as e:
                        actions.append(f"{a['symbol']}: forced close FAILED: {str(e)[:120]}")
                        events.append(f"EXPIRY: forced close of {a['symbol']} failed ({str(e)[:80]}); act now")
            elif a["action"] == "dne":
                if not self._recently(key, 120):
                    self._mark(key)
                    try:
                        self.b.do_not_exercise(a["symbol"])
                        actions.append(f"{a['symbol']}: do-not-exercise filed")
                        self.j.add_decision(None, "dne", a["symbol"], status="filed", meta={"reason": a["reason"]})
                    except Exception as e:
                        actions.append(f"{a['symbol']}: DNE FAILED: {str(e)[:120]}")
        return events, actions

    def check_entries(self, positions: list[dict[str, Any]], market_open: bool) -> tuple[list[str], list[str]]:
        """Fire armed conditional entries whose trigger price has been crossed."""
        recs = self.entries.all()
        if not recs:
            return [], []
        events: list[str] = []
        actions: list[str] = []
        now = now_et()
        for sym, rec in recs.items():
            if expired(rec, now):
                from .entries import arm_outcome, arm_outcome_text
                self.entries.disarm(sym)
                rv = arm_outcome_text(arm_outcome(self.m, rec))
                try:
                    self.j.add_note(rv + " (expired)")
                except Exception:
                    pass
                actions.append(f"{sym}: armed entry expired (was: {rec['direction']} {rec['trigger_price']}); {rv}")
                continue
        recs = self.entries.all()
        if not recs or not market_open or self.risk is None:
            return events, actions
        held = {p["symbol"] for p in positions}
        snaps = self.m.snapshots(sorted(set(recs) | {"SPY"}))
        spy_chg = snaps.get("SPY", {}).get("chg_pct")
        for sym, rec in recs.items():
            px = snaps.get(sym, {}).get("price")
            if not px or not_yet(rec, now) or not index_ok(rec, spy_chg):
                continue
            if snaps.get(sym, {}).get("price_src") == "yfinance-fallback":
                # fallback quotes can lag by minutes: never open a position on one. Exits and resting stops are unaffected.
                if rec["direction"] == "above" and px >= rec["trigger_price"] or rec["direction"] == "below" and px <= rec["trigger_price"]:
                    events.append(f"{sym}: armed trigger {rec['direction']} {rec['trigger_price']} seen on a fallback quote ({px}); waiting for the broker feed before firing")
                continue
            if sym in held and (rec.get("expression") or "stock") == "stock":
                # double-buy protection for stock arms only; an option expression on a held underlying is a deliberate
                # add-on and is sized against the per-underlying cap when it fires
                self.entries.disarm(sym)
                actions.append(f"{sym}: armed stock entry dropped, the stock is already held (an add-on must be an option expression)")
                continue
            if trigger_hit(rec, float(px)):
                try:
                    msg = execute_entry(rec, float(px), self.risk, self.b, self.m, self.j, self.x)
                except Exception as e:
                    msg = f"{sym}: conditional entry FAILED: {str(e)[:120]}"
                actions.append(msg)
                if "EXECUTED" in msg or "blocked" in msg or "FAILED" in msg or "Nothing bought" in msg or "not filled yet" in msg:
                    self.entries.disarm(sym)
                    events.append("ENTRY: " + msg)
        return events, actions

    def check_spreads(self, positions: list[dict[str, Any]], market_open: bool) -> tuple[list[str], list[str]]:
        """Debit verticals: wake at target, force-close at the stop on net value (spreads cannot carry a resting stop)."""
        sb = SpreadBook(self.j)
        recs = sb.all()
        if not recs:
            return [], []
        held = {p["symbol"] for p in positions}
        events: list[str] = []
        actions: list[str] = []
        for key, rec in recs.items():
            if rec["long"] not in held:
                sb.clear(key)
                actions.append(f"spread {occ_human(rec['long'])} no longer held; record cleared")
                continue
            if not market_open:
                continue
            try:
                v = spread_value(self.m, rec)
            except Exception as e:
                log.warning("spread value failed: %s", e)
                continue
            if v is None:
                continue
            if v <= float(rec["stop"]) and not self._recently(f"spstop:{key}", 20):
                self._mark(f"spstop:{key}")
                try:
                    msg = close_spread_now(self.b, self.j, rec, f"stop hit: value {v} <= {rec['stop']}", v)
                    sb.clear(key)
                    actions.append(msg)
                    events.append("STOP: " + msg)
                except Exception as e:
                    events.append(f"STOP: spread {occ_human(rec['long'])} at stop ({v}) but close FAILED: {str(e)[:80]}; act now")
            elif v >= float(rec["target"]) and not self._recently(f"sptgt:{key}", self.cfg.target_renotify_min):
                self._mark(f"sptgt:{key}")
                if getattr(self.cfg, "target_mode", "take") == "take":
                    try:
                        msg = close_spread_now(self.b, self.j, rec, f"target hit: value {v} >= {rec['target']}", v)
                        sb.clear(key)
                        actions.append("TARGET TAKEN: " + msg)
                        events.append("TARGET TAKEN: " + msg)
                    except Exception as e:
                        events.append(f"TARGET: spread {occ_human(rec['long'])} at target ({v}) but close FAILED: {str(e)[:80]}; act now")
                else:
                    events.append(f"TARGET: spread {occ_human(rec['long'])}/{occ_human(rec['short'])} value {v} >= target {rec['target']} (entry {rec['entry']}); take profit or trail")
        return events, actions

    # ------------------------------------------------------------------ tick
    # ------------------------------------------------------------------ targets (mechanical)
    def _take_stock_target(self, sym: str, p: dict[str, Any], px: float, tgt: float, actions: list[str], events: list[str]) -> None:
        held = float(p.get("qty_available") or p["qty"])
        pct = max(1, min(100, int(self.cfg.target_take_pct)))
        q = held if pct >= 100 else round(held * pct / 100, 6)
        try:
            self.x.cancel_exit_orders(sym)
            o = self.b.submit_stock_order(sym, "sell", qty=q, order_type="market")
            fill = o.get("filled_avg_price") or px
            self.j.add_decision(None, "close", sym, side="sell", qty=q, price=fill, order_id=o.get("id"), status=o.get("status", "submitted"),
                                meta={"reason": f"target {tgt} reached ({px}); mechanical take {pct}%", "mechanical": True, "partial": q < held - 1e-9}, underlying=sym)
            remaining = 0.0 if q >= held - 1e-9 else round(held - q, 6)
            if remaining > 0:
                lv = self.j.exits(sym) or {}
                new_stop = max(float(lv.get("stop_price") or 0), float(p.get("avg_entry_price") or 0))
                self.x.update(sym, stop_price=new_stop)
                self.x.place_stop(sym, remaining, new_stop)
                msg = f"TARGET TAKEN: {sym} sold {q:g} of {held:g} at ~{fill} (target {tgt}); {remaining:g} left with the stop at {new_stop}"
            else:
                self.j.set("exits:" + sym, None)
                msg = f"TARGET TAKEN: {sym} sold {q:g} at ~{fill} (target {tgt})"
            actions.append(msg)
            events.append(msg + " — review the thesis; re-enter only through the gate")
        except Exception as e:
            events.append(f"TARGET: {sym} {px} >= target {tgt} but the mechanical sell FAILED: {str(e)[:100]}; act now")

    def _take_option_target(self, sym: str, p: dict[str, Any], cur: float, tgt: float, actions: list[str], events: list[str]) -> None:
        q = int(float(p.get("qty_available") or p["qty"]))
        try:
            snap = next((r for r in self.m.option_snapshots([sym]) if r.get("symbol") == sym), {})
            bid = snap.get("bid")
            if not bid:
                raise RuntimeError("no bid")
            self.x.cancel_exit_orders(sym)
            o = self.b.submit_option_order(sym, "sell", q, float(bid), "sell_to_close")
            self.j.add_decision(None, "close", sym, side="sell", qty=q, price=float(bid), order_id=o.get("id"), status=o.get("status", "submitted"),
                                meta={"reason": f"target premium {tgt} reached (mark {cur}); mechanical sell at bid", "mechanical": True}, underlying=parse_occ(sym)["underlying"])
            self.j.set("exits:" + sym, None)
            msg = f"TARGET TAKEN: {occ_human(sym)} {q}x sell-to-close at {bid} (mark {cur} >= target {tgt})"
            actions.append(msg)
            events.append(msg + " — confirm the fill; a DAY limit at the bid can miss on a fast tape")
        except Exception as e:
            events.append(f"TARGET: {occ_human(sym)} mark {cur} >= target premium {tgt} but the mechanical sell FAILED: {str(e)[:100]}; act now")

    def tick(self, market_open: bool) -> tuple[list[str], list[str]]:
        self._market_open = market_open
        """Returns (events that should wake the agent, housekeeping actions taken)."""
        events: list[str] = []
        actions: list[str] = []
        try:
            positions = self.b.positions()
            open_orders = self.b.open_orders()
        except Exception as e:
            log.warning("watch: broker unavailable: %s", e)
            return [], []
        try:
            events += self.check_fills()
        except Exception as e:
            log.warning("watch fills: %s", e)
        if self.cfg.auto_stops:
            try:
                actions += self.x.ensure(positions, open_orders)
            except Exception as e:
                log.warning("watch ensure exits: %s", e)
        if market_open:
            try:
                actions += self.check_option_orders(open_orders)
            except Exception as e:
                log.warning("watch option orders: %s", e)
        try:
            ev, ac = self.check_expiry(positions)
            events += ev
            actions += ac
        except Exception as e:
            log.warning("watch expiry: %s", e)
        try:
            ev, ac = self.check_entries(positions, market_open)
            events += ev
            actions += ac
        except Exception as e:
            log.warning("watch entries: %s", e)
        try:
            ev, ac = self.check_spreads(positions, market_open)
            events += ev
            actions += ac
        except Exception as e:
            log.warning("watch spreads: %s", e)
        if market_open:
            try:
                events += self.check_prices(positions, actions)
            except Exception as e:
                log.warning("watch prices: %s", e)
            try:
                events += self.check_news(positions)
            except Exception as e:
                log.warning("watch news: %s", e)
            try:
                events += self.check_world(positions)
            except Exception as e:
                log.warning("watch world: %s", e)
        return events, actions
