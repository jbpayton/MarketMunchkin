"""Search / data providers behind the research tools: Tavily, Finnhub, SearXNG.

Keys come from .env (read lazily so the dashboard can change them without a restart); provider
settings (enabled, order, monthly budgets) live in data/search.json. Usage is metered per month.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from typing import Any

import httpx
from dotenv import dotenv_values

from .config import DATA_DIR, ROOT, SETTINGS

log = logging.getLogger("munchkin.providers")

SEARCH_CONFIG_FILE = DATA_DIR / "search.json"
ENV_FILE = ROOT / ".env"
KEY_NAMES = {"tavily": "TAVILY_API_KEY", "finnhub": "FINNHUB_API_KEY", "brave": "BRAVE_API_KEY"}
DEFAULT_CONFIG = {
    "order": ["tavily", "searxng"],                      # web/news search chain, first that returns results wins (merged with the next on news)
    "enabled": {"tavily": True, "searxng": True, "finnhub": True, "brave": False},
    "budgets": {"tavily": 900, "brave": 1800, "finnhub": 50000},   # calls per calendar month
    "merge_news": True,                                   # for news queries, merge the first two providers' results
}
_cfg_cache: tuple[float, dict] | None = None


# ------------------------------------------------------------------ config / keys / usage
def load_config() -> dict[str, Any]:
    global _cfg_cache
    if _cfg_cache and time.time() - _cfg_cache[0] < 20:
        return _cfg_cache[1]
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if SEARCH_CONFIG_FILE.exists():
        try:
            saved = json.loads(SEARCH_CONFIG_FILE.read_text())
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            log.warning("search.json unreadable: %s", e)
    _cfg_cache = (time.time(), cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    global _cfg_cache
    SEARCH_CONFIG_FILE.write_text(json.dumps(cfg, indent=1))
    _cfg_cache = None


def get_key(provider: str) -> str:
    name = KEY_NAMES.get(provider)
    if not name:
        return ""
    vals = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    return (vals.get(name) or "").strip()


def set_key(provider: str, value: str | None) -> None:
    """Write/clear a key in .env, preserving other lines and 0600 permissions. Never logs the value."""
    name = KEY_NAMES[provider]
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if not l.strip().startswith(name + "=")]
    if value:
        lines.append(f"{name}={value.strip()}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    try:
        ENV_FILE.chmod(0o600)
    except Exception:
        pass


def masked_key(provider: str) -> str | None:
    k = get_key(provider)
    if not k:
        return None
    return ("*" * max(0, len(k) - 4)) + k[-4:]


def enabled(provider: str) -> bool:
    cfg = load_config()
    if provider == "searxng":
        return bool(cfg["enabled"].get("searxng", True))
    return bool(cfg["enabled"].get(provider, False)) and bool(get_key(provider))


def _usage_key(provider: str) -> str:
    return f"usage:{provider}:{dt.date.today().strftime('%Y-%m')}"


def usage(provider: str) -> int:
    try:
        from .journal import Journal
        return int(Journal().get(_usage_key(provider), 0) or 0)
    except Exception:
        return 0


def _count(provider: str) -> None:
    try:
        from .journal import Journal
        j = Journal()
        j.set(_usage_key(provider), int(j.get(_usage_key(provider), 0) or 0) + 1)
    except Exception:
        pass


def within_budget(provider: str) -> bool:
    b = load_config()["budgets"].get(provider)
    return b is None or usage(provider) < int(b)


def status() -> dict[str, Any]:
    cfg = load_config()
    out = {}
    for p in ("tavily", "finnhub", "brave", "searxng"):
        out[p] = {"enabled_flag": bool(cfg["enabled"].get(p, p == "searxng")), "has_key": bool(get_key(p)) if p != "searxng" else True,
                  "key_masked": masked_key(p) if p != "searxng" else None, "active": enabled(p) if p != "searxng" else bool(cfg["enabled"].get("searxng", True)),
                  "usage_month": usage(p), "budget": cfg["budgets"].get(p), "url": SETTINGS.searxng_url if p == "searxng" else None}
    out["order"] = cfg["order"]
    out["merge_news"] = cfg.get("merge_news", True)
    return out


# ------------------------------------------------------------------ Tavily
def tavily_extract(url: str, max_chars: int = 9000) -> str:
    """Fetch a page through Tavily's extract endpoint (their crawler gets past many bot walls). One call per URL."""
    key = get_key("tavily")
    if not key:
        raise RuntimeError("tavily key not set")
    r = httpx.post("https://api.tavily.com/extract", json={"urls": [url], "extract_depth": "basic"},
                   headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=40)
    _count("tavily")
    if r.status_code != 200:
        raise RuntimeError(f"tavily extract HTTP {r.status_code}: {r.text[:120]}")
    d = r.json()
    res = d.get("results") or []
    if not res:
        failed = d.get("failed_results") or []
        raise RuntimeError("tavily extract: " + (str(failed[0].get("error")) if failed else "no content"))
    text = (res[0].get("raw_content") or "").strip()
    return text[:max_chars]


