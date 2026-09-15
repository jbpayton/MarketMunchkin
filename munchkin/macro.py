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
    "dollar_index": "DX-Y.NYB", "vix": "^VIX", "vix3m": "^VIX3M", "es_futures": "ES=F", "nq_futures": "NQ=F", "bitcoin": "BTC-USD",
    # cross-asset tells (ETF proxies): credit appetite, size, growth vs broad, participation, duration
    "spy": "SPY", "qqq": "QQQ", "iwm": "IWM", "rsp": "RSP", "hyg": "HYG", "lqd": "LQD", "tlt": "TLT",
}
# tape keys shown to the model in the compact dashboard (the ETF proxies feed the dials instead)
TAPE_SHOW = ["us10y_yield", "us5y_yield", "us3m_yield", "wti_oil", "gold", "copper", "dollar_index", "vix", "es_futures", "nq_futures", "bitcoin"]

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
        chunk = re.sub(r"\s+", " ", txt[i:i + 3200]) if i >= 0 else txt[:1200]
        return f"SOURCE: federalreserve.gov FOMC calendar\n{chunk}"

    return _cached("fomccal", 24 * 3600, fetch)


_MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}


def fomc_dates(n: int = 6) -> list[str]:
    """Decision days (last day of each scheduled meeting) from the Fed's calendar page markup, this year and next.
    Reads the HTML directly: the readable-text extraction drops meetings that have no minutes yet, i.e. the future ones."""
    def fetch():
        r = httpx.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", headers={"User-Agent": BROWSER_UA}, timeout=15)
        return r.text
    html = _cached("fomchtml", 24 * 3600, fetch)
    this_year = dt.date.today().year
    panels = [(int(m.group(1)), m.end()) for m in re.finditer(r"(20\d\d) FOMC Meetings", html)]
    out: list[str] = []
    for k, (year, pos) in enumerate(panels):
        if year not in (this_year, this_year + 1):
            continue
        seg = html[pos:panels[k + 1][1] if k + 1 < len(panels) else len(html)]
        for mm in re.finditer(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>\s*</div>\s*<div class="fomc-meeting__date[^>]*>([^<]+)<', seg):
            months, days = mm.group(1).strip(), mm.group(2).strip()
            if "notation" in days.lower():
                continue
            mon_name = months.split("/")[-1].strip()[:3].lower()
            mon = next((v for k2, v in _MONTHS.items() if k2[:3].lower() == mon_name), None)
            day_m = re.findall(r"\d{1,2}", days)
            if not mon or not day_m:
                continue
            try:
                out.append(dt.date(year, mon, int(day_m[-1])).isoformat())
            except ValueError:
                continue
    today = dt.date.today().isoformat()
    upcoming = sorted(d for d in set(out) if d >= today)     # the next n decision days, not the first n of the year
    return upcoming[:n]


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
            data = yf.download(list(MARKET_TICKERS.values()), period="2mo", interval="1d", progress=False, group_by="ticker", threads=True)
        except Exception as e:
            return {"error": str(e)[:120]}
        for name, tk in MARKET_TICKERS.items():
            try:
                c = data[tk]["Close"].dropna()
                last, prev = float(c.iloc[-1]), float(c.iloc[-2])
                rec = {"last": round(last, 2), "chg_1d_pct": round((last / prev - 1) * 100, 2)}
                if len(c) >= 5:
                    rec["chg_5d_pct"] = round((last / float(c.iloc[-5]) - 1) * 100, 2)
                if len(c) >= 21:
                    rec["chg_20d_pct"] = round((last / float(c.iloc[-21]) - 1) * 100, 2)
                if "yield" in name:
                    rec["chg_1d_bp"] = round((last - prev) * 100, 1)
                    if len(c) >= 21:
                        rec["chg_20d_bp"] = round((last - float(c.iloc[-21])) * 100, 1)
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


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def world_dials(tape: dict[str, Any], regime: dict[str, Any] | None, vix: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Cross-asset 'state of the world' dials. Each is scored -1 (risk-off / headwind) .. +1 (risk-on / tailwind)
    from the 20-day tape so the dashboard can draw them as diverging bars. Pure function of cached data."""
    t = lambda k, f="chg_20d_pct": (tape.get(k) or {}).get(f)  # noqa: E731
    dials: list[dict[str, Any]] = []

    def add(key, label, score, value, note):
        if score is None:
            return
        dials.append({"key": key, "label": label, "score": round(_clamp(score), 2), "value": value, "note": note})

    b = (regime or {}).get("breadth") or {}
    if b.get("pct_above_sma50") is not None:
        x = float(b["pct_above_sma50"])
        add("breadth", "Breadth", (x - 50) / 40, f"{x:.0f}% above 50-day", "share of the screened universe in an uptrend")
    if t("spy") is not None:
        x = float(t("spy"))
        add("trend", "Trend", x / 6, f"SPY {x:+.1f}% · 20d", "broad index momentum")
    v = (vix or {}).get("vix") or (tape.get("vix") or {}).get("last")
    if v is not None:
        v = float(v); v3 = (tape.get("vix3m") or {}).get("last"); term = round(v / float(v3), 2) if v3 else None
        add("vol", "Volatility", (20 - v) / 10 + (0 if term is None else (-0.3 if term > 1 else 0.15)), f"VIX {v:.1f}" + (f" · term {term}" if term else ""), "under 20 calm; term > 1 = near-term fear (backwardation)")
    if t("hyg") is not None and t("lqd") is not None:
        d = float(t("hyg")) - float(t("lqd"))
        add("credit", "Credit", d / 2.5, f"HY vs IG {d:+.1f}% · 20d", "high-yield outperforming investment-grade = appetite for risk")
    if t("iwm") is not None and t("spy") is not None:
        d = float(t("iwm")) - float(t("spy"))
        add("size", "Small caps", d / 4, f"IWM vs SPY {d:+.1f}% · 20d", "small caps leading = broad risk-on")
    if t("rsp") is not None and t("spy") is not None:
        d = float(t("rsp")) - float(t("spy"))
        add("participation", "Participation", d / 2.5, f"equal- vs cap-weight {d:+.1f}%", "equal-weight leading = the rally is broad, not just megacaps")
    if t("qqq") is not None and t("spy") is not None:
        d = float(t("qqq")) - float(t("spy"))
        add("growth", "Growth vs broad", d / 4, f"QQQ vs SPY {d:+.1f}% · 20d", "growth leadership = risk appetite; lagging = rotation/defense")
    if t("us10y_yield", "chg_20d_bp") is not None:
        bp = float(t("us10y_yield", "chg_20d_bp")); lvl = (tape.get("us10y_yield") or {}).get("last")
        m3 = (tape.get("us3m_yield") or {}).get("last"); curve = f" · 10y−3m {float(lvl) - float(m3):+.2f}" if lvl is not None and m3 is not None else ""
        add("rates", "Rates", -bp / 40, f"10y {lvl}% ({bp:+.0f}bp 20d){curve}", "rising long yields tighten conditions; inverted curve = late cycle")
    if t("dollar_index") is not None:
        x = float(t("dollar_index")); lvl = (tape.get("dollar_index") or {}).get("last")
        add("dollar", "Dollar", -x / 3, f"DXY {lvl} ({x:+.1f}% 20d)", "a rising dollar is a headwind for risk assets and commodities")
    if t("copper") is not None and t("gold") is not None:
        d = float(t("copper")) - float(t("gold"))
        add("cycle", "Copper / gold", d / 6, f"{d:+.1f}% · 20d", "copper over gold = growth expectations; gold over copper = fear")
    if t("wti_oil") is not None:
        x = float(t("wti_oil")); lvl = (tape.get("wti_oil") or {}).get("last")
        add("oil", "Oil", -x / 12, f"WTI {lvl} ({x:+.1f}% 20d)", "a spike taxes consumers and lifts inflation risk")
    if t("bitcoin") is not None:
        x = float(t("bitcoin"))
        add("crypto", "Crypto", x / 15, f"BTC {x:+.1f}% · 20d", "the most speculative risk gauge")
    return dials
