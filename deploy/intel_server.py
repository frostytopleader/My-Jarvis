"""
Jarvis Intel MCP Server — port 8004 (internal only)

T2: osint_person, company_intel, regulatory_monitor, news_briefing
T3: flight_intel, maritime_intel, satellite_imagery, patent_search,
    track_entity, real_estate_scan
T4: competitive_map

All tools use public APIs — no additional keys required by default.
Playwright (installed in build.sh) needed for browser-based tools.
"""
import asyncio
import json
import os
import re
import sqlite3
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

# ── Paths & shared constants ───────────────────────────────────────────────────
JARVIS_DIR      = Path(os.path.expanduser("~/.openjarvis"))
SCREENSHOTS_DIR = JARVIS_DIR / "screenshots"
MONITOR_DB      = JARVIS_DIR / "monitor.db"   # shared with tools_server.py

BROWSER_ARGS     = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
TWILIO_TO        = os.environ.get("JARVIS_NOTIFY_NUMBER", "+13032699875")
MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL_SECONDS", "600"))


# ── Utility helpers ────────────────────────────────────────────────────────────

def _require_playwright() -> str | None:
    if not PLAYWRIGHT_OK:
        return "Playwright not installed. Run: pip install playwright && python -m playwright install chromium"
    return None


def _launch_browser(p):
    return p.chromium.launch(headless=True, args=BROWSER_ARGS)


def _get(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)",
                 "Accept": "application/json, text/html, */*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _news_rss(query: str, n: int = 10) -> list[dict]:
    """Google News RSS items for a query."""
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}+when:3d&hl=en-US&gl=US&ceid=US:en"
    try:
        raw = _get(url).decode("utf-8", errors="replace")
    except Exception as e:
        return [{"title": f"News fetch error: {e}", "url": "", "pub": ""}]

    titles  = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", raw)
    links   = re.findall(r"<link>(https?://[^<]+)</link>", raw)
    pubdats = re.findall(r"<pubDate>(.*?)</pubDate>", raw)
    sources = re.findall(r"<source[^>]*>(.*?)</source>", raw)

    items = []
    for i, title in enumerate(titles[1:n + 1]):
        items.append({
            "title":  title,
            "url":    links[i + 1] if i + 1 < len(links) else "",
            "pub":    pubdats[i + 1][:22] if i + 1 < len(pubdats) else "",
            "source": sources[i] if i < len(sources) else "",
        })
    return items


