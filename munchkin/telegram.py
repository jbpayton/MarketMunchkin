"""Telegram: outbound alerts and a command channel for the operator.

Outbound (`notify`) is used by the daemon for fills, armed fires, errors, broker degradation and run summaries.
Inbound (`Bot`) long-polls the Bot API and answers quick commands from the journal and broker without the model;
anything else becomes an operator task that the next run answers, and the reply is pushed back to the chat.

Security: the bot only talks to chats that were paired with a one-time code generated on the dashboard. Everything
else gets a "not paired" reply and is ignored. Commands are a fixed set; free text only ever becomes a task for the
agent, which still runs behind the risk engine.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import time
from typing import Any, Callable

import httpx

from . import providers as P
from .config import DATA_DIR, HALT_FILE

log = logging.getLogger("munchkin.telegram")
STATE_FILE = DATA_DIR / "telegram.json"
API = "https://api.telegram.org/bot{token}/{method}"
KINDS = {"fills": "fills, armed entries fired, stops re-armed", "errors": "run failures", "broker": "broker / data feed degradation",
         "sessions": "pre-market plan and post-market review summaries", "events": "watcher wake events (moves, news, expiry)"}
DEFAULT_NOTIFY = {"fills": True, "errors": True, "broker": True, "sessions": True, "events": False}
RATE_LIMIT_S = {"errors": 600, "broker": 900}
MAX_LEN = 3900


# ---------------------------------------------------------------- state
def load_state() -> dict[str, Any]:
    try:
        d = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except Exception:
        d = {}
    d.setdefault("chat_ids", [])
    d.setdefault("notify", dict(DEFAULT_NOTIFY))
    d.setdefault("last_sent", {})
    return d


def save_state(d: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(d, indent=1))


def token() -> str:
    return P.get_key("telegram")


def configured() -> bool:
    return bool(token())


def paired() -> list[int]:
    return [int(x) for x in load_state().get("chat_ids", [])]


def new_pair_code(ttl_s: int = 600) -> str:
    d = load_state()
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    d["pair_code"], d["pair_expires"] = code, time.time() + ttl_s
    save_state(d)
    return code


def try_pair(chat_id: int, code: str) -> bool:
    d = load_state()
    if not d.get("pair_code") or time.time() > float(d.get("pair_expires", 0)):
        return False
    if code.strip().upper() != d["pair_code"]:
        return False
    if chat_id not in d["chat_ids"]:
        d["chat_ids"].append(chat_id)
    d.pop("pair_code", None)
    d.pop("pair_expires", None)
    save_state(d)
    return True


def unpair(chat_id: int) -> None:
    d = load_state()
    d["chat_ids"] = [c for c in d["chat_ids"] if int(c) != int(chat_id)]
    save_state(d)


def set_notify(kind: str, on: bool) -> None:
    d = load_state()
    if kind in KINDS:
        d["notify"][kind] = bool(on)
        save_state(d)


def mute(minutes: int) -> None:
    d = load_state()
    d["muted_until"] = time.time() + minutes * 60
    save_state(d)


def unmute() -> None:
    d = load_state()
    d.pop("muted_until", None)
    save_state(d)


# ---------------------------------------------------------------- outbound
def _post(method: str, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    r = httpx.post(API.format(token=token(), method=method), json=payload, timeout=timeout)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(f"telegram {method}: {d.get('description', r.text[:120])}")
    return d


def send(chat_id: int, text: str) -> bool:
    """Send plain text to one chat, chunked. Never raises."""
    try:
        text = text.strip() or "(empty)"
        for i in range(0, len(text), MAX_LEN):
            _post("sendMessage", {"chat_id": chat_id, "text": text[i:i + MAX_LEN], "disable_web_page_preview": True})
        return True
    except Exception as e:
        log.warning("telegram send failed: %s", e)
        return False


def notify(text: str, kind: str = "fills", chat_id: int | None = None, force: bool = False) -> bool:
    """Push to every paired chat (or one). Honours the per-kind toggles, mute, and a rate limit for noisy kinds."""
    if not configured():
        return False
    d = load_state()
    chats = [chat_id] if chat_id is not None else [int(x) for x in d.get("chat_ids", [])]
    if not chats:
        return False
    if not force:
        if kind in KINDS and not d["notify"].get(kind, True):
            return False
        if time.time() < float(d.get("muted_until", 0)):
            return False
        lim = RATE_LIMIT_S.get(kind)
        if lim and time.time() - float(d["last_sent"].get(kind, 0)) < lim:
            return False
    ok = all(send(c, text) for c in chats)
    if ok and kind in RATE_LIMIT_S:
        d = load_state()
        d["last_sent"][kind] = time.time()
        save_state(d)
    return ok


# ---------------------------------------------------------------- inbound
HELP = """MarketMunchkin commands
/status - equity, P&L, positions, arms, style
/positions  /arms  /last  /tasks
/halt  /resume - kill switch (asks you to confirm)
/style defensive|balanced|aggressive
/disarm SYMBOL
/mute 60 (minutes)  /unmute
/notify - show alert toggles;  /notify fills off
/hypo <claim> - propose a hypothesis for the Lab;  /lab - the ledger
Anything else you type is sent to the agent as an operator request; the answer comes back here on the next run (a minute or so during market hours)."""


class Bot:
    """Command handler. `deps` is a duck-typed bundle: journal, broker, risk (with .state()), style getters/setters, entries."""

    def __init__(self, journal: Any, broker: Any = None, risk: Any = None) -> None:
        self.j, self.b, self.r = journal, broker, risk
        self.pending_confirm: dict[int, tuple[str, float]] = {}
        self.offset = 0

    # ---- helpers
    def _status_text(self) -> str:
        from .entries import EntryBook
        from .styles import get_style
        lines = []
        try:
            st = self.r.state()
            lines.append(f"Equity ${st.virtual_equity:.2f} ({'+' if st.daily_pnl >= 0 else ''}{st.daily_pnl:.2f} today) · settled BP ${st.buying_power_now:.2f}")
            lines.append(f"Market {'OPEN' if st.market_open else 'closed'} · style {get_style(self.j)} · {'HALTED' if HALT_FILE.exists() else 'trading enabled'}")
        except Exception as e:
            lines.append(f"risk state unavailable: {str(e)[:80]}")
        try:
            pos = self.b.positions()
            if pos:
                reg = self.j.all_exits() if hasattr(self.j, "all_exits") else {}
                for p in pos:
                    ex = reg.get(p["symbol"], {}) if isinstance(reg, dict) else {}
                    lines.append(f"• {p['symbol']} {float(p['qty']):g} @ {float(p['avg_entry_price']):.2f} → {float(p['current_price']):.2f} "
                                 f"({float(p['unrealized_pl']):+.2f}) stop {ex.get('stop_level', '?')} tgt {ex.get('target_level', '?')}")
            else:
                lines.append("• flat")
        except Exception as e:
            lines.append(f"positions unavailable: {str(e)[:80]}")
        arms = EntryBook(self.j).all()
        if arms:
            lines.append("Armed: " + ", ".join(f"{a['symbol']} {a['direction']} {a['trigger_price']} ${a['notional']:g} {a.get('expression') or 'stock'}" for a in arms.values()))
        else:
            lines.append("Armed: none")
        s = self.j.sessions(1)
        if s:
            lines.append(f"Last Run #{s[0]['id']} {s[0]['phase']} {s[0]['started_at'][11:16]}: {(s[0].get('actions') or s[0].get('summary') or '')[:300]}")
        return "\n".join(lines)

    def _positions_text(self) -> str:
        try:
            pos = self.b.positions()
        except Exception as e:
            return f"positions unavailable: {str(e)[:80]}"
        if not pos:
            return "flat"
        return "\n".join(f"{p['symbol']}: {float(p['qty']):g} @ {float(p['avg_entry_price']):.2f} → {float(p['current_price']):.2f} ({float(p['unrealized_pl']):+.2f}, {float(p['unrealized_plpc']) * 100:+.2f}%)" for p in pos)

    def _arms_text(self) -> str:
        from .entries import EntryBook
        arms = EntryBook(self.j).all()
        if not arms:
            return "nothing armed"
        return "\n".join(f"{a['symbol']} {a['direction']} {a['trigger_price']} ${a['notional']:g} {a.get('expression') or 'stock'} · stop {a['stop_price']} tgt {a['target_price']} · {a['catalyst_grade']}"
                         + (f" · not before {a['not_before'][5:16]}" if a.get('not_before') else "") for a in arms.values())

    def _last_text(self) -> str:
        s = self.j.sessions(1)
        if not s:
            return "no runs yet"
        s = s[0]
        return f"#{s['id']} {s['phase']} {s['started_at'][11:16]} → {(s['ended_at'] or 'running')[11:16]}\n{(s.get('summary') or '(no summary)')[:3000]}"

    def _tasks_text(self) -> str:
        rows = self.j.open_tasks(10)
        return "\n".join(f"#{t['id']} [{t['kind']} p{t['priority']}] {t['text'][:120]}" for t in rows) if rows else "queue empty"

    # ---- dispatch
    def handle_text(self, chat_id: int, text: str) -> str:
        """Returns the reply for one incoming message."""
        text = (text or "").strip()
        if not text:
            return ""
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        arg = arg.strip()
        if cmd == "/pair":
            return "Paired. This chat now receives alerts and can command the agent. /help for commands." if try_pair(chat_id, arg) else "No valid pairing code. Generate one on the dashboard (Config → Telegram) and send /pair CODE within 10 minutes."
        if chat_id not in paired():
            return f"This chat is not paired (chat id {chat_id}). Generate a pairing code on the dashboard and send /pair CODE."
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return self._status_text()
        if cmd == "/positions":
            return self._positions_text()
        if cmd == "/arms":
            return self._arms_text()
        if cmd == "/last":
            return self._last_text()
        if cmd == "/tasks":
            return self._tasks_text()
        if cmd in ("/halt", "/resume"):
            want = cmd[1:]
            pend = self.pending_confirm.get(chat_id)
            if arg.lower() == "yes" and pend and pend[0] == want and time.time() - pend[1] < 90:
                self.pending_confirm.pop(chat_id, None)
                if want == "halt":
                    HALT_FILE.write_text(f"halted from telegram {dt.datetime.now().isoformat(timespec='seconds')}\n")
                else:
                    HALT_FILE.unlink(missing_ok=True)
                self.j.add_event("config", f"{want} from telegram")
                return "Halted: no new entries; exits, stops and the watcher keep running." if want == "halt" else "Resumed: entries enabled."
            self.pending_confirm[chat_id] = (want, time.time())
            return f"Confirm with `/{want} yes` within 90 seconds." + (" Currently HALTED." if HALT_FILE.exists() else " Currently trading.")
        if cmd == "/style":
            from .styles import STYLES, get_style, set_style
            if arg.lower() in STYLES:
                set_style(self.j, arg.lower(), source="telegram")
                return f"Style set to {arg.lower()}; applies from the next run."
            return f"Current style: {get_style(self.j)}. Use /style defensive | balanced | aggressive."
        if cmd == "/disarm":
            from .entries import EntryBook
            u = arg.upper()
            if not u:
                return "Usage: /disarm SYMBOL"
            book = EntryBook(self.j)
            if u not in book.all():
                return f"{u} is not armed."
            book.disarm(u)
            self.j.add_event("action", f"{u} disarmed from telegram")
            return f"{u} disarmed."
        if cmd == "/hypo":
            from .lab import Lab
            if len(arg) < 40:
                return "Say what happens, when, to what, over what horizon (at least 40 characters)."
            r = Lab(self.j).propose(arg[:80], arg, origin="operator", origin_ref=f"telegram {chat_id}")
            if r.get("error"):
                return "Not recorded: " + r["error"]
            self.j.add_event("lab", f"hypothesis #{r['id']} proposed from telegram: {arg[:100]}")
            return f"Hypothesis #{r['id']} recorded. The agent will write the spec and test it in a LAB run tonight; results land on the Brain page and in /lab."
        if cmd == "/lab":
            from .lab import Lab
            return Lab(self.j).index_text(15)
        if cmd == "/mute":
            try:
                mins = int(arg or "60")
            except ValueError:
                return "Usage: /mute MINUTES"
            mute(mins)
            return f"Alerts muted for {mins} minutes (replies to your requests still come through)."
        if cmd == "/unmute":
            unmute()
            return "Alerts unmuted."
        if cmd == "/notify":
            d = load_state()
            if arg:
                parts = arg.split()
                if len(parts) == 2 and parts[0] in KINDS and parts[1].lower() in ("on", "off"):
                    set_notify(parts[0], parts[1].lower() == "on")
                    d = load_state()
                else:
                    return "Usage: /notify KIND on|off  (kinds: " + ", ".join(KINDS) + ")"
            return "\n".join(f"{'✓' if d['notify'].get(k) else '✗'} {k}: {desc}" for k, desc in KINDS.items())
        if cmd.startswith("/") and cmd not in ("/ask",):
            return "Unknown command. " + HELP
        question = arg if cmd == "/ask" else text
        tid = self.j.add_task(question[:1500], priority=0, kind="operator")
        self.j.set(f"telegram:task:{tid}", chat_id)
        self.j.add_event("config", f"operator request #{tid} queued from telegram: {question[:120]}")
        return f"Queued as request #{tid}. The agent answers on its next run; I will send it here."

    # ---- polling
    def poll_once(self, timeout: int = 50) -> int:
        d = _post("getUpdates", {"offset": self.offset, "timeout": timeout, "allowed_updates": ["message"]}, timeout=timeout + 10)
        n = 0
        for upd in d.get("result", []):
            self.offset = max(self.offset, int(upd["update_id"]) + 1)
            msg = upd.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            if chat is None:
                continue
            try:
                reply = self.handle_text(int(chat), msg.get("text") or "")
            except Exception as e:
                log.exception("telegram command failed")
                reply = f"error: {type(e).__name__}: {str(e)[:120]}"
            if reply:
                send(int(chat), reply)
            n += 1
        return n

    def poll_forever(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        log.info("telegram poller started")
        backoff = 5
        while not should_stop():
            if not configured():
                time.sleep(30)
                continue
            try:
                self.poll_once()
                backoff = 5
            except Exception as e:
                log.warning("telegram poll failed: %s; retry in %ds", str(e)[:120], backoff)
                time.sleep(backoff)
                backoff = min(120, backoff * 2)
