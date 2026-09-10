"""Primary-source macro data: BLS public API and release pages, Fed RSS/FOMC calendar, market dashboard via yfinance.
Everything here is free and keyless. Numbers returned carry their source and date so the agent can cite them."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
import xml.etree.ElementTree as ET_
from typing import Any

import httpx
import trafilatura

log = logging.getLogger("munchkin.macro")

_CONTACT = os.environ.get("MUNCHKIN_CONTACT", "").strip()
BLS_UA = f"MarketMunchkin/0.1 (personal research bot{'; contact: ' + _CONTACT if _CONTACT else ''})"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

BLS_SERIES = {
    "cpi": ("CUSR0000SA0", "CPI-U all items, SA index"),
    "core_cpi": ("CUSR0000SA0L1E", "CPI-U less food & energy, SA index"),
    "ppi": ("WPSFD4", "PPI final demand, SA index"),
    "core_ppi": ("WPSFD49104", "PPI final demand less foods, energy, trade, SA index"),
    "unemployment": ("LNS14000000", "Unemployment rate, %"),
    "payrolls": ("CES0000000001", "Nonfarm payrolls, thousands"),
    "hourly_earnings": ("CES0500000003", "Avg hourly earnings, private, $"),
}
BLS_RELEASES = {
    "cpi": "https://www.bls.gov/news.release/cpi.nr0.htm",
    "ppi": "https://www.bls.gov/news.release/ppi.nr0.htm",
    "jobs": "https://www.bls.gov/news.release/empsit.nr0.htm",
    "jolts": "https://www.bls.gov/news.release/jolts.nr0.htm",
    "eci": "https://www.bls.gov/news.release/eci.nr0.htm",
}
MARKET_TICKERS = {
    "us10y_yield": "^TNX", "us5y_yield": "^FVX", "us3m_yield": "^IRX", "wti_oil": "CL=F", "gold": "GC=F", "copper": "HG=F",
    "dollar_index": "DX-Y.NYB", "vix": "^VIX", "es_futures": "ES=F", "nq_futures": "NQ=F", "bitcoin": "BTC-USD",
}

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


# ------------------------------------------------------------------ BLS API
def bls_series(keys: list[str] | None = None) -> dict[str, Any]:
    keys = [k for k in (keys or list(BLS_SERIES)) if k in BLS_SERIES]

    def fetch():
        ids = [BLS_SERIES[k][0] for k in keys]
        r = httpx.post("https://api.bls.gov/publicAPI/v1/timeseries/data/", json={"seriesid": ids},
                       headers={"Content-type": "application/json", "User-Agent": BLS_UA}, timeout=25)
        d = r.json()
        if d.get("status") != "REQUEST_SUCCEEDED":
            return {"error": f"BLS API: {d.get('status')} {d.get('message')}"}
        out: dict[str, Any] = {"source": "BLS public API v1 (api.bls.gov)", "fetched": dt.datetime.now().isoformat(timespec="minutes")}
        by_id = {s["seriesID"]: s["data"] for s in d.get("Results", {}).get("series", [])}
        for k in keys:
            sid, label = BLS_SERIES[k]
            data = [x for x in by_id.get(sid, []) if x.get("period", "").startswith("M") and x["period"] != "M13"]
            if not data:
                out[k] = {"label": label, "error": "no data"}
                continue
            latest = data[0]
            val = float(latest["value"])
            rec: dict[str, Any] = {"label": label, "series": sid, "latest_period": f"{latest['periodName']} {latest['year']}", "value": val}
            if len(data) > 1:
                prev = float(data[1]["value"])
                rec["prev_value"] = prev
                rec["mom_pct"] = round((val / prev - 1) * 100, 2) if prev else None
                rec["mom_change"] = round(val - prev, 3)
            if len(data) > 12:
                yago = float(data[12]["value"])
                rec["yoy_pct"] = round((val / yago - 1) * 100, 2) if yago else None
            if k in ("unemployment",):
                rec.pop("mom_pct", None)
                rec.pop("yoy_pct", None)
            if k == "payrolls" and len(data) > 1:
                rec["mom_change_thousands"] = rec.pop("mom_change")
                rec.pop("mom_pct", None)
            out[k] = rec
        return out

    return _cached("bls:" + ",".join(keys), 6 * 3600, fetch)


def bls_release(name: str, max_chars: int = 2600) -> str:
    url = BLS_RELEASES.get(name.lower())
    if not url:
        return f"unknown release '{name}'; choose one of {list(BLS_RELEASES)}"

    def fetch():
        r = httpx.get(url, headers={"User-Agent": BLS_UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            return f"BLS page returned HTTP {r.status_code} (BLS blocks unidentified bots; set MUNCHKIN_CONTACT=email in .env)"
        txt = trafilatura.extract(r.text, include_tables=False) or ""
        txt = re.sub(r"[ \t]+", " ", txt)
        i = txt.find("Transmission of material")
        if i > 0:
            txt = txt[i:]
        return f"SOURCE: {url} (fetched {dt.datetime.now():%Y-%m-%d %H:%M})\n" + txt[:max_chars]

    return _cached("blsrel:" + name, 3600, fetch)


def bls_schedule(days_ahead: int = 21) -> str:
    """Upcoming BLS releases from the official per-release schedule pages."""
    pages = {"CPI": "cpi", "PPI": "ppi", "Employment Situation (jobs)": "empsit", "JOLTS": "jolts", "Employment Cost Index": "eci",
             "Real Earnings": "realer", "Productivity": "prod2"}

    def fetch():
        today = dt.date.today()
        out: list[tuple[dt.date, str]] = []
        for label, slug in pages.items():
            try:
                r = httpx.get(f"https://www.bls.gov/schedule/news_release/{slug}.htm", headers={"User-Agent": BLS_UA}, timeout=12)
                if r.status_code != 200:
                    continue
                txt = trafilatura.extract(r.text, include_tables=True) or ""
                for m in re.finditer(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})\s*\|\s*(\d{1,2}:\d{2}\s*[AP]M)", txt):
                    mon = m.group(1)[:3]
                    try:
                        d = dt.datetime.strptime(f"{mon} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
                    except ValueError:
                        continue
                    if today <= d <= today + dt.timedelta(days=days_ahead):
                        out.append((d, f"{d.isoformat()} ({d:%a}) {m.group(4)} ET: {label}"))
            except Exception as e:
                log.warning("bls schedule %s: %s", slug, e)
        out.sort()
        return "SOURCE: bls.gov release schedules\n" + ("\n".join(x[1] for x in out) if out else "(no releases in window)")

    return _cached("blssched", 12 * 3600, fetch)


# ------------------------------------------------------------------ Fed
def fed_monetary_rss(n: int = 6) -> list[dict[str, str]]:
    def fetch():
        r = httpx.get("https://www.federalreserve.gov/feeds/press_monetary.xml", headers={"User-Agent": BROWSER_UA}, timeout=15)
        root = ET_.fromstring(r.content)
        items = []
        for it in root.iter("item"):
            items.append({"date": (it.findtext("pubDate") or "")[:16], "title": (it.findtext("title") or "").strip()[:140],
                          "link": (it.findtext("link") or "").strip()})
        return items[:n]

    return _cached("fedrss", 3600, fetch)


def fomc_calendar() -> str:
    def fetch():
        r = httpx.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", headers={"User-Agent": BROWSER_UA}, timeout=15)
        txt = trafilatura.extract(r.text, include_tables=True) or ""
        year = str(dt.date.today().year)
        i = txt.find(year)
        chunk = re.sub(r"\s+", " ", txt[i:i + 1400]) if i >= 0 else txt[:800]
        return f"SOURCE: federalreserve.gov FOMC calendar\n{chunk}"

    return _cached("fomccal", 24 * 3600, fetch)


def fed_statement(max_chars: int = 2500) -> str:
    items = fed_monetary_rss(10)
    stmt = next((i for i in items if "statement" in i["title"].lower()), items[0] if items else None)
    if not stmt:
        return "no Fed press releases found"

    def fetch():
        r = httpx.get(stmt["link"], headers={"User-Agent": BROWSER_UA}, timeout=15, follow_redirects=True)
        txt = trafilatura.extract(r.text) or ""
        return f"SOURCE: {stmt['link']} ({stmt['date']})\n{txt[:max_chars]}"

    return _cached("fedstmt:" + stmt["link"], 6 * 3600, fetch)


# ------------------------------------------------------------------ market dashboard
def market_dashboard() -> dict[str, Any]:
    def fetch():
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance as yf
        out: dict[str, Any] = {"source": "Yahoo Finance via yfinance", "as_of": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
        try:
            data = yf.download(list(MARKET_TICKERS.values()), period="6d", interval="1d", progress=False, group_by="ticker", threads=True)
        except Exception as e:
            return {"error": str(e)[:120]}
        for name, tk in MARKET_TICKERS.items():
            try:
                c = data[tk]["Close"].dropna()
                last, prev = float(c.iloc[-1]), float(c.iloc[-2])
                rec = {"last": round(last, 2), "chg_1d_pct": round((last / prev - 1) * 100, 2)}
                if len(c) >= 5:
                    rec["chg_5d_pct"] = round((last / float(c.iloc[-5]) - 1) * 100, 2)
                if "yield" in name:
                    rec["chg_1d_bp"] = round((last - prev) * 100, 1)
                out[name] = rec
            except Exception:
                out[name] = {"error": "n/a"}
        return out

    return _cached("mktdash", 600, fetch)


def format_macro_data(d: dict[str, Any]) -> str:
    lines = [f"BLS data ({d.get('source')}, fetched {d.get('fetched')}):"]
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        if "error" in v:
            lines.append(f"- {k}: {v['error']}")
            continue
        extra = []
        if v.get("mom_pct") is not None:
            extra.append(f"m/m {v['mom_pct']:+.2f}%")
        if v.get("mom_change_thousands") is not None:
            extra.append(f"m/m {v['mom_change_thousands']:+.0f}k")
        if v.get("yoy_pct") is not None:
            extra.append(f"y/y {v['yoy_pct']:+.2f}%")
        lines.append(f"- {k} ({v['label']}): {v['value']} for {v['latest_period']}" + (f" | {' | '.join(extra)}" if extra else "") + (f" | prev {v.get('prev_value')}" if v.get('prev_value') is not None else ""))
    return "\n".join(lines)


def format_dashboard(d: dict[str, Any]) -> str:
    if "error" in d:
        return f"market dashboard error: {d['error']}"
    parts = []
    for k, v in d.items():
        if isinstance(v, dict) and "last" in v:
            parts.append(f"{k} {v['last']} ({v['chg_1d_pct']:+.2f}% 1d" + (f", {v['chg_5d_pct']:+.2f}% 5d" if "chg_5d_pct" in v else "") + (f", {v['chg_1d_bp']:+.1f}bp" if "chg_1d_bp" in v else "") + ")")
    return f"Rates/commodities/futures ({d.get('source')}, {d.get('as_of')}): " + "; ".join(parts)
