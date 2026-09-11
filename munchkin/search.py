"""Web research: SearXNG JSON API plus a readable page fetcher."""
from __future__ import annotations

import logging
import re

import httpx
import trafilatura

from .config import SETTINGS
from . import providers as P

log = logging.getLogger("munchkin.search")

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")


def _searx(query: str, category: str, time_range: str | None) -> dict:
    params = {"q": query, "format": "json", "categories": category, "language": "en-US"}
    if time_range:
        params["time_range"] = time_range
    r = httpx.get(f"{SETTINGS.searxng_url.rstrip('/')}/search", params=params, timeout=40.0)
    r.raise_for_status()
    return r.json()


def _dedupe(rows: list[dict], max_results: int) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        u = (r.get("url") or "").rstrip("/")
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(r)
        if len(out) >= max_results:
            break
    return out


def _searxng_search(query: str, category: str, max_results: int, time_range: str | None) -> list[dict]:
    attempts = [(category, time_range)]
    if time_range:
        attempts.append((category, None))
    other = "news" if category != "news" else "general"
    attempts += [(other, time_range), (other, None)] if time_range else [(other, None)]
    data: dict = {}
    for cat, tr in attempts:
        try:
            data = _searx(query, cat, tr)
        except Exception as e:
            log.warning("searxng %s/%s failed: %s", cat, tr, e)
            continue
        if data.get("results"):
            break
    out: list[dict] = []
    for res in data.get("results", []):
        out.append({"title": (res.get("title") or "").strip()[:160], "url": res.get("url", ""),
                    "snippet": re.sub(r"\s+", " ", (res.get("content") or "")).strip()[:400],
                    "date": (res.get("publishedDate") or "")[:16] or None, "engine": res.get("engine")})
    for a in data.get("answers", [])[:2]:
        out.insert(0, {"title": "answer", "url": "", "snippet": str(a)[:400], "date": None, "engine": "answer"})
    return out


def web_search(query: str, category: str = "general", max_results: int = 8,
               time_range: str | None = None) -> list[dict]:
    """Provider chain (data/search.json): first enabled provider with results wins; news queries merge the
    first two so paid and free sources complement each other. Budgets are enforced per calendar month."""
    from . import providers as P
    cfg = P.load_config()
    collected: list[dict] = []
    served: list[str] = []
    for name in cfg.get("order", ["tavily", "searxng"]):
        if name == "searxng":
            if not cfg["enabled"].get("searxng", True):
                continue
            fn = lambda: _searxng_search(query, category, max_results, time_range)
        elif name == "tavily":
            if not P.enabled("tavily") or not P.within_budget("tavily"):
                continue
            fn = lambda: P.tavily_search(query, category, max_results, time_range)
        else:
            continue
        try:
            rows = fn()
        except Exception as e:
            log.warning("provider %s failed: %s", name, e)
            continue
        if rows:
            collected += rows
            served.append(name)
            if category != "news" or not cfg.get("merge_news", True) or len(served) >= 2:
                break
    out = _dedupe(collected, max_results)
    if served:
        log.info("web_search served by %s (%d results)", "+".join(served), len(out))
    return out


def fetch_page(url: str, max_chars: int = 6000) -> str:
    """Download a page and return its main readable text. Falls back to Tavily's extract API when the
    direct fetch is blocked, errors, or comes back too thin (paywall/JS shells)."""
    text = _fetch_direct(url, max_chars)
    thin = text.startswith("ERROR") or len(text) < 400
    if thin and P.enabled("tavily") and P.get_key("tavily") and P.within_budget("tavily"):
        try:
            alt = P.tavily_extract(url, max_chars=max_chars)
            if len(alt) >= 200 and (text.startswith("ERROR") or len(alt) > len(text) * 1.5):
                log.info("fetch_page %s served by tavily extract (%d chars)", url, len(alt))
                return "(fetched via Tavily extract; direct fetch was blocked or thin)\n" + alt + ("\n...[page truncated]" if len(alt) >= max_chars else "")
        except Exception as e:
            log.info("tavily extract fallback failed for %s: %s", url, e)
            if text.startswith("ERROR"):
                text += f"; Tavily extract also failed: {str(e)[:100]}"
    return text


def _fetch_direct(url: str, max_chars: int) -> str:
    try:
        r = httpx.get(url, timeout=25.0, follow_redirects=True,
                      headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
    except Exception as e:  # network errors, bad URLs
        return f"ERROR fetching {url}: {type(e).__name__}: {e}"
    if r.status_code >= 400:
        return f"ERROR fetching {url}: HTTP {r.status_code} (site may block bots; try another source or rely on search snippets)"
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype and "json" not in ctype:
        return f"ERROR: unsupported content-type {ctype}"
    text = trafilatura.extract(r.text, include_comments=False, include_tables=True,
                               favor_precision=False, url=url) or ""
    if len(text) < 200:
        # crude fallback
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "lxml")
        for t in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            t.decompose()
        text = re.sub(r"\n{2,}", "\n", soup.get_text("\n")).strip()
    if len(text) < 100:
        return f"ERROR: page returned no readable text (likely JS-rendered or bot-blocked): {url}"
    text = re.sub(r"[ \t]+", " ", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[page truncated]"
    return text
