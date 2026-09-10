"""Research-breadth tracking and the entry gate.

The tracker records what the model actually looked at during a session. Entry
tools consult `gate()` so capital cannot be committed to a name that was never
charted or grounded in news, or before a broad scan happened.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CATALYST_GRADES = ("confirmed", "speculative", "none")


@dataclass
class ResearchTracker:
    charted: set[str] = field(default_factory=set)
    newsed: set[str] = field(default_factory=set)
    dossiers: set[str] = field(default_factory=set)
    screened: bool = False
    context_checked: bool = False
    min_charts: int = 4
    require_dossier: bool = True

    def note_chart(self, symbol: str) -> None:
        self.charted.add(symbol.upper())

    def note_news(self, symbols: list[str] | None) -> None:
        for s in symbols or []:
            self.newsed.add(s.upper())

    def note_search(self, query: str) -> None:
        # a web search whose query mentions a ticker counts as grounding for it
        for tok in re.findall(r"\b[A-Z]{1,5}\b", query):
            self.newsed.add(tok)

    def note_dossier(self, symbol: str) -> None:
        u = symbol.upper()
        self.dossiers.add(u)
        self.charted.add(u)
        self.newsed.add(u)

    def note_screen(self) -> None:
        self.screened = True

    def note_context(self) -> None:
        self.context_checked = True

    def gate(self, underlying: str) -> list[str]:
        u = underlying.upper()
        v: list[str] = []
        if not self.context_checked:
            v.append("state of the world not checked this session: call get_market_context first")
        if not self.screened:
            v.append("no broad scan yet: run get_setups or screen_stocks before entering anything")
        if len(self.charted) < self.min_charts:
            v.append(f"only {len(self.charted)} symbol(s) charted this session; compare at least {self.min_charts} candidates (get_chart) before committing capital")
        if u not in self.charted:
            v.append(f"{u} has not been charted this session (get_chart)")
        if u not in self.newsed:
            v.append(f"{u} has not been grounded in news this session (get_news with symbols=[{u}] or web_search mentioning {u})")
        if self.require_dossier and u not in self.dossiers:
            v.append(f"no research dossier for {u} this session: run research_symbol('{u}') before committing capital")
        return v


def normalize_grade(grade: str | None) -> str | None:
    if grade is None:
        return None
    g = grade.strip().lower()
    aliases = {"confirmed": "confirmed", "verified": "confirmed", "primary": "confirmed",
               "speculative": "speculative", "rumor": "speculative", "rumour": "speculative", "unconfirmed": "speculative",
               "none": "none", "no_catalyst": "none", "unexplained": "none", "technical": "none"}
    return aliases.get(g)
