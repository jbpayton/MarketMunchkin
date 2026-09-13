"""Persistent memory: sessions, decisions, fills, round-trip trades, lessons, plan.

SQLite (stdlib). The playbook is a markdown file the agent can revise.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from typing import Any

from .config import DATA_DIR
from .util import ET, business_days_back, is_option, now_et

log = logging.getLogger("munchkin.journal")

DB_FILE = DATA_DIR / "munchkin.db"
PLAYBOOK_FILE = DATA_DIR / "playbook.md"
PLAYBOOK_HISTORY = DATA_DIR / "playbook_history"

DEFAULT_PLAYBOOK = """# MarketMunchkin Playbook (living document; revise via update_playbook)

## Edge hypotheses (to be validated)
- Multi-day swing trades on liquid names with a clear catalyst and a defined stop.
- Momentum continuation after high-volume breakouts; avoid chasing extended moves (>2 ATR above 20d SMA).
- Mean reversion only on quality names at 20d lows with RSI<30 and no broken thesis.
- Options: buy calls/puts or debit spreads only with a catalyst inside the holding window, DTE 14-45, delta 0.35-0.60, spread <15% of mid.

## Sizing
- Risk no more than ~8% of equity per idea on stocks (stop distance x size), max 40% notional in one name.
- Options premium per idea <= 25% of equity; total options premium <= 60%.

## Rules of thumb
- Manage existing positions before hunting for new ones.
- Every entry needs: thesis, catalyst/timing, target, stop, horizon.
- Day trades are scarce (3 per 5 days): reserve for stop-loss exits, never for scalping.
- Do not buy right before earnings with the whole account; size for the gap risk.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, phase TEXT, task TEXT, started_at TEXT, ended_at TEXT,
  summary TEXT, tool_calls INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, dry_run INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts TEXT, kind TEXT, symbol TEXT, underlying TEXT,
  side TEXT, qty REAL, price REAL, thesis TEXT, target TEXT, stop TEXT, horizon TEXT, order_id TEXT,
  status TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS fills (
  id TEXT PRIMARY KEY, ts TEXT, symbol TEXT, side TEXT, qty REAL, price REAL, order_id TEXT, is_option INTEGER);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, opened_at TEXT, closed_at TEXT, qty REAL,
  entry REAL, exit REAL, pnl REAL, pnl_pct REAL, is_option INTEGER, review TEXT, UNIQUE(symbol, opened_at, closed_at));
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, text TEXT, source TEXT, tags TEXT, weight REAL DEFAULT 1.0);
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_id INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS trace (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, seq INTEGER, ts TEXT, kind TEXT, name TEXT, args TEXT,
  result TEXT, secs REAL);
