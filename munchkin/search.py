"""Web research: SearXNG JSON API plus a readable page fetcher."""
from __future__ import annotations

import logging
import time
import json
import re

import httpx
import trafilatura

from .config import DATA_DIR, SETTINGS
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


CACHE_FILE = DATA_DIR / "search_cache.json"
CACHE_TTL_S = {"news": 20 * 60, "general": 60 * 60}


def _cache_load() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except Exception:
        return {}


def _cache_save(d: dict) -> None:
    try:
        cutoff = time.time() - 24 * 3600
        d = {k: v for k, v in d.items() if v.get("t", 0) > cutoff}
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(CACHE_FILE)
    except Exception as e:
        log.debug("search cache save failed: %s", e)


def web_search(query: str, category: str = "general", max_results: int = 8,
               time_range: str | None = None, use_cache: bool = True) -> list[dict]:
    """Provider chain (data/search.json), free providers first: Google News RSS and SearXNG (no keys), Brave (2,000/month
    free), Tavily last (1,000 credits/month, paced per day). News queries merge the first two providers with results.
    Identical queries are served from a shared cache (20 min news, 60 min general) so a hundred sessions a day do not
    burn a hundred credits on "stock market today"."""
    from . import providers as P
    key = json.dumps([query.strip().lower(), category, int(max_results), time_range or ""])
    cache = _cache_load()
    hit = cache.get(key)
    if use_cache and hit and time.time() - hit.get("t", 0) < CACHE_TTL_S.get(category, 1800):
        return hit["rows"]
    cfg = P.load_config()
    collected: list[dict] = []
    served: list[str] = []
    for name in cfg.get("order", ["googlenews", "searxng", "brave", "tavily"]):
        if name == "googlenews":
            if category != "news" or not P.enabled("googlenews"):
                continue
            fn = lambda: P.googlenews_search(query, max_results, time_range)
        elif name == "searxng":
            if not P.enabled("searxng"):
                continue
            fn = lambda: _searxng_search(query, category, max_results, time_range)
        elif name == "brave":
            if not P.enabled("brave") or not P.within_budget("brave"):
                continue
            fn = lambda: P.brave_search(query, category, max_results, time_range)
        elif name == "tavily":
            if not P.enabled("tavily") or not P.within_budget("tavily"):
                continue
            fn = lambda: P.tavily_search(query, category, max_results, time_range)
        else:
            continue
        if name in ("brave", "tavily") and collected:
            break   # paid or quota-bound providers are a last resort: never spent to "merge" a second source
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
    if out:
        cache = _cache_load()
        cache[key] = {"t": time.time(), "rows": out, "by": served}
        _cache_save(cache)
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