def _ddg_search(query: str, n: int = 8) -> list[dict]:
    """DuckDuckGo HTML search — no JS needed."""
    encoded = urllib.parse.urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{encoded}"
    req = urllib.request.Request(
        url,
        data=encoded.encode(),
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return [{"title": f"Search error: {e}", "url": "", "snippet": ""}]

    titles   = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)

    results = []
    for i, (href, raw_title) in enumerate(titles[:n]):
        title   = re.sub(r"<[^>]+>", "", raw_title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        # DuckDuckGo wraps real URLs; extract from ?uddg= param if present
        m = re.search(r"[?&]uddg=([^&]+)", href)
        clean_url = urllib.parse.unquote(m.group(1)) if m else href
        results.append({"title": title, "url": clean_url, "snippet": snippet})
    return results


def _wiki_summary(topic: str) -> str:
    """Wikipedia REST summary for a topic."""
    encoded = urllib.parse.quote(topic.replace(" ", "_"))
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
    try:
        data = json.loads(_get(url))
        extract = data.get("extract", "")
        return extract[:1200] if extract else ""
    except Exception:
        return ""


def _sec_efts(query: str, forms: str = "", n: int = 8) -> list[dict]:
    """SEC EDGAR full-text search — returns recent filings mentioning query."""
    params = {"q": f'"{query}"', "dateRange": "custom",
              "startdt": "2023-01-01", "hits.hits.total.value": "true"}
    if forms:
        params["forms"] = forms
    encoded = urllib.parse.urlencode(params)
    url = f"https://efts.sec.gov/LATEST/search-index?{encoded}"
    try:
        data = json.loads(_get(url))
    except Exception as e:
        return [{"name": f"SEC search error: {e}", "date": "", "form": "", "url": ""}]

    hits = data.get("hits", {}).get("hits", [])
    results = []
    for h in hits[:n]:
        s = h.get("_source", {})
        results.append({
            "name":  s.get("entity_name", s.get("display_names", ["?"])[0] if s.get("display_names") else "?"),
            "date":  s.get("file_date", ""),
            "form":  s.get("form_type", ""),
            "desc":  (s.get("period_of_report") or "")[:80],
            "url":   f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={s.get('entity_id','')}&type={s.get('form_type','')}&dateb=&owner=include&count=10",
        })
    return results


def _nominatim_geocode(location: str) -> tuple[float, float] | None:
    """Return (lat, lon) for a place name via OpenStreetMap Nominatim."""
    encoded = urllib.parse.quote(location)
    url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0 (personal AI)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            results = json.loads(r.read())
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None


def _ensure_monitor_schema(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS monitors (
        url TEXT PRIMARY KEY, hash TEXT, snippet TEXT,
        checked_at TEXT, title TEXT,
        watch_active INTEGER DEFAULT 0, label TEXT DEFAULT ''
    )""")
    for col, defn in [("watch_active", "INTEGER DEFAULT 0"), ("label", "TEXT DEFAULT ''")]:
        try:
            db.execute(f"ALTER TABLE monitors ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass


# ── T2 Intel Tools ─────────────────────────────────────────────────────────────

def tool_osint_person(name: str, context: str = "") -> str:
    """
    Multi-source OSINT profile on any public individual.
    Searches news, SEC filings, Wikipedia, and public web.
    Use for due diligence before NexFlow deals or investor meetings.
    """
    sections = [f"OSINT Report: {name}", f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]

    if context:
        sections.append(f"Context: {context}\n")

    # Wikipedia
    wiki = _wiki_summary(name)
    if wiki:
        sections.append("── Wikipedia ──────────────────────────────────")
        sections.append(wiki)
        sections.append("")

    # Recent news
    news = _news_rss(f'"{name}"', n=8)
    if news:
        sections.append("── Recent News (last 3 days) ──────────────────")
        for item in news:
            src = f" [{item['source']}]" if item.get("source") else ""
            sections.append(f"• {item['title']}{src}")
            sections.append(f"  {item['pub']}  {item['url']}")
        sections.append("")

    # SEC EDGAR
    sec_hits = _sec_efts(name, n=5)
    real_hits = [h for h in sec_hits if "error" not in h.get("name", "").lower()]
    if real_hits:
        sections.append("── SEC EDGAR Filings ───────────────────────────")
        for h in real_hits:
            sections.append(f"• {h['name']}  ({h['form']})  {h['date']}")
            sections.append(f"  {h['url']}")
        sections.append("")

    # Web search
    web = _ddg_search(f'"{name}" {context}', n=6)
    if web:
        sections.append("── Web Results ─────────────────────────────────")
        for r in web:
            if r.get("title") and "error" not in r["title"].lower():
                sections.append(f"• {r['title']}")
                if r.get("snippet"):
                    sections.append(f"  {r['snippet'][:180]}")
                sections.append(f"  {r['url']}")
        sections.append("")

    # LinkedIn search hint
    li_query = urllib.parse.quote(f"{name} linkedin")
    sections.append("── LinkedIn ────────────────────────────────────")
    sections.append(f"Search: https://www.google.com/search?q={li_query}")

    return "\n".join(sections)


def tool_company_intel(company: str, context: str = "") -> str:
    """
    Full company intelligence profile: funding, leadership, products, news,
    SEC filings, regulatory footprint. SAC-level competitive intel.
    """
    sections = [f"Company Intel: {company}", f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]

    if context:
        sections.append(f"Context: {context}\n")

    # Wikipedia
    wiki = _wiki_summary(company)
    if wiki:
        sections.append("── Overview (Wikipedia) ────────────────────────")
        sections.append(wiki)
        sections.append("")

    # SEC EDGAR company filings
    sec_hits = _sec_efts(company, forms="10-K,10-Q,8-K,S-1", n=6)
    real_hits = [h for h in sec_hits if "error" not in h.get("name", "").lower()]
    if real_hits:
        sections.append("── SEC Filings ─────────────────────────────────")
        for h in real_hits:
            sections.append(f"• {h['name']}  Form {h['form']}  {h['date']}")
        sections.append(f"  EDGAR search: https://efts.sec.gov/LATEST/search-index?q=%22{urllib.parse.quote(company)}%22&forms=10-K,10-Q,8-K,S-1")
        sections.append("")

    # Crunchbase link
    cb_slug = company.lower().replace(" ", "-").replace(",", "").replace(".", "")
    sections.append("── Crunchbase ──────────────────────────────────")
    sections.append(f"  https://www.crunchbase.com/organization/{cb_slug}")
    sections.append("")

    # Recent news
    news = _news_rss(f'"{company}"', n=10)
    if news:
        sections.append("── Recent News (last 3 days) ──────────────────")
        for item in news:
            src = f" [{item['source']}]" if item.get("source") else ""
            sections.append(f"• {item['title']}{src}  {item['pub']}")
            sections.append(f"  {item['url']}")
        sections.append("")

    # Web search
    web = _ddg_search(f'"{company}" funding leadership products {context}', n=6)
    if web:
        sections.append("── Web Intelligence ────────────────────────────")
        for r in web:
            if r.get("title") and "error" not in r["title"].lower():
                sections.append(f"• {r['title']}")
                if r.get("snippet"):
                    sections.append(f"  {r['snippet'][:200]}")
                sections.append(f"  {r['url']}")

    return "\n".join(sections)


def tool_regulatory_monitor(query: str, source: str = "all") -> str:
    """
    Search SEC, CFTC, and FINRA for enforcement actions, filings, and
    regulatory events matching a ticker, company name, or person.
    Essential for trading compliance and due diligence.
    """
    sections = [f"Regulatory Monitor: '{query}'", f"Source: {source}  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]

    # SEC EDGAR full-text search
    if source in ("all", "sec"):
        sections.append("── SEC EDGAR Full-Text Search ──────────────────")
        hits = _sec_efts(query, n=10)
        good = [h for h in hits if "error" not in h.get("name", "").lower()]
        if good:
            for h in good:
                sections.append(f"• {h['date']}  {h['form']:8s}  {h['name']}")
            sections.append(f"\n  Full results: https://efts.sec.gov/LATEST/search-index?q=%22{urllib.parse.quote(query)}%22")
        else:
            sections.append("  No recent SEC filings found.")
        sections.append("")

    # SEC enforcement actions
    if source in ("all", "sec"):
        sections.append("── SEC Enforcement Actions ─────────────────────")
        enf_hits = _sec_efts(query, forms="LITIGATION RELEASE,ADMIN PROC", n=5)
        enf_good = [h for h in enf_hits if "error" not in h.get("name", "").lower()]
        if enf_good:
            for h in enf_good:
                sections.append(f"• {h['date']}  {h['form']}  {h['name']}")
        else:
            sections.append("  No enforcement actions found.")
            sections.append(f"  Manual check: https://www.sec.gov/litigation/litreleases.shtml")
        sections.append("")

    # CFTC enforcement news
    if source in ("all", "cftc"):
        sections.append("── CFTC News ───────────────────────────────────")
        cftc_news = _news_rss(f"{query} CFTC enforcement action", n=5)
        if cftc_news and cftc_news[0].get("title") and "error" not in cftc_news[0]["title"]:
            for item in cftc_news:
                sections.append(f"• {item['title']}  {item['pub']}")
                sections.append(f"  {item['url']}")
        else:
            sections.append("  No recent CFTC news.")
            sections.append(f"  Manual check: https://www.cftc.gov/LawRegulation/Enforcement/index.htm")
        sections.append("")

    # FINRA BrokerCheck
    if source in ("all", "finra"):
        sections.append("── FINRA BrokerCheck ───────────────────────────")
        finra_encoded = urllib.parse.quote(query)
        sections.append(f"  Search: https://brokercheck.finra.org/search/genericsearch/{finra_encoded}")
        sections.append(f"  API: https://api.brokercheck.finra.org/search/firm?query={finra_encoded}")
        try:
            bc_data = json.loads(_get(
                f"https://api.brokercheck.finra.org/search/individual?query={finra_encoded}&hl=true&nrows=5",
                timeout=8
            ))
            hits = bc_data.get("hits", {}).get("hits", [])
            if hits:
                for h in hits[:5]:
                    s = h.get("_source", {})
                    name_ = s.get("ind_firstname", "") + " " + s.get("ind_lastname", "")
                    sections.append(f"  • {name_.strip()}  CRD: {s.get('ind_source_id','?')}")
        except Exception:
            sections.append("  (BrokerCheck API unavailable — use manual link above)")

    return "\n".join(sections)


def tool_news_briefing(topics: list = None) -> str:
    """
    Personalized news digest across Andre's core interests.
    Default topics: BTC/crypto, asteroid mining, Dubai, NexFlow AI,
    SAC/family office, trading desk, CU Boulder, aerospace.
    Pass custom topics list to override.
    """
    default_topics = [
        ("Crypto & Markets",    "bitcoin ethereum crypto market SEC"),
        ("Asteroid Mining",     "asteroid mining space resources ISRU Planetary Resources"),
        ("Dubai & UAE",         "Dubai UAE business investment real estate 2026"),
        ("AI & Automation",     "AI automation workflow NexFlow artificial intelligence"),
        ("Macro & Fed",         "Federal Reserve rate cut inflation CPI macro economy"),
        ("Aerospace & Space",   "SpaceX Rocket Lab asteroid launch 2026 2027"),
        ("Family Office / PE",  "family office sovereign wealth fund private equity"),
        ("CU Boulder",          "CU Boulder University Colorado research"),
    ]

    if topics:
        query_list = [(t, t) for t in topics]
    else:
        query_list = default_topics

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    sections = [f"Jarvis Morning Briefing  {ts}", f"{'─'*50}", ""]

    total_items = 0
    for label, query in query_list:
        news = _news_rss(query, n=5)
        good = [n for n in news if n.get("title") and "error" not in n["title"].lower()]
        if not good:
            continue
        sections.append(f"▸ {label}")
        for item in good[:3]:
            src = f" [{item['source']}]" if item.get("source") else ""
            sections.append(f"  • {item['title']}{src}")
            sections.append(f"    {item['pub']}  {item['url']}")
        sections.append("")
        total_items += len(good[:3])

    sections.insert(2, f"  {total_items} stories across {len(query_list)} topics\n")
    return "\n".join(sections)


# ── T3 Intel Tools ─────────────────────────────────────────────────────────────

def tool_flight_intel(query: str) -> str:
    """
    Real-time flight tracking — look up any flight by number (UA123),
    tail number (N12345), or route ('Denver to Dubai', 'DEN JFK').
    Uses FlightAware public web data via Playwright.
    """
    if err := _require_playwright():
        return err

    # Classify query type
    query = query.strip()
    is_tail   = bool(re.match(r"^[A-Z]-?[A-Z0-9]{3,6}$", query.upper()))
    is_flight = bool(re.match(r"^[A-Z]{2,3}\s*\d{1,4}[A-Z]?$", query.upper().replace(" ", "")))

    if is_tail:
        fa_url = f"https://flightaware.com/resources/registration/{query.upper()}"
    elif is_flight:
        clean = query.upper().replace(" ", "")
        fa_url = f"https://flightaware.com/live/flight/{clean}"
    else:
        # Route / free-text search
        encoded = urllib.parse.quote(query)
        fa_url = f"https://flightaware.com/live/findflight?origin=&destination=&filter={encoded}"

    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; Jarvis/1.0)")
        try:
            page.goto(fa_url, timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            title   = page.title()
            content = page.inner_text("body")[:4000]
            url_out = page.url
        finally:
            browser.close()

    # Also try OpenSky for live position data (no key, anonymous)
    opensky_data = ""
    try:
        if is_flight:
            callsign = re.sub(r"[^A-Z0-9]", "", query.upper())
            cs_encoded = urllib.parse.quote(callsign.ljust(8))
            os_url = f"https://opensky-network.org/api/states/all?time=0"
            # just link it - full OpenSky data is 100MB+, filter isn't free
            opensky_data = f"\nOpenSky callsign search: https://opensky-network.org/aircraft-profile?icao24=&callsign={callsign}"
    except Exception:
        pass

    return (
        f"Flight Intel: {query}\n"
        f"Source: {url_out}\n\n"
        f"Page: {title}\n\n"
        f"{content}"
        f"{opensky_data}"
    )


def tool_maritime_intel(query: str) -> str:
    """
    AIS vessel tracking — search any ship by name, IMO number, or port.
    Useful for commodity trade thesis and CMC logistics planning.
    Data via VesselFinder public search.
    """
    if err := _require_playwright():
        return err

    encoded = urllib.parse.quote(query)
    vf_url  = f"https://www.vesselfinder.com/vessels?name={encoded}"

    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; Jarvis/1.0)")
        try:
            page.goto(vf_url, timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            title   = page.title()
            content = page.inner_text("body")[:4000]
            url_out = page.url
        finally:
            browser.close()

    # Also search MarineTraffic
    mt_url = f"https://www.marinetraffic.com/en/ais/index/search/all?keyword={encoded}"

    return (
        f"Maritime Intel: {query}\n"
        f"Source: {url_out}\n\n"
        f"Page: {title}\n\n"
        f"{content[:3000]}\n\n"
        f"Also: {mt_url}"
    )


def tool_satellite_imagery(location: str, zoom: int = 16) -> str:
    """
    Screenshot satellite imagery for any location.
    Accepts: place name ('Dubai Marina'), coordinates ('25.0657,55.1713'),
    or address. Saves PNG to ~/.openjarvis/screenshots/.
    Use to scout CMC Dubai campus site, assess real estate parcels.
    """
    if err := _require_playwright():
        return err

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Parse or geocode location
    coord_match = re.match(r"^(-?\d+\.?\d*)\s*[,\s]\s*(-?\d+\.?\d*)$", location.strip())
    if coord_match:
        lat = float(coord_match.group(1))
        lon = float(coord_match.group(2))
        display_loc = f"{lat},{lon}"
    else:
        coords = _nominatim_geocode(location)
        if not coords:
            return f"Could not geocode location: {location}\nTry passing coordinates directly, e.g. '25.0657,55.1713'"
        lat, lon = coords
        display_loc = location

    # Google Maps satellite view
    maps_url = f"https://www.google.com/maps/@{lat},{lon},{zoom}z/data=!3m1!1e3"

    ts       = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_loc = re.sub(r"[^a-zA-Z0-9_-]", "_", location)[:40]
    out_path = SCREENSHOTS_DIR / f"sat_{safe_loc}_{ts}.png"

    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(maps_url, timeout=30_000, wait_until="networkidle")
            page.wait_for_timeout(3000)
            # Dismiss cookie popup if present
            for sel in ['button[aria-label*="Accept"]', 'button:has-text("Accept all")',
                        'button:has-text("I agree")']:
                try:
                    page.click(sel, timeout=2000)
                    page.wait_for_timeout(1000)
                    break
                except Exception:
                    pass
            page.screenshot(path=str(out_path))
        finally:
            browser.close()

    return (
        f"Satellite Imagery: {display_loc}\n"
        f"Coordinates: {lat:.6f}, {lon:.6f}\n"
        f"Zoom level: {zoom}\n"
        f"Screenshot saved: {out_path}\n"
        f"Google Maps URL: {maps_url}"
    )


def tool_patent_search(query: str, assignee: str = "", year_from: str = "2018") -> str:
    """
    Search USPTO patents by keyword, assignee company, or technology area.
    Use to check freedom-to-operate for CMC tech, track competitor IP filings,
    or identify acquisition targets by patent portfolio.
    Data via USPTO PatentsView API (free, no key).
    """
    # Build PatentsView query
    q_parts: list = [{"_text_any": {"patent_title": query, "patent_abstract": query}}]
    if assignee:
        q_parts.append({"_text_any": {"assignee_organization": assignee}})
    if year_from:
        q_parts.append({"_gte": {"patent_date": f"{year_from}-01-01"}})

    pv_query = json.dumps({"_and": q_parts} if len(q_parts) > 1 else q_parts[0])
    pv_fields = json.dumps([
        "patent_id", "patent_title", "patent_date",
        "assignee_organization", "inventor_last_name",
        "patent_abstract", "patent_num_cited_by_us_patents",
    ])
    pv_sort   = json.dumps([{"patent_date": "desc"}])
    params    = urllib.parse.urlencode({
        "q": pv_query, "f": pv_fields, "s": pv_sort, "o": '{"per_page":12}'
    })
    url = f"https://search.patentsview.org/api/v1/patent/?{params}"

    try:
        data = json.loads(_get(url, timeout=15))
    except Exception as e:
        return f"USPTO PatentsView API error: {e}\nManual search: https://patentsview.org/search"

    patents = data.get("patents", [])
    total   = data.get("total_patent_count", 0)

    if not patents:
        return (
            f"No patents found for query='{query}'"
            + (f", assignee='{assignee}'" if assignee else "")
            + f"\n\nManual search: https://patentsview.org/search"
        )

    lines = [
        f"Patent Search: '{query}'" + (f"  assignee='{assignee}'" if assignee else ""),
        f"Total matches: {total}  |  Showing: {len(patents)}",
        f"Source: USPTO PatentsView  |  Since: {year_from}",
        "",
        f"  {'Date':12s}  {'Citations':>9}  {'Assignee':<30}  Title",
        f"  {'─'*12}  {'─'*9}  {'─'*30}  {'─'*40}",
    ]

    for p in patents:
        date     = p.get("patent_date", "?")
        title    = (p.get("patent_title") or "?")[:60]
        assigns  = p.get("assignees") or [{}]
        org      = (assigns[0].get("assignee_organization") or "Individual")[:28]
        cited    = p.get("patent_num_cited_by_us_patents") or 0
        pid      = p.get("patent_id", "")
        lines.append(f"  {date:12s}  {cited:>9}  {org:<30}  {title}")
        if pid:
            lines.append(f"    https://patents.google.com/patent/US{pid}")

    lines.append(f"\nFull PatentsView results: https://patentsview.org/search")
    return "\n".join(lines)


def tool_track_entity(entity: str, label: str = "") -> str:
    """
    Add any company, person, or ticker to background news monitoring.
    Jarvis will check Google News every 10 min and TEXT ANDRE when new
    significant news breaks about this entity.
    Example: 'SpaceX', 'Elon Musk', 'BTC SEC ETF', 'CMC asteroid mining'.
    """
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)

    # Build a Google News RSS URL for this entity
    encoded  = urllib.parse.quote(f'"{entity}"')
    news_url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    display  = label or entity

    # Register in monitor.db (shared with tools_server.py background loop)
    with sqlite3.connect(str(MONITOR_DB)) as db:
        _ensure_monitor_schema(db)
        # Grab a baseline hash of the current RSS feed
        try:
            import hashlib
            raw  = _get(news_url, timeout=10)
            h    = hashlib.md5(raw).hexdigest()
            snip = raw.decode("utf-8", errors="replace")[:600]
        except Exception:
            h, snip = "baseline_error", ""

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            """INSERT OR REPLACE INTO monitors
               (url, hash, snippet, checked_at, title, watch_active, label)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (news_url, h, snip, now, f"News: {display}", display),
        )

    return (
        f"Now tracking: {display}\n"
        f"Watching: Google News RSS for '{entity}'\n"
        f"Interval: every {MONITOR_INTERVAL // 60} minute(s)\n"
        f"Alert: SMS → {TWILIO_TO} when news feed changes\n\n"
        f"Feed URL: {news_url}\n\n"
        f"Use list_watches (in tools server) to see all active monitors.\n"
        f"Use unwatch_url to stop:\n  url = {news_url}"
    )


def tool_real_estate_scan(location: str, beds_min: int = 0, price_max: int = 0) -> str:
    """
    Scan property listings and pricing trends for any location.
    Covers Boulder, Denver, Dubai, and any US/international market.
    Returns current listings, median price, price/sqft trends.
    Data via Redfin (US) or Property Finder (Dubai).
    """
    if err := _require_playwright():
        return err

    location_lower = location.lower()
    is_dubai = any(k in location_lower for k in ["dubai", "uae", "abu dhabi"])

    if is_dubai:
        query = urllib.parse.quote(location)
        url   = f"https://www.propertyfinder.ae/en/search?l={query}&c=2&fu=0&rp=y"
        source = "Property Finder Dubai"
    else:
        query = urllib.parse.quote(location)
        url   = f"https://www.redfin.com/city/search/{query}"
        source = "Redfin"

    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
        try:
            page.goto(url, timeout=35_000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            title   = page.title()
            content = page.inner_text("body")[:5000]
            final_url = page.url
        finally:
            browser.close()

    filters = []
    if beds_min:
        filters.append(f"{beds_min}+ beds")
    if price_max:
        filters.append(f"≤ ${price_max:,}")
    filter_str = f"  Filters: {', '.join(filters)}" if filters else ""

    return (
        f"Real Estate Scan: {location}\n"
        f"Source: {source}{filter_str}\n"
        f"URL: {final_url}\n\n"
        f"Page: {title}\n\n"
        f"{content}"
    )


# ── T4 Intel Tools ─────────────────────────────────────────────────────────────

def tool_competitive_map(vertical: str, context: str = "") -> str:
    """
    Build a live competitive landscape for any vertical.
    Maps key players, funding, differentiation, and market positioning.
    Covers all Champeau Organization verticals: trading desk, NexFlow AI,
    Dubai campus analogs, SAC peer funds, CMC asteroid mining comps.
    Returns a structured competitive matrix with sourced intel.
    """
    sections = [
        f"Competitive Map: {vertical}",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"{'═'*55}",
        "",
    ]

    if context:
        sections.append(f"Context: {context}\n")

    # News landscape
    sections.append("── Industry News (last 3 days) ─────────────────────────")
    news = _news_rss(f"{vertical} competitor market 2026", n=8)
    good_news = [n for n in news if n.get("title") and "error" not in n["title"].lower()]
    if good_news:
        for item in good_news[:6]:
            src = f" [{item['source']}]" if item.get("source") else ""
            sections.append(f"• {item['title']}{src}")
            sections.append(f"  {item['url']}")
    else:
        sections.append("  No recent news found.")
    sections.append("")

    # Key players from web search
    sections.append("── Key Players (Web Intel) ──────────────────────────────")
    players_query = f'"{vertical}" top companies competitors players 2026 site:crunchbase.com OR site:pitchbook.com OR site:techcrunch.com'
    web_results = _ddg_search(players_query, n=8)
    for r in web_results:
        if r.get("title") and "error" not in r["title"].lower():
            sections.append(f"• {r['title']}")
            if r.get("snippet"):
                sections.append(f"  {r['snippet'][:200]}")
            sections.append(f"  {r['url']}")
    sections.append("")

    # SEC EDGAR — public companies in this vertical
    sections.append("── SEC Filings — Public Comps ───────────────────────────")
    sec_hits = _sec_efts(vertical, forms="10-K,S-1,8-K", n=6)
    good_sec = [h for h in sec_hits if "error" not in h.get("name", "").lower()]
    if good_sec:
        for h in good_sec:
            sections.append(f"• {h['name']:35s}  {h['form']:6s}  {h['date']}")
        sections.append(f"\n  Source: https://efts.sec.gov/LATEST/search-index?q=%22{urllib.parse.quote(vertical)}%22&forms=10-K,S-1")
    else:
        sections.append("  No public company filings found — private market or use broader query.")
    sections.append("")

    # Patent landscape
    sections.append("── Patent Landscape (USPTO) ─────────────────────────────")
    try:
        q_parts = json.dumps({"_text_any": {"patent_title": vertical, "patent_abstract": vertical}})
        f_parts = json.dumps(["patent_id", "patent_title", "patent_date", "assignee_organization"])
        s_parts = json.dumps([{"patent_date": "desc"}])
        params  = urllib.parse.urlencode({"q": q_parts, "f": f_parts, "s": s_parts, "o": '{"per_page":5}'})
        pdata   = json.loads(_get(f"https://search.patentsview.org/api/v1/patent/?{params}", timeout=10))
        patents = pdata.get("patents", [])
        total_p = pdata.get("total_patent_count", 0)
        if patents:
            sections.append(f"  {total_p} total patents matching '{vertical}'")
            for p in patents[:5]:
                assigns = p.get("assignees") or [{}]
                org     = (assigns[0].get("assignee_organization") or "Individual")[:30]
                date    = p.get("patent_date", "?")
                title   = (p.get("patent_title") or "?")[:55]
                sections.append(f"  • {date}  {org:<30}  {title}")
        else:
            sections.append(f"  No patents found for '{vertical}'")
    except Exception as e:
        sections.append(f"  Patent search unavailable: {e}")
    sections.append("")

    # Strategic positioning framework
    sections.append("── Strategic Framework ──────────────────────────────────")
    sections.append(f"  Vertical      : {vertical}")
    sections.append(f"  CO position   : [document Andre's positioning here]")
    sections.append(f"  Key moat      : [document differentiator here]")
    sections.append(f"  Threat level  : [assess based on above intel]")
    sections.append(f"  Opportunity   : [identify gap in competitive field]")
    sections.append("")
    sections.append("  → Use company_intel('<competitor>') for deep dives on any player above.")
    sections.append("  → Use track_entity('<competitor>') to get SMS alerts when they make news.")

    return "\n".join(sections)


# ── MCP manifest ───────────────────────────────────────────────────────────────

TOOLS = [
    # ── T2 ─────────────────────────────────────────────────────────────────────
    {
        "name": "osint_person",
        "description": (
            "Deep multi-source OSINT on any public individual. "
            "Searches news, SEC filings, Wikipedia, LinkedIn, and public web. "
            "Use for due diligence before NexFlow deals, investor meetings, or partnerships. "
            "Returns a structured profile with sourced intel."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name":    {"type": "string", "description": "Full name of the person"},
                "context": {"type": "string", "default": "", "description": "Context to focus the search (e.g. 'NexFlow investor' or 'CMC asteroid mining')"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "company_intel",
        "description": (
            "Full company intelligence profile: funding, leadership, products, news, "
            "SEC filings, regulatory footprint, and competitive position. "
            "SAC-level competitive intel for any company worldwide."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company name, e.g. 'SpaceX', 'Planetary Resources', 'NexFlow AI'"},
                "context": {"type": "string", "default": "", "description": "Context, e.g. 'CMC competitor' or 'potential acquisition target'"},
            },
            "required": ["company"],
        },
    },
    {
        "name": "regulatory_monitor",
        "description": (
            "Search SEC, CFTC, and FINRA for enforcement actions, filings, and regulatory events. "
            "Query by ticker symbol, company name, or person name. "
            "Essential for trading compliance, counterparty due diligence, and investment research."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":  {"type": "string", "description": "Ticker, company, or person name to search"},
                "source": {"type": "string", "default": "all", "description": "Filter: 'sec', 'cftc', 'finra', or 'all'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "news_briefing",
        "description": (
            "Personalized news digest across Andre's core interests. "
            "Default covers: BTC/crypto, asteroid mining, Dubai, AI/NexFlow, macro/Fed, "
            "aerospace/SpaceX, family office/PE, CU Boulder. "
            "Pass a list of custom topics to override the defaults."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional custom topic list. Omit to use Andre's default interests.",
                    "default": [],
                }
            },
            "required": [],
        },
    },
    # ── T3 ─────────────────────────────────────────────────────────────────────
    {
        "name": "flight_intel",
        "description": (
            "Real-time flight tracking via FlightAware + OpenSky. "
            "Look up any flight number (UA123), tail number (N12345), "
            "or describe a route ('Denver to Dubai', 'DEN JFK'). "
            "Useful for tracking founder travel, logistics planning, and competitive intelligence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Flight number, tail number, or route description"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "maritime_intel",
        "description": (
            "AIS vessel tracking via VesselFinder. "
            "Search any vessel by name, IMO number, or port. "
            "Useful for commodity trade thesis, CMC launch logistics, and shipping route analysis."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Vessel name, IMO number, or port name"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "satellite_imagery",
        "description": (
            "Screenshot Google Maps satellite imagery for any location. "
            "Accepts place name ('Dubai Marina'), lat/lon ('25.0657,55.1713'), or address. "
            "Pre-scout CMC Dubai campus site, assess real estate parcels, "
            "monitor construction progress."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Place name, address, or 'lat,lon' coordinates"},
                "zoom":     {"type": "integer", "default": 16, "description": "Zoom level 1-20 (default 16 = neighborhood; 18 = parcel)"},
            },
            "required": ["location"],
        },
    },
    {
        "name": "patent_search",
        "description": (
            "Search USPTO patents by keyword, technology, or assignee company. "
            "Check freedom-to-operate for CMC tech, track competitor IP filings, "
            "identify acquisition targets by patent portfolio strength. "
            "Data via USPTO PatentsView API (free, no key)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":     {"type": "string", "description": "Technology or keyword, e.g. 'asteroid mining extraction' or 'AI workflow automation'"},
                "assignee":  {"type": "string", "default": "", "description": "Filter by assignee company name"},
                "year_from": {"type": "string", "default": "2018", "description": "Only patents filed since this year (YYYY)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "track_entity",
        "description": (
            "Add any company, person, or ticker to Jarvis's background news monitor. "
            "Jarvis will check Google News every 10 minutes and TEXT ANDRE via SMS "
            "when significant news breaks. "
            "Example: 'SpaceX', 'Elon Musk Dubai', 'CMC asteroid mining'. "
            "Integrates with the same background loop as watch_url."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Company, person, or topic to monitor, e.g. 'SpaceX' or 'CMC asteroid mining'"},
                "label":  {"type": "string", "default": "", "description": "Short label for SMS alerts (defaults to entity name)"},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "real_estate_scan",
        "description": (
            "Scan property listings and pricing trends for any location. "
            "Covers Boulder, Denver, Dubai, and any US or international market. "
            "Returns current listings, pricing, and market overview. "
            "Feeds Champeau Properties thesis with live data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location":  {"type": "string", "description": "City, neighborhood, or address, e.g. 'Boulder CO', 'Dubai Marina', 'Denver Tech Center'"},
                "beds_min":  {"type": "integer", "default": 0, "description": "Minimum bedrooms (0 = any)"},
                "price_max": {"type": "integer", "default": 0, "description": "Max price in USD (0 = any)"},
            },
            "required": ["location"],
        },
    },
    # ── T4 ─────────────────────────────────────────────────────────────────────
    {
        "name": "competitive_map",
        "description": (
            "Build a live competitive landscape for any vertical or market. "
            "Aggregates news, public company filings, patent filings, and web intel. "
            "Covers all CO verticals: trading desk, NexFlow AI, Dubai campus, "
            "SAC peer funds, CMC asteroid mining. Returns a structured matrix "
            "with sourced intel and strategic framework."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vertical": {"type": "string", "description": "Market or vertical, e.g. 'asteroid mining', 'AI workflow automation', 'family office', 'crypto trading desk'"},
                "context":  {"type": "string", "default": "", "description": "Strategic context, e.g. 'CMC competitive positioning' or 'NexFlow market sizing'"},
            },
            "required": ["vertical"],
        },
    },
]

TOOL_FNS = {
    "osint_person":        lambda a: tool_osint_person(**a),
    "company_intel":       lambda a: tool_company_intel(**a),
    "regulatory_monitor":  lambda a: tool_regulatory_monitor(**a),
    "news_briefing":       lambda a: tool_news_briefing(**a),
    "flight_intel":        lambda a: tool_flight_intel(**a),
    "maritime_intel":      lambda a: tool_maritime_intel(**a),
    "satellite_imagery":   lambda a: tool_satellite_imagery(**a),
    "patent_search":       lambda a: tool_patent_search(**a),
    "track_entity":        lambda a: tool_track_entity(**a),
    "real_estate_scan":    lambda a: tool_real_estate_scan(**a),
    "competitive_map":     lambda a: tool_competitive_map(**a),
}

SESSION_ID = "jarvis-intel-1"

# ── MCP HTTP server ────────────────────────────────────────────────────────────

app = FastAPI()


@app.post("/")
async def handle(request: Request):
    body   = await request.json()
    method = body.get("method", "")
    id_    = body.get("id")

    if method == "initialize":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": id_,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "jarvis-intel", "version": "1.0.0"},
                },
            },
            headers={"Mcp-Session-Id": SESSION_ID},
        )

    if method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0"})

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {"tools": TOOLS}})

    if method == "tools/call":
        params = body.get("params", {})
        name   = params.get("name", "")
        args   = params.get("arguments", {})
        fn     = TOOL_FNS.get(name)
        if not fn:
            return JSONResponse({
                "jsonrpc": "2.0", "id": id_,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            })
        try:
            result = await asyncio.to_thread(lambda: fn(args))
        except Exception:
            result = f"Tool error:\n{traceback.format_exc()}"
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": id_,
            "result": {"content": [{"type": "text", "text": result}]},
        })

    return JSONResponse({
        "jsonrpc": "2.0", "id": id_,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    })


if __name__ == "__main__":
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    print("Jarvis Intel server starting on port 8004")
    print(f"  Playwright : {'available' if PLAYWRIGHT_OK else 'NOT installed — browser tools will fail'}")
    print(f"  Monitor DB : {MONITOR_DB}")
    print(f"  SMS alerts → {TWILIO_TO}")
    uvicorn.run(app, host="127.0.0.1", port=8004, log_level="info")