CREATE INDEX IF NOT EXISTS trace_session ON trace(session_id);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity REAL, cash REAL, market_open INTEGER);
CREATE TABLE IF NOT EXISTS iv_history (date TEXT, symbol TEXT, iv30 REAL, rv20 REAL, PRIMARY KEY(date, symbol));
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_id INTEGER, kind TEXT, priority INTEGER,
  text TEXT, status TEXT DEFAULT 'open', done_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS breadth_history (date TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS reservations (key TEXT PRIMARY KEY, amount REAL, created_at TEXT, expires_at TEXT, note TEXT);
"""
MIGRATIONS = [  # (table, column, type) added when missing; legacy rows keep NULL = unknown attribution
    ("decisions", "strategy_id", "INTEGER"), ("decisions", "experiment_id", "INTEGER"), ("decisions", "execution_mode", "TEXT"),
    ("trades", "strategy_id", "INTEGER"), ("trades", "experiment_id", "INTEGER"), ("trades", "execution_mode", "TEXT"),
]


class Journal:
    def __init__(self, path=DB_FILE) -> None:
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        PLAYBOOK_HISTORY.mkdir(exist_ok=True)
        if not PLAYBOOK_FILE.exists():
            PLAYBOOK_FILE.write_text(DEFAULT_PLAYBOOK)

    def _migrate(self) -> None:
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.commit()

    # ------------------------------------------------------------------ reservations (cash holds)
    def reserve(self, key: str, amount: float, available: float, ttl_s: int = 180, note: str = "") -> tuple[bool, float]:
        """Atomically hold `amount` of cash if it fits in `available` minus the other live holds. Serialised with an
        immediate transaction so two entry paths (a session and the watcher thread, or two processes) cannot both pass."""
        now = now_et()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("DELETE FROM reservations WHERE expires_at < ?", (now.isoformat(timespec="seconds"),))
            others = float(self.conn.execute("SELECT COALESCE(SUM(amount), 0) FROM reservations WHERE key != ?", (key,)).fetchone()[0])
            if amount > available - others + 1e-9:
                self.conn.execute("COMMIT")
                return False, others
            self.conn.execute("INSERT OR REPLACE INTO reservations(key, amount, created_at, expires_at, note) VALUES (?,?,?,?,?)",
                              (key, float(amount), now.isoformat(timespec="seconds"), (now + dt.timedelta(seconds=ttl_s)).isoformat(timespec="seconds"), note))
            self.conn.execute("COMMIT")
            return True, others
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def release(self, key: str) -> None:
        self.conn.execute("DELETE FROM reservations WHERE key=?", (key,))
        self.conn.commit()

    def reserved_total(self, exclude_key: str | None = None) -> float:
        now = now_et().isoformat(timespec="seconds")
        self.conn.execute("DELETE FROM reservations WHERE expires_at < ?", (now,))
        q = "SELECT COALESCE(SUM(amount), 0) FROM reservations" + (" WHERE key != ?" if exclude_key else "")
        return float(self.conn.execute(q, (exclude_key,) if exclude_key else ()).fetchone()[0])

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, json.dumps(value)))
        self.conn.commit()

    # ------------------------------------------------------------------ sessions
    def start_session(self, phase: str, task: str | None, dry_run: bool) -> int:
        cur = self.conn.execute("INSERT INTO sessions(phase, task, started_at, dry_run) VALUES (?,?,?,?)",
                                (phase, task, now_et().isoformat(timespec="seconds"), int(dry_run)))
        self.conn.commit()
        return int(cur.lastrowid)

    def end_session(self, sid: int, summary: str, tool_calls: int, ptok: int, ctok: int) -> None:
        self.conn.execute("UPDATE sessions SET ended_at=?, summary=?, tool_calls=?, prompt_tokens=?, completion_tokens=? WHERE id=?",
                          (now_et().isoformat(timespec="seconds"), summary, tool_calls, ptok, ctok, sid))
        self.conn.commit()

    def sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_session_summary(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE summary IS NOT NULL AND dry_run=0 ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ decisions
    def add_decision(self, session_id: int | None, kind: str, symbol: str, side: str | None = None,
                     qty: float | None = None, price: float | None = None, thesis: str | None = None,
                     target: str | None = None, stop: str | None = None, horizon: str | None = None,
                     order_id: str | None = None, status: str | None = None, meta: dict | None = None,
                     underlying: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions(session_id, ts, kind, symbol, underlying, side, qty, price, thesis, target, stop, horizon, order_id, status, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, now_et().isoformat(timespec="seconds"), kind, symbol, underlying, side, qty, price, thesis, target,
             stop, horizon, order_id, status, json.dumps(meta or {})))
        self.conn.commit()
        return int(cur.lastrowid)

    def update_decision_status(self, order_id: str, status: str) -> None:
        self.conn.execute("UPDATE decisions SET status=? WHERE order_id=?", (status, order_id))
        self.conn.commit()

    def decisions(self, limit: int = 20, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            rows = self.conn.execute("SELECT * FROM decisions WHERE symbol=? OR underlying=? ORDER BY id DESC LIMIT ?",
                                     (symbol, symbol, limit)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def thesis_for(self, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE symbol=? AND kind='open' AND status NOT IN ('rejected','canceled','blocked') ORDER BY id DESC LIMIT 1",
            (symbol,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ fills & trades
    def upsert_fills(self, fills: list[dict[str, Any]]) -> int:
        n = 0
        for f in fills:
            if f.get("activity_type") != "FILL":
                continue
            sym = f["symbol"]
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO fills(id, ts, symbol, side, qty, price, order_id, is_option) VALUES (?,?,?,?,?,?,?,?)",
                    (f["id"], f["transaction_time"], sym, f["side"], float(f["qty"]), float(f["price"]), f.get("order_id"),
                     int(is_option(sym))))
                n += self.conn.total_changes and 1
            except Exception as e:
                log.warning("fill insert failed: %s", e)
        self.conn.commit()
        return n

    def fills(self, since: dt.datetime | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM fills WHERE 1=1"
        args: list[Any] = []
        if since is not None:
            q += " AND ts >= ?"
            args.append(since.astimezone(dt.timezone.utc).isoformat())
        if symbol:
            q += " AND symbol = ?"
            args.append(symbol)
        q += " ORDER BY ts ASC"
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    def latest_fill_ts(self) -> str | None:
        row = self.conn.execute("SELECT MAX(ts) AS m FROM fills").fetchone()
        return row["m"] if row and row["m"] else None

    @staticmethod
    def _fill_date(ts: str) -> dt.date:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return t.astimezone(ET).date()

    def rebuild_trades(self) -> list[dict[str, Any]]:
        """FIFO round trips per symbol. Returns trades newly inserted."""
        fills = self.fills()
        by_sym: dict[str, list[dict]] = {}
        for f in fills:
            by_sym.setdefault(f["symbol"], []).append(f)
        new: list[dict[str, Any]] = []
        for sym, fs in by_sym.items():
            lots: list[list[float]] = []  # [qty, price, ts]
            opened_at = None
            mult = 100.0 if is_option(sym) else 1.0
            realized = 0.0
            entry_cost = 0.0
            closed_qty = 0.0
            exit_proceeds = 0.0
            for f in fs:
                side = f["side"]
                q = float(f["qty"])
                p = float(f["price"])
                if side == "buy":
                    if not lots:
                        opened_at = f["ts"]
                        realized = entry_cost = closed_qty = exit_proceeds = 0.0
                    lots.append([q, p, f["ts"]])
                elif side in ("sell", "sell_short"):
                    remaining = q
                    while remaining > 1e-9 and lots:
                        lot = lots[0]
                        take = min(lot[0], remaining)
                        realized += (p - lot[1]) * take * mult
                        entry_cost += lot[1] * take
                        exit_proceeds += p * take
                        closed_qty += take
                        lot[0] -= take
                        remaining -= take
                        if lot[0] <= 1e-9:
                            lots.pop(0)
                    if not lots and closed_qty > 0:
                        entry = entry_cost / closed_qty
                        exit_ = exit_proceeds / closed_qty
                        try:
                            cur = self.conn.execute(
                                "INSERT OR IGNORE INTO trades(symbol, opened_at, closed_at, qty, entry, exit, pnl, pnl_pct, is_option)"
                                " VALUES (?,?,?,?,?,?,?,?,?)",
                                (sym, opened_at, f["ts"], closed_qty, round(entry, 4), round(exit_, 4), round(realized, 2),
                                 round((exit_ / entry - 1) * 100, 2) if entry else None, int(mult > 1)))
                            if cur.rowcount:
                                new.append({"symbol": sym, "opened_at": opened_at, "closed_at": f["ts"], "pnl": round(realized, 2)})
                        except Exception as e:
                            log.warning("trade insert failed: %s", e)
        self.conn.commit()
        return new

    def realized_by_day(self) -> dict[str, float]:
        """Realized P&L per ET date from FIFO matching of fills, including partial exits."""
        by_sym: dict[str, list[dict]] = {}
        for f in self.fills():
            by_sym.setdefault(f["symbol"], []).append(f)
        out: dict[str, float] = {}
        for sym, fs in by_sym.items():
            mult = 100.0 if is_option(sym) else 1.0
            lots: list[list[float]] = []
            for f in fs:
                q, p = float(f["qty"]), float(f["price"])
                if f["side"] == "buy":
                    lots.append([q, p])
                else:
                    day = self._fill_date(f["ts"]).isoformat()
                    rem = q
                    while rem > 1e-9 and lots:
                        take = min(lots[0][0], rem)
                        out[day] = out.get(day, 0.0) + (p - lots[0][1]) * take * mult
                        lots[0][0] -= take
                        rem -= take
                        if lots[0][0] <= 1e-9:
                            lots.pop(0)
        return {k: round(v, 2) for k, v in out.items()}

    def trades(self, limit: int = 30, unreviewed_only: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM trades" + (" WHERE review IS NULL" if unreviewed_only else "") + " ORDER BY closed_at DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(q, (limit,)).fetchall()]

    def set_trade_review(self, trade_id: int, review: str) -> None:
        self.conn.execute("UPDATE trades SET review=? WHERE id=?", (review, trade_id))
        self.conn.commit()

    def trade_stats(self) -> dict[str, Any]:
        rows = self.trades(limit=1000)
        if not rows:
            return {"closed_trades": 0}
        pnls = [r["pnl"] for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        return {"closed_trades": len(rows), "win_rate_pct": round(len(wins) / len(rows) * 100, 1),
                "total_pnl": round(sum(pnls), 2), "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
                "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
                "best": round(max(pnls), 2), "worst": round(min(pnls), 2)}

    # ------------------------------------------------------------------ day trades (PDT)
    def day_trades(self, window_business_days: int = 5) -> list[dict[str, Any]]:
        """(date, symbol) pairs where a buy preceded a sell of the same symbol on the same day."""
        start = business_days_back(window_business_days - 1)
        since = dt.datetime.combine(start, dt.time.min, tzinfo=ET)
        fs = self.fills(since=since)
        by_day_sym: dict[tuple[dt.date, str], list[dict]] = {}
        for f in fs:
            by_day_sym.setdefault((self._fill_date(f["ts"]), f["symbol"]), []).append(f)
        out = []
        for (d, sym), lst in by_day_sym.items():
            seen_buy = False
            for f in lst:
                if f["side"] == "buy":
                    seen_buy = True
                elif seen_buy and f["side"] in ("sell", "sell_short"):
                    out.append({"date": d.isoformat(), "symbol": sym})
                    break
        return sorted(out, key=lambda x: x["date"])

    def bought_today(self, symbol: str) -> bool:
        today = now_et().date()
        since = dt.datetime.combine(today, dt.time.min, tzinfo=ET)
        return any(f["side"] == "buy" for f in self.fills(since=since, symbol=symbol))

    def unsettled_proceeds(self, settlement_days: int = 1) -> float:
        """Sale proceeds from sells that have not yet settled (T+settlement_days)."""
        today = now_et().date()
        cutoff = business_days_back(settlement_days - 1, today) if settlement_days > 1 else today
        since = dt.datetime.combine(cutoff, dt.time.min, tzinfo=ET)
        total = 0.0
        for f in self.fills(since=since):
            if f["side"] in ("sell", "sell_short"):
                total += float(f["qty"]) * float(f["price"]) * (100.0 if f["is_option"] else 1.0)
        return round(total, 2)

    # ------------------------------------------------------------------ lessons / notes / plan
    def add_lesson(self, text: str, source: str = "agent", tags: str = "", weight: float = 1.0) -> int:
        cur = self.conn.execute("INSERT INTO lessons(ts, text, source, tags, weight) VALUES (?,?,?,?,?)",
                                (now_et().isoformat(timespec="seconds"), text.strip(), source, tags, weight))
        self.conn.commit()
        return int(cur.lastrowid)

    def lessons(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM lessons ORDER BY weight DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def add_note(self, text: str, session_id: int | None = None) -> None:
        self.conn.execute("INSERT INTO notes(ts, session_id, text) VALUES (?,?,?)",
                          (now_et().isoformat(timespec="seconds"), session_id, text.strip()))
        self.conn.commit()

    def notes(self, limit: int = 10) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def get_plan(self) -> str:
        return self.get("plan", "") or ""

    def set_plan(self, text: str) -> None:
        self.set("plan", text.strip())
        self.set("plan_ts", now_et().isoformat(timespec="seconds"))

    # ------------------------------------------------------------------ traces / events / equity (dashboard)
    def add_trace(self, session_id: int | None, kind: str, name: str | None = None, args: str | None = None,
                  result: str | None = None, secs: float | None = None) -> None:
        seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM trace WHERE session_id=?", (session_id,)).fetchone()[0]
        self.conn.execute("INSERT INTO trace(session_id, seq, ts, kind, name, args, result, secs) VALUES (?,?,?,?,?,?,?,?)",
                          (session_id, seq, now_et().isoformat(timespec="seconds"), kind, name, args, result, secs))
        self.conn.commit()

    def trace(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM trace WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def session(self, session_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def add_event(self, kind: str, text: str) -> None:
        self.conn.execute("INSERT INTO events(ts, kind, text) VALUES (?,?,?)", (now_et().isoformat(timespec="seconds"), kind, text[:2000]))
        self.conn.commit()

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def add_equity(self, equity: float, cash: float, market_open: bool) -> None:
        self.conn.execute("INSERT OR REPLACE INTO equity(ts, equity, cash, market_open) VALUES (?,?,?,?)",
                          (now_et().isoformat(timespec="seconds"), equity, cash, int(market_open)))
        self.conn.commit()

    def equity_series(self, since: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        if since:
            rows = self.conn.execute("SELECT * FROM equity WHERE ts >= ? ORDER BY ts LIMIT ?", (since, limit)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM equity ORDER BY ts LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ self-assigned task queue
    def add_task(self, text: str, priority: int = 2, session_id: int | None = None, kind: str = "task") -> int:
        cur = self.conn.execute("INSERT INTO tasks(ts, session_id, kind, priority, text) VALUES (?,?,?,?,?)",
                                (now_et().isoformat(timespec="seconds"), session_id, kind, int(priority), text.strip()))
        self.conn.commit()
        return int(cur.lastrowid)

    def open_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM tasks WHERE status='open' ORDER BY priority ASC, id ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def next_task(self) -> dict[str, Any] | None:
        t = self.open_tasks(1)
        return t[0] if t else None

    def complete_task(self, task_id: int, result: str = "", status: str = "done") -> bool:
        cur = self.conn.execute("UPDATE tasks SET status=?, done_at=?, result=? WHERE id=? AND status='open'",
                                (status, now_et().isoformat(timespec="seconds"), result[:2000], task_id))
        self.conn.commit()
        return cur.rowcount > 0

    def tasks_history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def close_orphans(self, note: str = "orphaned: daemon restarted mid-session") -> int:
        cur = self.conn.execute("UPDATE sessions SET ended_at=?, summary=COALESCE(summary, ?) WHERE ended_at IS NULL",
                                (now_et().isoformat(timespec="seconds"), note))
        self.conn.commit()
        return cur.rowcount

    def sessions_today(self, phase: str | None = None) -> int:
        d = now_et().date().isoformat()
        if phase:
            return self.conn.execute("SELECT COUNT(*) FROM sessions WHERE started_at LIKE ? AND phase=?", (d + "%", phase)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM sessions WHERE started_at LIKE ?", (d + "%",)).fetchone()[0]

    # ------------------------------------------------------------------ iv / breadth history
    def record_iv(self, symbol: str, iv30: float | None, rv20: float | None) -> dict[str, Any]:
        d = now_et().date().isoformat()
        if iv30 is not None:
            self.conn.execute("INSERT OR REPLACE INTO iv_history(date, symbol, iv30, rv20) VALUES (?,?,?,?)", (d, symbol.upper(), iv30, rv20))
            self.conn.commit()
        rows = self.conn.execute("SELECT iv30 FROM iv_history WHERE symbol=? ORDER BY date", (symbol.upper(),)).fetchall()
        hist = [r["iv30"] for r in rows if r["iv30"] is not None]
        if len(hist) < 2 or iv30 is None:
            return {"iv_history_days": len(hist)}
        return {"iv_history_days": len(hist), "iv_rank_pct": round(sum(1 for h in hist if h <= iv30) / len(hist) * 100),
                "iv_min": round(min(hist), 1), "iv_max": round(max(hist), 1)}

    def record_breadth(self, data: dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO breadth_history(date, data) VALUES (?,?)", (now_et().date().isoformat(), json.dumps(data)))
        self.conn.commit()

    def breadth_history(self, days: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT date, data FROM breadth_history ORDER BY date DESC LIMIT ?", (days,)).fetchall()
        return [{"date": r["date"], **json.loads(r["data"])} for r in reversed(rows)]

    # ------------------------------------------------------------------ exit levels (resting stop / watched target)
    def exits(self, symbol: str) -> dict[str, Any] | None:
        return self.get(f"exits:{symbol.upper()}")

    def all_exits(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT key, value FROM kv WHERE key LIKE 'exits:%'").fetchall()
        return {r["key"][6:]: json.loads(r["value"]) for r in rows if r["value"] and r["value"] != "null"}

    def set_exits(self, symbol: str, **fields: Any) -> dict[str, Any]:
        cur = self.exits(symbol) or {}
        cur.update({k: v for k, v in fields.items() if v is not None})
        cur["updated"] = now_et().isoformat(timespec="seconds")
        self.set(f"exits:{symbol.upper()}", cur)
        return cur

    def clear_exits(self, symbol: str) -> None:
        self.set(f"exits:{symbol.upper()}", None)

    # ------------------------------------------------------------------ playbook
    def playbook(self) -> str:
        return PLAYBOOK_FILE.read_text()

    def update_playbook(self, new_text: str) -> None:
        old = PLAYBOOK_FILE.read_text()
        stamp = now_et().strftime("%Y%m%d-%H%M%S")
        (PLAYBOOK_HISTORY / f"playbook-{stamp}.md").write_text(old)
        PLAYBOOK_FILE.write_text(new_text.strip() + "\n")
