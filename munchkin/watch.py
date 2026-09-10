"""State watcher: polls fills, prices, targets and news between sessions and decides when to wake the agent."""
from __future__ import annotations

import datetime as dt
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

    def check_prices(self, positions: list[dict[str, Any]]) -> list[str]:
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
        # targets / option levels
        for p in positions:
            sym = p["symbol"]
            lv = self.j.exits(sym) or {}
            if is_option(sym):
                cur = float(p["current_price"])
                tgt, stp = lv.get("target_price"), lv.get("stop_price")
                if tgt and cur >= float(tgt) and not self._recently(f"tgt:{sym}", self.cfg.target_renotify_min):
                    events.append(f"TARGET: {occ_human(sym)} mark {cur} >= target premium {tgt}")
                    self._mark(f"tgt:{sym}")
                if stp and cur <= float(stp) and not self._recently(f"stp:{sym}", 10):
                    events.append(f"STOP LEVEL: {occ_human(sym)} mark {cur} <= stop premium {stp} (check the resting stop / act)")
                    self._mark(f"stp:{sym}")
            else:
                px = snaps.get(sym, {}).get("price")
                tgt = lv.get("target_price")
                if px and tgt and px >= float(tgt) and not self._recently(f"tgt:{sym}", self.cfg.target_renotify_min):
                    events.append(f"TARGET: {sym} {px} >= target {tgt}")
                    self._mark(f"tgt:{sym}")
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
                self.entries.disarm(sym)
                actions.append(f"{sym}: armed entry expired (was: {rec['direction']} {rec['trigger_price']})")
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
            if sym in held:
                self.entries.disarm(sym)
                actions.append(f"{sym}: armed entry dropped, position already held")
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
                events.append(f"TARGET: spread {occ_human(rec['long'])}/{occ_human(rec['short'])} value {v} >= target {rec['target']} (entry {rec['entry']}); take profit or trail")
        return events, actions

    # ------------------------------------------------------------------ tick
    def tick(self, market_open: bool) -> tuple[list[str], list[str]]:
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
                events += self.check_prices(positions)
            except Exception as e:
                log.warning("watch prices: %s", e)
            try:
                events += self.check_news(positions)
            except Exception as e:
                log.warning("watch news: %s", e)
        return events, actions
