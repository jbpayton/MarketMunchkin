"""Research-breadth tracking and the entry gate.

The tracker records what the model actually looked at during a session. Entry
tools consult `gate()` so capital cannot be committed to a name that was never
charted or grounded in news, or before a broad scan happened.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any
from dataclasses import dataclass, field

CATALYST_GRADES = ("confirmed", "speculative", "none")


@dataclass
class ResearchTracker:
    """What the agent has actually looked at. With continuous sessions a few minutes apart the evidence has to carry
    over: everything noted here is stamped and persisted through the journal, and the gate counts anything inside
    `window_hours` (default 3h), not just the current session."""
    charted: set[str] = field(default_factory=set)
    newsed: set[str] = field(default_factory=set)
    dossiers: set[str] = field(default_factory=set)
    screened: bool = False
    context_checked: bool = False
    min_charts: int = 4
    require_dossier: bool = True
    journal: Any = None
    window_hours: float = 3.0
    _stamps: dict[str, dict[str, str]] = field(default_factory=lambda: {"charted": {}, "newsed": {}, "dossiers": {}, "flags": {}})

    KEY = "research:state"

    def attach(self, journal: Any, window_hours: float | None = None) -> "ResearchTracker":
        """Load recent evidence from the journal (entries older than the window are dropped)."""
        self.journal = journal
        if window_hours is not None:
            self.window_hours = float(window_hours)
        try:
            raw = journal.get(self.KEY) or {}
        except Exception:
            raw = {}
        cutoff = (dt.datetime.now() - dt.timedelta(hours=self.window_hours)).isoformat(timespec="seconds")
        for kind in ("charted", "newsed", "dossiers"):
            fresh = {k: v for k, v in (raw.get(kind) or {}).items() if v >= cutoff}
            self._stamps[kind] = fresh
            getattr(self, kind).update(fresh)
        flags = {k: v for k, v in (raw.get("flags") or {}).items() if v >= cutoff}
        self._stamps["flags"] = flags
        self.screened = self.screened or "screened" in flags
        self.context_checked = self.context_checked or "context" in flags
        return self

    def _stamp(self, kind: str, key: str) -> None:
        self._stamps.setdefault(kind, {})[key] = dt.datetime.now().isoformat(timespec="seconds")
        if self.journal is not None:
            try:
                self.journal.set(self.KEY, self._stamps)
            except Exception:
                pass

    def note_chart(self, symbol: str) -> None:
        self.charted.add(symbol.upper())
        self._stamp("charted", symbol.upper())

    def note_news(self, symbols: list[str] | None) -> None:
        for s in symbols or []:
            self.newsed.add(s.upper())
            self._stamp("newsed", s.upper())

    def note_search(self, query: str) -> None:
        # a web search whose query mentions a ticker counts as grounding for it
        for tok in re.findall(r"\b[A-Z]{1,5}\b", query):
            self.newsed.add(tok)
            self._stamp("newsed", tok)

    def note_dossier(self, symbol: str) -> None:
        u = symbol.upper()
        self.dossiers.add(u)
        self.charted.add(u)
        self.newsed.add(u)
        for kind in ("dossiers", "charted", "newsed"):
            self._stamp(kind, u)

    def note_screen(self) -> None:
        self.screened = True
        self._stamp("flags", "screened")

    def note_context(self) -> None:
        self.context_checked = True
        self._stamp("flags", "context")

    def gate(self, underlying: str) -> list[str]:
        u = underlying.upper()
        v: list[str] = []
        w = f"in the last {self.window_hours:g}h"
        if not self.context_checked:
            v.append(f"state of the world not checked {w}: call get_market_context first")
        if not self.screened:
            v.append(f"no broad scan {w}: run get_setups or screen_stocks before entering anything")
        if len(self.charted) < self.min_charts:
            v.append(f"only {len(self.charted)} symbol(s) charted {w}; compare at least {self.min_charts} candidates (get_chart) before committing capital")
        if u not in self.charted:
            v.append(f"{u} has not been charted {w} (get_chart)")
        if u not in self.newsed:
            v.append(f"{u} has not been grounded in news {w} (get_news with symbols=[{u}] or web_search mentioning {u})")
        if self.require_dossier and u not in self.dossiers:
            v.append(f"no research dossier for {u} {w}: run research_symbol('{u}') before committing capital")
        return v


def normalize_grade(grade: str | None) -> str | None:
    if grade is None:
        return None
    g = grade.strip().lower()
    aliases = {"confirmed": "confirmed", "verified": "confirmed", "primary": "confirmed",
               "speculative": "speculative", "rumor": "speculative", "rumour": "speculative", "unconfirmed": "speculative",
               "none": "none", "no_catalyst": "none", "unexplained": "none", "technical": "none"}
    return aliases.get(g)
