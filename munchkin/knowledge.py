"""Durable knowledge library filled by off-hours study sessions.

Each topic is a markdown file under data/knowledge/<slug>.md with a small front matter block. The system prompt shows the
index (title + one-line summary) so the agent knows what it already understands; get_knowledge returns the full note.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from .config import DATA_DIR

KNOWLEDGE_DIR = DATA_DIR / "knowledge"
MAX_BODY = 6000

# The curriculum: what a trader running this book should understand. STUDY runs rotate through it (least recently
# studied first) unless the agent has noticed a more pressing gap. (slug, title, why it matters here)
CURRICULUM: list[tuple[str, str, str]] = [
    ("inflation-prints", "CPI, PPI and PCE: how the prints are built and how markets trade them", "the machine trades around these releases"),
    ("fed-reaction-function", "The Fed's reaction function: dots, pressers, QT, the reverse repo facility", "rates drive the regime score"),
    ("yield-curve", "The yield curve, inversions and what 10y-3m has predicted", "the rates dial"),
    ("credit-spreads", "Credit spreads (HY vs IG) as an equity risk gauge", "the credit dial"),
    ("dollar-and-flows", "The dollar, global liquidity and cross-border flows into US equities", "the dollar dial"),
    ("oil-and-inflation", "Oil shocks, OPEC and how energy feeds inflation expectations", "the oil dial"),
    ("labor-market", "Payrolls, claims, JOLTS and the unemployment rate as leading/lagging signals", "monthly catalysts"),
    ("earnings-season", "Earnings season mechanics: guidance, whisper numbers, post-earnings drift", "single-name catalysts"),
    ("options-market-structure", "Options market structure: dealer gamma, 0DTE, pin risk, IV crush", "we buy options; we must not be the dumb money"),
    ("iv-and-expected-move", "Implied vs realized volatility, expected move, skew and term structure", "how we choose single vs vertical"),
    ("market-breadth", "Breadth, participation and the equal-weight vs cap-weight tell", "the breadth/participation dials"),
    ("sector-rotation", "Sector rotation through the business cycle", "where to look for leaders"),
    ("semiconductor-cycle", "The semiconductor cycle, AI capex and the supply chain", "the largest sector in the universe"),
    ("consumer-and-housing", "Consumer credit, housing and retail sales as demand gauges", "consumer discretionary exposure"),
    ("treasury-supply", "Treasury auctions, the refunding and how supply moves long yields", "rates shocks"),
    ("etf-flows-and-rebalancing", "ETF flows, index rebalancing and inclusion effects", "mechanical flows create setups"),
    ("short-interest", "Short interest, squeezes and days-to-cover", "asymmetric setups and traps"),
    ("buybacks-and-blackouts", "Buybacks, blackout windows and their effect on dips", "bid under the market"),
    ("tariffs-and-trade", "Tariffs, trade policy and sector winners/losers", "policy catalysts"),
    ("fiscal-and-deficits", "Fiscal policy, deficits and the term premium", "the macro backdrop"),
    ("volatility-regimes", "Volatility regimes: VIX term structure, vol-of-vol, what a spike means for the next weeks", "the volatility dial"),
    ("seasonality", "Seasonality: month-end, quarter-end, opex, holidays, the September effect", "calendar tendencies"),
    ("event-day-patterns", "How CPI day, FOMC day and payrolls day usually trade (pre, print, follow-through)", "we hold through prints"),
    ("reading-the-open", "Reading the first 30 minutes: gaps, opening range, relative volume, VWAP", "our intraday entries"),
    ("small-caps-and-financing", "Small caps, financing conditions and why they lead or lag", "the size dial"),
    ("geopolitics-and-markets", "Geopolitical shocks and how markets have historically absorbed them", "headline risk"),
    ("crypto-as-risk-gauge", "Crypto as a risk-appetite gauge and its correlation with tech", "the crypto dial"),
    ("cash-account-mechanics", "Cash-account mechanics: T+1 settlement, good-faith violations, options assignment and exercise", "our own plumbing"),
]


def slugify(topic: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (topic or "").lower()).strip("-")
    return s[:60] or "untitled"


class KnowledgeBase:
    def __init__(self, root: Path = KNOWLEDGE_DIR) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ read
    def _parse(self, p: Path) -> dict[str, Any]:
        txt = p.read_text(encoding="utf-8")
        meta: dict[str, Any] = {"slug": p.stem, "title": p.stem, "updated": "", "sources": []}
        body = txt
        if txt.startswith("---\n"):
            end = txt.find("\n---\n", 4)
            if end > 0:
                for line in txt[4:end].splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k, v = k.strip(), v.strip()
                        if k == "sources":
                            meta["sources"] = [x.strip() for x in v.split("|") if x.strip()]
                        elif k in ("title", "updated", "summary"):
                            meta[k] = v
                body = txt[end + 5:]
        meta["body"] = body.strip()
        if not meta.get("summary"):
            meta["summary"] = meta["body"].split("\n", 1)[0][:200]
        return meta

    def index(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("*.md")):
            try:
                m = self._parse(p)
                out.append({"slug": m["slug"], "title": m["title"], "updated": m["updated"], "summary": m["summary"][:200], "sources": len(m["sources"]), "chars": len(m["body"])})
            except Exception:
                continue
        return sorted(out, key=lambda x: x["updated"], reverse=True)

    def get(self, topic: str) -> dict[str, Any] | None:
        p = self.root / f"{slugify(topic)}.md"
        if not p.exists():
            # fuzzy: title contains the words
            words = [w for w in re.split(r"\W+", topic.lower()) if len(w) > 2]
            for cand in self.index():
                hay = (cand["title"] + " " + cand["slug"]).lower()
                if words and all(w in hay for w in words):
                    p = self.root / f"{cand['slug']}.md"
                    break
            else:
                return None
        return self._parse(p)

    def index_text(self, limit: int = 40) -> str:
        rows = self.index()[:limit]
        if not rows:
            return "(empty — off-hours STUDY runs fill this in)"
        return "\n".join(f"- {r['slug']}: {r['title']} — {r['summary'][:120]} [{r['updated'][:10]}]" for r in rows)

    # ------------------------------------------------------------------ write
    def save(self, topic: str, title: str, body: str, sources: list[str], summary: str | None = None) -> Path:
        slug = slugify(topic)
        body = (body or "").strip()[:MAX_BODY]
        summary = (summary or body.split("\n", 1)[0])[:200].replace("\n", " ")
        srcs = " | ".join(s.strip() for s in sources if s and s.strip())[:2000]
        p = self.root / f"{slug}.md"
        p.write_text(f"---\ntitle: {title.strip()[:140]}\nupdated: {dt.datetime.now().isoformat(timespec='minutes')}\nsummary: {summary}\nsources: {srcs}\n---\n{body}\n", encoding="utf-8")
        return p

    # ------------------------------------------------------------------ curriculum
    def next_topics(self, n: int = 3) -> list[dict[str, str]]:
        have = {r["slug"]: r["updated"] for r in self.index()}
        ranked = sorted(CURRICULUM, key=lambda c: have.get(c[0], ""))  # never studied ("") first, then oldest
        return [{"slug": s, "title": t, "why": w, "last": have.get(s, "never")} for s, t, w in ranked[:n]]