def tavily_search(query: str, category: str = "general", max_results: int = 8, time_range: str | None = None) -> list[dict]:
    key = get_key("tavily")
    if not key:
        return []
    days = {"day": 1, "week": 7, "month": 30, "year": 365}.get(time_range or "", None)
    body: dict[str, Any] = {"query": query, "topic": "news" if category == "news" else "general", "search_depth": "basic",
                            "max_results": min(max_results, 10), "include_answer": False, "include_raw_content": False}
    if days and category == "news":
        body["days"] = days
    r = httpx.post("https://api.tavily.com/search", json=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=25)
    _count("tavily")
    if r.status_code != 200:
        raise RuntimeError(f"tavily HTTP {r.status_code}: {r.text[:120]}")
    out = []
    for res in r.json().get("results", []):
        out.append({"title": (res.get("title") or "")[:160], "url": res.get("url", ""),
                    "snippet": re.sub(r"\s+", " ", res.get("content") or "")[:600],
                    "date": (res.get("published_date") or "")[:16] or None, "engine": "tavily", "score": res.get("score")})
    return out


# ------------------------------------------------------------------ Finnhub
FINNHUB = "https://finnhub.io/api/v1"


def _fh(path: str, **params: Any) -> Any:
    key = get_key("finnhub")
    if not key:
        return None
    params["token"] = key
    r = httpx.get(f"{FINNHUB}{path}", params=params, timeout=20)
    _count("finnhub")
    if r.status_code == 429:
        raise RuntimeError("finnhub rate limit (60/min)")
    if r.status_code != 200:
        raise RuntimeError(f"finnhub HTTP {r.status_code}: {r.text[:100]}")
    return r.json()


def finnhub_company_news(symbol: str, days: int = 3, limit: int = 10) -> list[dict]:
    if not enabled("finnhub"):
        return []
    to = dt.date.today()
    data = _fh("/company-news", symbol=symbol.upper(), **{"from": (to - dt.timedelta(days=days)).isoformat(), "to": to.isoformat()}) or []
    out = []
    for a in data[:limit]:
        ts = dt.datetime.fromtimestamp(a.get("datetime", 0), tz=dt.timezone.utc)
        out.append({"time": ts.astimezone().strftime("%m-%d %H:%M"), "headline": (a.get("headline") or "")[:160], "source": a.get("source"),
                    "symbols": symbol.upper(), "url": a.get("url"), "summary": (a.get("summary") or "")[:240], "engine": "finnhub"})
    return out


def finnhub_market_news(limit: int = 12) -> list[dict]:
    if not enabled("finnhub"):
        return []
    data = _fh("/news", category="general") or []
    out = []
    for a in data[:limit]:
        ts = dt.datetime.fromtimestamp(a.get("datetime", 0), tz=dt.timezone.utc)
        out.append({"time": ts.astimezone().strftime("%m-%d %H:%M"), "headline": (a.get("headline") or "")[:160], "source": a.get("source"),
                    "symbols": (a.get("related") or "")[:30], "url": a.get("url"), "summary": (a.get("summary") or "")[:240], "engine": "finnhub"})
    return out


