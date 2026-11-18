"""
Jarvis Browser MCP server — port 8007
Scraping, real-estate alerts, competitor job intel, gov portal monitoring,
conference discovery, price-drop hunting, LinkedIn prospect scanning.
"""
import asyncio, hashlib, json, os, re, sqlite3, time
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ── constants ────────────────────────────────────────────────────────────────
DB_PATH     = os.path.expanduser("~/.openjarvis/monitor.db")
TWILIO_SID  = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN= os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
ANDRE_PHONE = os.environ.get("ANDRE_PHONE", "+13032699875")
HEADERS     = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

app = FastAPI()

# ── shared helpers ────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS re_seen_listings (
            id TEXT PRIMARY KEY, source TEXT, title TEXT, price TEXT,
            url TEXT, seen_at REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gov_monitor_hashes (
            portal TEXT, keyword TEXT, url TEXT, hash TEXT,
            last_changed REAL, PRIMARY KEY (portal, keyword, url)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            url TEXT PRIMARY KEY, title TEXT, price REAL,
            currency TEXT, last_checked REAL, last_changed REAL
        )""")
    conn.commit()
    return conn


def _sms(body: str) -> str:
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM):
        return f"[SMS not configured — message would be: {body[:120]}]"
    import urllib.request, urllib.parse
    data = urllib.parse.urlencode({
        "From": TWILIO_FROM, "To": ANDRE_PHONE, "Body": body[:1600]
    }).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
        data=data, method="POST"
    )
    import base64
    creds = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"SMS sent ({r.status})"
    except Exception as exc:
        return f"SMS error: {exc}"


def _get(url: str, timeout: int = 15) -> str:
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as c:
        return c.get(url).text


def _ddg_html(query: str, max_results: int = 10) -> list[dict]:
    """DuckDuckGo HTML search — no API key required."""
    url = "https://html.duckduckgo.com/html/"
    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as c:
        r = c.post(url, data={"q": query, "b": "", "kl": "us-en"})
    soup = BeautifulSoup(r.text, "lxml")
    results = []
    for a in soup.select("a.result__a")[:max_results]:
        href = a.get("href", "")
        snippet_el = a.find_parent("div", class_="result__body")
        snippet = ""
        if snippet_el:
            s = snippet_el.find("a", class_="result__snippet")
            if s:
                snippet = s.get_text(" ", strip=True)
        results.append({"title": a.get_text(strip=True), "url": href, "snippet": snippet})
    return results


def _extract_price(html: str, url: str) -> tuple[float | None, str]:
    """Extract price from product page HTML. Returns (amount, currency)."""
    soup = BeautifulSoup(html, "lxml")
    # 1. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            offers = data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0]
            price = offers.get("price") or offers.get("lowPrice")
            currency = offers.get("priceCurrency", "USD")
            if price:
                return float(str(price).replace(",", "")), currency
        except Exception:
            pass
    # 2. OG tags
    og = soup.find("meta", property="product:price:amount")
    if og and og.get("content"):
        currency_tag = soup.find("meta", property="product:price:currency")
        cur = currency_tag["content"] if currency_tag else "USD"
        try:
            return float(og["content"].replace(",", "")), cur
        except Exception:
            pass
    # 3. Regex heuristic
    text = soup.get_text(" ")
    m = re.search(r"[\$€£¥₪]\s*([\d,]+(?:\.\d{1,2})?)", text)
    if m:
        sym_map = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₪": "ILS"}
        sym = m.group(0).strip()[0]
        try:
            return float(m.group(1).replace(",", "")), sym_map.get(sym, "USD")
        except Exception:
            pass
    return None, "USD"


# ── tool implementations ──────────────────────────────────────────────────────

def tool_real_estate_alert_bot(
    action: str = "check",
    min_price: int = 0,
    max_price: int = 10_000_000,
    min_beds: int = 0,
    location: str = "Denver CO",
    sources: str = "zillow,redfin",
    alert_sms: bool = True,
) -> str:
    """
    Scrape real-estate listing sites for new matches and SMS-alert Andre.
    action: "check" (run now) | "status" (show seen-listing counts)
    sources: comma-separated list of: zillow, redfin, dubizzle
    """
    if action == "status":
        conn = _db()
        rows = conn.execute(
            "SELECT source, COUNT(*) FROM re_seen_listings GROUP BY source"
        ).fetchall()
        conn.close()
        return json.dumps({"seen_listings": dict(rows)})

    source_list = [s.strip().lower() for s in sources.split(",")]
    new_hits: list[dict] = []
    conn = _db()

    def _check_zillow():
        query = f"site:zillow.com/homes {location} {min_beds}bd"
        results = _ddg_html(query, max_results=15)
        for r in results:
            if "/homedetails/" not in r["url"]:
                continue
            lid = hashlib.md5(r["url"].encode()).hexdigest()
            if conn.execute("SELECT 1 FROM re_seen_listings WHERE id=?", (lid,)).fetchone():
                continue
            # Try to extract price from snippet
            price_m = re.search(r"\$([\d,]+[kKmM]?)", r["snippet"])
            price_str = price_m.group(0) if price_m else "unknown"
            conn.execute(
                "INSERT OR IGNORE INTO re_seen_listings VALUES (?,?,?,?,?,?)",
                (lid, "zillow", r["title"], price_str, r["url"], time.time())
            )
            new_hits.append({"source": "zillow", "title": r["title"],
                             "price": price_str, "url": r["url"]})

    def _check_redfin():
        query = f"site:redfin.com {location} for sale {min_beds} bed"
        results = _ddg_html(query, max_results=15)
        for r in results:
            if "/home/" not in r["url"] and "/redfin.com" not in r["url"]:
                continue
            lid = hashlib.md5(r["url"].encode()).hexdigest()
            if conn.execute("SELECT 1 FROM re_seen_listings WHERE id=?", (lid,)).fetchone():
                continue
            price_m = re.search(r"\$([\d,]+[kKmM]?)", r["snippet"])
            price_str = price_m.group(0) if price_m else "unknown"
            conn.execute(
                "INSERT OR IGNORE INTO re_seen_listings VALUES (?,?,?,?,?,?)",
                (lid, "redfin", r["title"], price_str, r["url"], time.time())
            )
            new_hits.append({"source": "redfin", "title": r["title"],
                             "price": price_str, "url": r["url"]})

    def _check_dubizzle():
        query = f"site:dubizzle.com/for-sale/properties {min_beds} bedroom"
        results = _ddg_html(query, max_results=15)
        for r in results:
            lid = hashlib.md5(r["url"].encode()).hexdigest()
            if conn.execute("SELECT 1 FROM re_seen_listings WHERE id=?", (lid,)).fetchone():
                continue
            price_m = re.search(r"AED\s*([\d,]+)", r["snippet"])
            price_str = price_m.group(0) if price_m else "unknown"
            conn.execute(
                "INSERT OR IGNORE INTO re_seen_listings VALUES (?,?,?,?,?,?)",
                (lid, "dubizzle", r["title"], price_str, r["url"], time.time())
            )
            new_hits.append({"source": "dubizzle", "title": r["title"],
                             "price": price_str, "url": r["url"]})

    for src in source_list:
        try:
            if src == "zillow":
                _check_zillow()
            elif src == "redfin":
                _check_redfin()
            elif src in ("dubizzle", "propertyfinder"):
                _check_dubizzle()
        except Exception as exc:
            new_hits.append({"source": src, "error": str(exc)})

    conn.commit()
    conn.close()

    if not new_hits:
        return f"No new listings found for {location} ({', '.join(source_list)})."

    summary_lines = [f"🏠 {h['source'].upper()}: {h['title']} — {h['price']}\n{h.get('url','')}"
                     for h in new_hits if "error" not in h]
    if alert_sms and summary_lines:
        sms_body = f"Jarvis RE Alert ({location}):\n" + "\n\n".join(summary_lines[:3])
        _sms(sms_body)

    return json.dumps({"new_listings": new_hits, "count": len(new_hits)}, indent=2)


def tool_competitor_job_intel(
    companies: str = "OpenAI,Anthropic",
    keywords: str = "",
    max_per_company: int = 10,
) -> str:
    """
    Scrape public job postings for target companies and extract strategic signals
    (what they're building, hiring spikes, new initiatives).
    companies: comma-separated company names
    keywords: optional filter keywords (e.g. "AI,ML,infrastructure")
    """
    company_list = [c.strip() for c in companies.split(",") if c.strip()]
    kw_list = [k.strip().lower() for k in keywords.split(",") if k.strip()] if keywords else []
    output: dict[str, Any] = {}

    for company in company_list:
        postings: list[dict] = []
        try:
            # Search LinkedIn, Greenhouse, Lever, Indeed
            for site in ["linkedin.com/jobs", "boards.greenhouse.io", "jobs.lever.co", "indeed.com"]:
                q = f'site:{site} "{company}" jobs'
                if kw_list:
                    q += " " + " OR ".join(f'"{k}"' for k in kw_list[:3])
                results = _ddg_html(q, max_results=max_per_company)
                for r in results:
                    if company.lower() not in (r["title"] + r["snippet"]).lower():
                        continue
                    # Extract signals from title/snippet
                    signals = []
                    text = (r["title"] + " " + r["snippet"]).lower()
                    signal_patterns = {
                        "AI/ML build-out": r"\b(llm|ml|machine learning|ai researcher|foundation model)\b",
                        "Infrastructure scale": r"\b(infrastructure|platform engineer|sre|devops|kubernetes)\b",
                        "Sales expansion": r"\b(account executive|sales engineer|enterprise sales|bdr|sdr)\b",
                        "Research push": r"\b(research scientist|phd|scientist|researcher)\b",
                        "Security investment": r"\b(security engineer|trust.safety|red team|appsec)\b",
                        "Hardware/robotics": r"\b(fpga|hardware|robotics|embedded|silicon)\b",
                        "International expansion": r"\b(emea|apac|latam|international|global)\b",
                    }
                    for label, pat in signal_patterns.items():
                        if re.search(pat, text):
                            signals.append(label)
                    postings.append({
                        "title": r["title"],
                        "url": r["url"],
                        "snippet": r["snippet"][:200],
                        "signals": signals,
                        "source": site.split("/")[0],
                    })
                if len(postings) >= max_per_company:
                    break
        except Exception as exc:
            postings.append({"error": str(exc)})

        # Aggregate signals
        all_signals: dict[str, int] = {}
        for p in postings:
            for s in p.get("signals", []):
                all_signals[s] = all_signals.get(s, 0) + 1

        output[company] = {
            "total_postings_found": len([p for p in postings if "error" not in p]),
            "strategic_signals": sorted(all_signals.items(), key=lambda x: -x[1]),
            "sample_postings": postings[:5],
        }

    return json.dumps(output, indent=2)


def tool_gov_portal_monitor(
    portals: str = "sec_edgar,colorado_sos",
    keywords: str = "NexFlow,aerospace,uranium",
    alert_sms: bool = True,
) -> str:
    """
    Hash-based monitoring of government portals for new filings or rule changes.
    portals: comma-separated from: sec_edgar, colorado_sos, uae_moei, dmcc, faa_ast
    keywords: comma-separated search terms
    Returns changes since last check.
    """
    PORTAL_URLS = {
        "sec_edgar": "https://efts.sec.gov/LATEST/search-index?q={kw}&dateRange=custom&startdt={today}&enddt={today}&hits.hits._source=period_of_report,entity_name,file_date,form_type&hits.hits.total.value=true",
        "colorado_sos": "https://www.sos.state.co.us/biz/BusinessEntityCriteriaExt.do?nameTyp=EN&masterFileId=&entityId2=&queryParam={kw}&srchTyp=ENTITY&fileId=&partyId=&masterFileId=&action=getSearchResults",
        "uae_moei": "https://www.economy.gov.ae/english/Pages/search.aspx?k={kw}",
        "dmcc": "https://www.dmcc.ae/search#q={kw}",
        "faa_ast": "https://www.faa.gov/space/licenses?tab=commercial",
    }

    portal_list = [p.strip().lower() for p in portals.split(",") if p.strip()]
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _db()
    changes: list[dict] = []

    for portal in portal_list:
        if portal not in PORTAL_URLS:
            continue
        url_tpl = PORTAL_URLS[portal]
        for kw in kw_list:
            try:
                if portal == "sec_edgar":
                    # SEC EDGAR full-text search API
                    url = (
                        f"https://efts.sec.gov/LATEST/search-index?q=%22{kw}%22"
                        f"&dateRange=custom&startdt={today}&enddt={today}"
                    )
                else:
                    url = url_tpl.format(kw=kw.replace(" ", "+"), today=today)

                html = _get(url, timeout=20)
                content_hash = hashlib.sha256(html.encode()).hexdigest()

                row = conn.execute(
                    "SELECT hash FROM gov_monitor_hashes WHERE portal=? AND keyword=? AND url=?",
                    (portal, kw, url)
                ).fetchone()

                if row is None:
                    conn.execute(
                        "INSERT INTO gov_monitor_hashes VALUES (?,?,?,?,?)",
                        (portal, kw, url, content_hash, time.time())
                    )
                    changes.append({
                        "portal": portal, "keyword": kw,
                        "status": "first_check", "url": url
                    })
                elif row[0] != content_hash:
                    conn.execute(
                        "UPDATE gov_monitor_hashes SET hash=?, last_changed=? "
                        "WHERE portal=? AND keyword=? AND url=?",
                        (content_hash, time.time(), portal, kw, url)
                    )
                    # Try to extract relevant text
                    soup = BeautifulSoup(html, "lxml")
                    excerpt = soup.get_text(" ", strip=True)[:300]
                    changes.append({
                        "portal": portal, "keyword": kw,
                        "status": "CHANGED", "url": url,
                        "excerpt": excerpt,
                    })
            except Exception as exc:
                changes.append({"portal": portal, "keyword": kw, "error": str(exc)})

    conn.commit()
    conn.close()

    real_changes = [c for c in changes if c.get("status") == "CHANGED"]
    if alert_sms and real_changes:
        lines = [f"⚠️ {c['portal'].upper()} changed for '{c['keyword']}'" for c in real_changes[:3]]
        _sms("Jarvis Gov Alert:\n" + "\n".join(lines))

    return json.dumps({
        "checked": len(changes),
        "changes": real_changes,
        "first_checks": [c for c in changes if c.get("status") == "first_check"],
        "errors": [c for c in changes if "error" in c],
    }, indent=2)


def tool_conference_registrar(
    topics: str = "aerospace,AI,crypto,mining",
    months_ahead: int = 6,
    region: str = "global",
    max_results: int = 20,
) -> str:
    """
    Find upcoming conferences in given topics. Scrapes public conference listings.
    topics: comma-separated industry topics
    region: "global", "usa", "uae", "europe"
    Returns list of conferences with dates, speakers, agenda snippets, registration links.
    """
    topic_list = [t.strip() for t in topics.split(",") if t.strip()]
    output: list[dict] = []
    seen_urls: set[str] = set()

    region_qualifier = {
        "usa": "USA OR US OR \"United States\"",
        "uae": "UAE OR Dubai OR \"Abu Dhabi\"",
        "europe": "Europe OR London OR Amsterdam OR Berlin",
        "global": "",
    }.get(region.lower(), "")

    for topic in topic_list:
        q = f'"{topic}" conference 2025 2026 registration agenda speakers'
        if region_qualifier:
            q += f" {region_qualifier}"
        try:
            results = _ddg_html(q, max_results=10)
            for r in results:
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
                # Extract dates from snippet/title
                date_m = re.search(
                    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
                    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
                    r"Dec(?:ember)?)\s+\d{1,2}(?:[–\-]\d{1,2})?,?\s*20\d\d\b",
                    r["title"] + " " + r["snippet"], re.I
                )
                output.append({
                    "topic": topic,
                    "title": r["title"],
                    "date": date_m.group(0) if date_m else "see link",
                    "url": r["url"],
                    "snippet": r["snippet"][:200],
                })
            # Also check 10times.com
            q2 = f'site:10times.com "{topic}" conference upcoming'
            results2 = _ddg_html(q2, max_results=5)
            for r in results2:
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
                date_m = re.search(r"\b20\d\d\b", r["snippet"])
                output.append({
                    "topic": topic,
                    "title": r["title"],
                    "date": date_m.group(0) if date_m else "2025–2026",
                    "url": r["url"],
                    "snippet": r["snippet"][:200],
                    "source": "10times",
                })
        except Exception as exc:
            output.append({"topic": topic, "error": str(exc)})

        if len(output) >= max_results:
            break

    return json.dumps({
        "conferences": output[:max_results],
        "count": min(len(output), max_results),
    }, indent=2)


def tool_price_drop_hunter(
    action: str = "check",
    url: str = "",
    label: str = "",
    target_price: float = 0.0,
    alert_sms: bool = True,
) -> str:
    """
    Monitor any product/service URL for price changes.
    action: "add" (register URL), "check" (check all tracked URLs), "list" (show tracked)
    url: product page URL (required for "add")
    label: human-readable label for the product
    target_price: SMS alert when price drops below this (0 = alert on any drop)
    """
    conn = _db()

    if action == "list":
        rows = conn.execute(
            "SELECT url, title, price, currency, last_changed FROM price_history"
        ).fetchall()
        conn.close()
        return json.dumps([{
            "url": r[0], "title": r[1], "price": r[2],
            "currency": r[3], "last_changed": r[4]
        } for r in rows], indent=2)

    if action == "add":
        if not url:
            conn.close()
            return "Error: url is required for action=add"
        try:
            html = _get(url)
            price, currency = _extract_price(html, url)
            soup = BeautifulSoup(html, "lxml")
            title = label or (soup.title.string.strip() if soup.title else url)
            conn.execute(
                "INSERT OR REPLACE INTO price_history VALUES (?,?,?,?,?,?)",
                (url, title, price, currency, time.time(), time.time())
            )
            conn.commit()
            conn.close()
            return f"Tracking: {title} — current price: {currency} {price}"
        except Exception as exc:
            conn.close()
            return f"Error adding URL: {exc}"

    # action == "check"
    rows = conn.execute(
        "SELECT url, title, price, currency FROM price_history"
    ).fetchall()
    if url:
        rows = [r for r in rows if r[0] == url]
    if not rows:
        conn.close()
        return "No URLs tracked yet. Use action=add to register a product page."

    alerts: list[dict] = []
    for (tracked_url, title, old_price, currency) in rows:
        try:
            html = _get(tracked_url)
            new_price, new_currency = _extract_price(html, tracked_url)
            if new_price is None:
                continue
            conn.execute(
                "UPDATE price_history SET price=?, currency=?, last_checked=? WHERE url=?",
                (new_price, new_currency, time.time(), tracked_url)
            )
            if old_price is not None and new_price < old_price:
                drop_pct = round((old_price - new_price) / old_price * 100, 1)
                conn.execute(
                    "UPDATE price_history SET last_changed=? WHERE url=?",
                    (time.time(), tracked_url)
                )
                hit_target = target_price == 0.0 or new_price <= target_price
                alerts.append({
                    "title": title,
                    "old_price": f"{currency} {old_price}",
                    "new_price": f"{new_currency} {new_price}",
                    "drop_pct": drop_pct,
                    "url": tracked_url,
                    "alert": hit_target,
                })
        except Exception as exc:
            alerts.append({"title": title, "url": tracked_url, "error": str(exc)})

    conn.commit()
    conn.close()

    sms_items = [a for a in alerts if a.get("alert") and "error" not in a]
    if alert_sms and sms_items:
        lines = [f"💸 {a['title']}: {a['old_price']} → {a['new_price']} (-{a['drop_pct']}%)"
                 for a in sms_items[:3]]
        _sms("Jarvis Price Alert:\n" + "\n".join(lines))

    if not alerts:
        return f"All {len(rows)} tracked prices unchanged."
    return json.dumps({"price_changes": alerts, "total_tracked": len(rows)}, indent=2)


def tool_linkedin_prospect_scan(
    icp: str = "VP of Operations at aerospace company in Colorado",
    company_filters: str = "",
    max_results: int = 20,
    outreach_context: str = "",
) -> str:
    """
    Search LinkedIn public profiles via DuckDuckGo to find NexFlow ICP prospects.
    icp: ideal customer profile description (title + industry + location)
    company_filters: comma-separated company names to target specifically
    outreach_context: NexFlow value prop to match against prospect background
    Returns prospect list with LinkedIn URL, title, company, relevance notes.
    """
    prospects: list[dict] = []
    seen_urls: set[str] = set()

    # Parse ICP into search terms
    queries = [f'site:linkedin.com/in "{icp}"']

    # Add company-specific queries
    if company_filters:
        for company in company_filters.split(","):
            company = company.strip()
            if company:
                queries.append(
                    f'site:linkedin.com/in "{company}" '
                    + " ".join(f'"{w}"' for w in icp.split()[:4])
                )

    # Keyword variants
    icp_words = icp.split()
    if len(icp_words) >= 2:
        queries.append(f'site:linkedin.com/in {" ".join(icp_words[:5])}')

    for q in queries:
        try:
            results = _ddg_html(q, max_results=15)
            for r in results:
                if "linkedin.com/in/" not in r["url"]:
                    continue
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])

                # Extract name from title (usually "Name - Title - Company | LinkedIn")
                title_parts = re.split(r" [-–|] ", r["title"])
                name = title_parts[0].strip() if title_parts else r["title"]
                role = title_parts[1].strip() if len(title_parts) > 1 else ""
                company = title_parts[2].strip() if len(title_parts) > 2 else ""
                # Remove "| LinkedIn" suffix
                company = re.sub(r"\s*\|\s*LinkedIn$", "", company, flags=re.I)

                # Relevance scoring
                relevance_notes: list[str] = []
                text = (r["title"] + " " + r["snippet"]).lower()
                if outreach_context:
                    for word in outreach_context.lower().split()[:10]:
                        if len(word) > 4 and word in text:
                            relevance_notes.append(f"mentions '{word}'")

                prospects.append({
                    "name": name,
                    "role": role,
                    "company": company,
                    "linkedin_url": r["url"],
                    "snippet": r["snippet"][:200],
                    "relevance_notes": relevance_notes[:3],
                })
                if len(prospects) >= max_results:
                    break
        except Exception as exc:
            prospects.append({"error": str(exc), "query": q})
        if len(prospects) >= max_results:
            break

    return json.dumps({
        "icp": icp,
        "prospects_found": len([p for p in prospects if "error" not in p]),
        "prospects": prospects[:max_results],
    }, indent=2)


# ── MCP plumbing ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "real_estate_alert_bot",
        "description": (
            "Scrapes Zillow, Redfin, and Property Finder Dubai / Dubizzle for new listings "
            "matching price range, bedrooms, and location. Deduplicates via SQLite and "
            "SMS-texts Andre when new matches appear. Use action=check to run, action=status "
            "to see seen-listing counts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["check", "status"], "default": "check"},
                "min_price": {"type": "integer", "default": 0},
                "max_price": {"type": "integer", "default": 10000000},
                "min_beds": {"type": "integer", "default": 0},
                "location": {"type": "string", "default": "Denver CO"},
                "sources": {"type": "string", "default": "zillow,redfin",
                            "description": "comma-separated: zillow, redfin, dubizzle"},
                "alert_sms": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "competitor_job_intel",
        "description": (
            "Scrapes public job boards (LinkedIn, Greenhouse, Lever, Indeed) for target companies "
            "and extracts strategic signals: what they're building, hiring spikes, new initiatives. "
            "Returns categorized signals per company."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "companies": {"type": "string", "default": "OpenAI,Anthropic",
                              "description": "comma-separated company names"},
                "keywords": {"type": "string", "default": "",
                             "description": "optional filter keywords e.g. 'AI,ML,infra'"},
                "max_per_company": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "gov_portal_monitor",
        "description": (
            "Hash-based monitoring of government portals — SEC EDGAR, Colorado SOS, UAE MOEI, "
            "DMCC, FAA AST — for new filings, rule changes, or public notices matching keywords. "
            "SMS-alerts Andre when content changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "portals": {"type": "string", "default": "sec_edgar,colorado_sos",
                            "description": "comma-separated: sec_edgar, colorado_sos, uae_moei, dmcc, faa_ast"},
                "keywords": {"type": "string", "default": "NexFlow,aerospace,uranium"},
                "alert_sms": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "conference_registrar",
        "description": (
            "Finds upcoming conferences in aerospace, mining, AI, crypto, Dubai real estate "
            "and other topics. Scrapes DuckDuckGo and 10times.com for speaker lists, agenda, "
            "dates, and registration links."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topics": {"type": "string", "default": "aerospace,AI,crypto,mining",
                           "description": "comma-separated industry topics"},
                "months_ahead": {"type": "integer", "default": 6},
                "region": {"type": "string", "default": "global",
                           "enum": ["global", "usa", "uae", "europe"]},
                "max_results": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "price_drop_hunter",
        "description": (
            "Monitors any product page or service listing URL for price changes. "
            "Extracts price via JSON-LD, OpenGraph, and regex. Stores history in SQLite "
            "and SMS-alerts Andre on drops. action=add to register, action=check to scan all, "
            "action=list to see tracked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "check", "list"], "default": "check"},
                "url": {"type": "string", "default": ""},
                "label": {"type": "string", "default": ""},
                "target_price": {"type": "number", "default": 0.0,
                                 "description": "Alert when price drops below this (0=any drop)"},
                "alert_sms": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "linkedin_prospect_scan",
        "description": (
            "Given a NexFlow ICP description, searches LinkedIn public pages via DuckDuckGo "
            "and returns a list of best-fit prospects for outreach: name, role, company, "
            "LinkedIn URL, and relevance notes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "icp": {"type": "string",
                        "default": "VP of Operations at aerospace company in Colorado",
                        "description": "Ideal customer profile: title, industry, location"},
                "company_filters": {"type": "string", "default": "",
                                    "description": "comma-separated specific companies to target"},
                "max_results": {"type": "integer", "default": 20},
                "outreach_context": {"type": "string", "default": "",
                                     "description": "NexFlow value prop keywords for relevance scoring"},
            },
        },
    },
]

TOOL_FNS = {
    "real_estate_alert_bot": tool_real_estate_alert_bot,
    "competitor_job_intel": tool_competitor_job_intel,
    "gov_portal_monitor": tool_gov_portal_monitor,
    "conference_registrar": tool_conference_registrar,
    "price_drop_hunter": tool_price_drop_hunter,
    "linkedin_prospect_scan": tool_linkedin_prospect_scan,
}


@app.post("/")
async def handle(request: Request) -> JSONResponse:
    body = await request.json()
    method = body.get("method", "")
    req_id = body.get("id", 1)

    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "browser-server", "version": "1.0.0"},
            },
        })

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

    if method == "tools/call":
        name = body.get("params", {}).get("name", "")
        args = body.get("params", {}).get("arguments", {})
        fn = TOOL_FNS.get(name)
        if not fn:
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            })
        try:
            result = await asyncio.to_thread(fn, **args)
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": str(result)}]},
            })
        except Exception as exc:
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            })

    return JSONResponse({
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


@app.get("/")
async def healthcheck():
    return {"status": "ok", "server": "browser-server", "tools": len(TOOLS)}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8007, log_level="info")
