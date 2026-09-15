"""The world brief as a versioned document instead of a rewritten essay.

Full rewrites are allowed in the phases that rebuild the picture (pre-market, research, post-market, reflect). Intraday
and EVENT runs append timestamped developments to it. Every version is kept, and each run is shown what
changed since the agent's previous run, so the picture accumulates instead of churning.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from .util import now_et

HISTORY_KEY = "world_brief_history"
REWRITE_PHASES = {"premarket", "research", "postmarket", "reflect", "adhoc", "lab", "study"}
MAX_HISTORY = 40
SECTIONS = ["Regime", "Drivers", "Themes", "Calendar", "Risks", "Facts", "Developments"]


def current(journal: Any) -> tuple[str, str]:
    return (journal.get("world_brief") or ""), (journal.get("world_brief_ts") or "")


def history(journal: Any) -> list[dict[str, Any]]:
    return list(journal.get(HISTORY_KEY) or [])


def _push(journal: Any, text: str, session_id: int | None, kind: str) -> None:
    h = history(journal)
    h.append({"ts": now_et().isoformat(timespec="seconds"), "session_id": session_id, "kind": kind, "text": text})
    journal.set(HISTORY_KEY, h[-MAX_HISTORY:])


def rewrite(journal: Any, text: str, session_id: int | None) -> None:
    text = text.strip()
    journal.set("world_brief", text)
    journal.set("world_brief_ts", now_et().isoformat(timespec="seconds"))
    _push(journal, text, session_id, "rewrite")


def add_development(journal: Any, section: str, text: str, source: str, session_id: int | None) -> str:
    """Append one timestamped bullet under '## Developments' (created if missing). Returns the bullet."""
    body, _ = current(journal)
    bullet = f"- [{now_et().strftime('%m-%d %H:%M')}] ({section.strip()[:24]}) {text.strip()[:400]} — {source.strip()[:120]}"
    if "## Developments" in body:
        body = body.rstrip() + "\n" + bullet
    else:
        body = body.rstrip() + "\n\n## Developments\n" + bullet
    journal.set("world_brief", body.strip())
    _push(journal, body.strip(), session_id, "development")
    return bullet


def since(journal: Any, session_id: int | None) -> str:
    """Lines added or removed since the brief the agent saw in its previous run (by session id), capped."""
    h = history(journal)
    if len(h) < 2:
        return "(no earlier version to compare)"
    cur = h[-1]
    prev = None
    for v in reversed(h[:-1]):
        if session_id is None or (v.get("session_id") or 0) < (session_id or 0):
            prev = v
            break
    if prev is None:
        prev = h[-2]
    a = [l for l in prev["text"].splitlines() if l.strip()]
    b = [l for l in cur["text"].splitlines() if l.strip()]
    added = [l for l in difflib.ndiff(a, b) if l.startswith("+ ")]
    removed = [l for l in difflib.ndiff(a, b) if l.startswith("- ")]
    out = []
    if added:
        out.append("added:\n" + "\n".join("  " + l[2:][:220] for l in added[:12]) + (f"\n  … {len(added) - 12} more" if len(added) > 12 else ""))
    if removed:
        out.append("removed:\n" + "\n".join("  " + l[2:][:160] for l in removed[:6]) + (f"\n  … {len(removed) - 6} more" if len(removed) > 6 else ""))
    if not out:
        return f"unchanged since {prev['ts'][11:16]} (session {prev.get('session_id')})"
    return f"since {prev['ts'][11:16]} (session {prev.get('session_id')}, {cur['kind']} at {cur['ts'][11:16]}):\n" + "\n".join(out)


def rewrite_allowed(journal: Any, phase: str, min_gap_hours: float = 2.0) -> tuple[bool, str]:
    if phase in REWRITE_PHASES:
        return True, ""
    _, ts = current(journal)
    if not ts:
        return True, ""
    try:
        import datetime as dt
        age_h = (now_et() - dt.datetime.fromisoformat(ts)).total_seconds() / 3600
    except Exception:
        age_h = 99
    if age_h >= min_gap_hours:
        return True, ""
    return False, f"the brief was rebuilt {age_h:.1f}h ago; INTRADAY runs add developments (add_development) rather than rewrite. Rewrites happen pre-market, in research, and post-market."


def outline(text: str) -> list[str]:
    return [l.strip("# ").strip() for l in text.splitlines() if l.startswith("#")]


def missing_sections(text: str) -> list[str]:
    low = text.lower()
    return [s for s in SECTIONS[:6] if not re.search(r"#+\s*" + s.lower(), low) and s.lower() not in low[:400]]