def finnhub_earnings_calendar(days_ahead: int = 100) -> dict[str, dict[str, Any]]:
    """All companies reporting in the window, one call. Returns symbol -> {date, hour, eps_estimate, ...}."""
    if not enabled("finnhub"):
        return {}
    to = dt.date.today() + dt.timedelta(days=days_ahead)
    data = _fh("/calendar/earnings", **{"from": dt.date.today().isoformat(), "to": to.isoformat()}) or {}
    out: dict[str, dict[str, Any]] = {}
    for e in data.get("earningsCalendar", []):
        sym = e.get("symbol")
        if not sym:
            continue
        if sym not in out or e.get("date", "") < out[sym]["date"]:
            out[sym] = {"date": e.get("date"), "hour": e.get("hour"), "eps_estimate": e.get("epsEstimate"), "revenue_estimate": e.get("revenueEstimate")}
    return out


def finnhub_metrics(symbol: str) -> dict[str, Any]:
    if not enabled("finnhub"):
        return {}
    data = _fh("/stock/metric", symbol=symbol.upper(), metric="all") or {}
    m = data.get("metric", {}) or {}
    keys = {"beta": "beta", "52WeekHigh": "hi52", "52WeekLow": "lo52", "peTTM": "pe_ttm", "psTTM": "ps_ttm", "epsGrowthTTMYoy": "eps_growth_yoy",
            "revenueGrowthTTMYoy": "rev_growth_yoy", "netProfitMarginTTM": "net_margin", "currentRatioQuarterly": "current_ratio",
            "totalDebt/totalEquityQuarterly": "debt_to_equity", "10DayAverageTradingVolume": "avg_vol_10d_m", "marketCapitalization": "mcap_m",
            "dividendYieldIndicatedAnnual": "div_yield"}
    return {v: m.get(k) for k, v in keys.items() if m.get(k) is not None}


def test_provider(provider: str) -> dict[str, Any]:
    t = time.time()
    try:
        if provider == "tavily":
            res = tavily_search("stock market today", "news", 3, "day")
            return {"ok": True, "detail": f"{len(res)} results in {time.time()-t:.1f}s; first: {res[0]['title'][:60] if res else '-'}"}
        if provider == "finnhub":
            q = _fh("/quote", symbol="SPY") or {}
            return {"ok": "c" in q, "detail": f"SPY quote {q.get('c')} in {time.time()-t:.1f}s"}
        if provider == "searxng":
            from .search import _searx
            d = _searx("stock market today", "news", "day")
            return {"ok": bool(d.get("results")), "detail": f"{len(d.get('results', []))} results; unresponsive: {d.get('unresponsive_engines')}"}
        if provider == "brave":
            return {"ok": False, "detail": "brave client not implemented yet"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:160]}"}
    return {"ok": False, "detail": "unknown provider"}


# ------------------------------------------------------------------ StockTwits (free, keyless): attention + crowd sentiment
def stocktwits_stream(symbol: str, limit: int = 30) -> dict[str, Any]:
    r = httpx.get(f"https://api.stocktwits.com/api/2/streams/symbol/{symbol.upper()}.json",
                  headers={"User-Agent": "MarketMunchkin/0.1 (personal research bot)"}, timeout=15)
    if r.status_code != 200:
        return {"error": f"stocktwits HTTP {r.status_code}"}
    d = r.json()
    msgs = d.get("messages", [])[:limit]
    bull = bear = 0
    rows = []
    newest = oldest = None
    for m in msgs:
        sent = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        bull += sent == "Bullish"
        bear += sent == "Bearish"
        ts = m.get("created_at", "")
        newest = newest or ts
        oldest = ts or oldest
        rows.append({"time": ts[5:16].replace("T", " "), "sentiment": sent or "-", "likes": (m.get("likes") or {}).get("total", 0),
                     "text": re.sub(r"\s+", " ", m.get("body") or "")[:160]})
    sym = d.get("symbol") or {}
    span_h = None
    try:
        t0 = dt.datetime.fromisoformat(oldest.replace("Z", "+00:00")); t1 = dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
        span_h = round((t1 - t0).total_seconds() / 3600, 1)
    except Exception:
        pass
    top = sorted(rows, key=lambda x: -x["likes"])[:5]
    return {"symbol": symbol.upper(), "watchers": sym.get("watchlist_count"), "messages": len(msgs), "span_hours_for_last_30": span_h,
            "msgs_per_hour": round(len(msgs) / span_h, 1) if span_h else None, "bullish": bull, "bearish": bear,
            "bull_ratio": round(bull / (bull + bear), 2) if (bull + bear) else None, "top_by_likes": top}
