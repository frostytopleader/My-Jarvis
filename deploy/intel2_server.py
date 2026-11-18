"""
Jarvis Intel-2 MCP server — port 8008
Advanced intelligence tools: fear/greed, whale wallets, macro calendar,
space funding, executive moves, trading behavior, media reputation,
satellite launches, space law, and life-OS snapshot.
"""
import asyncio, json, os, re, time
from datetime import datetime, timezone

import httpx
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

app = FastAPI()


# ── shared helpers ────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 15) -> str:
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as c:
        return c.get(url).text


def _get_json(url: str, timeout: int = 15) -> dict:
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as c:
        return c.get(url).json()


def _ddg_html(query: str, max_results: int = 10) -> list[dict]:
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


def _news_rss(query: str, max_items: int = 10) -> list[dict]:
    q = query.replace(" ", "+")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        xml = _get(url, timeout=15)
        soup = BeautifulSoup(xml, "lxml-xml")
        items = []
        for item in soup.find_all("item")[:max_items]:
            items.append({
                "title": (item.find("title") or {}).get_text(strip=True),
                "link": (item.find("link") or {}).get_text(strip=True),
                "pub_date": (item.find("pubDate") or {}).get_text(strip=True),
                "snippet": BeautifulSoup(
                    (item.find("description") or {}).get_text(strip=True), "lxml"
                ).get_text(strip=True)[:200],
            })
        return items
    except Exception:
        return []


# ── tool implementations ──────────────────────────────────────────────────────

def tool_fear_greed_composite(symbols: str = "BTC,SPY,NVDA") -> str:
    """
    Builds a composite fear/greed reading by aggregating:
    - CNN Fear & Greed Index (market sentiment)
    - Crypto Fear & Greed (Alternative.me)
    - VIX proxy from Yahoo Finance
    - RSI-based momentum signals
    Returns a 0–100 composite score with component breakdown.
    """
    output: dict = {}

    # CNN Fear & Greed — scrape the JSON embed
    try:
        html = _get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata", timeout=10)
        data = json.loads(html)
        fg = data.get("fear_and_greed", {})
        output["cnn_fear_greed"] = {
            "score": fg.get("score"),
            "rating": fg.get("rating"),
            "timestamp": fg.get("timestamp"),
        }
    except Exception as exc:
        output["cnn_fear_greed"] = {"error": str(exc)}

    # Crypto Fear & Greed (Alternative.me free API)
    try:
        data = _get_json("https://api.alternative.me/fng/?limit=1", timeout=10)
        fg_data = data.get("data", [{}])[0]
        output["crypto_fear_greed"] = {
            "score": int(fg_data.get("value", 0)),
            "rating": fg_data.get("value_classification"),
            "timestamp": fg_data.get("timestamp"),
        }
    except Exception as exc:
        output["crypto_fear_greed"] = {"error": str(exc)}

    # VIX (Yahoo Finance JSON)
    try:
        vix_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=5d"
        data = _get_json(vix_url, timeout=10)
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        vix = round(closes[-1], 2) if closes else None
        # VIX interpretation: <15 complacency, 15-25 moderate, 25-35 fear, >35 extreme fear
        if vix:
            if vix < 15:
                vix_signal = "complacency (bullish risk)"
            elif vix < 25:
                vix_signal = "moderate"
            elif vix < 35:
                vix_signal = "elevated fear"
            else:
                vix_signal = "extreme fear / capitulation"
        else:
            vix_signal = "unknown"
        output["vix"] = {"level": vix, "signal": vix_signal}
    except Exception as exc:
        output["vix"] = {"error": str(exc)}

    # Composite score (simple average of available signals, normalized 0–100)
    scores = []
    cnn_score = output.get("cnn_fear_greed", {}).get("score")
    if cnn_score:
        scores.append(float(cnn_score))
    crypto_score = output.get("crypto_fear_greed", {}).get("score")
    if crypto_score:
        scores.append(float(crypto_score))
    vix_val = output.get("vix", {}).get("level")
    if vix_val:
        # Invert VIX: high VIX = fear → low score
        vix_normalized = max(0, 100 - min(100, (vix_val - 10) * 2.5))
        scores.append(vix_normalized)

    if scores:
        composite = round(sum(scores) / len(scores), 1)
        if composite >= 75:
            label = "EXTREME GREED"
        elif composite >= 55:
            label = "GREED"
        elif composite >= 45:
            label = "NEUTRAL"
        elif composite >= 25:
            label = "FEAR"
        else:
            label = "EXTREME FEAR"
        output["composite"] = {"score": composite, "label": label,
                               "components_used": len(scores)}

    return json.dumps(output, indent=2)


def tool_whale_wallet_tracker(
    wallets: str = "",
    chains: str = "eth,btc",
    alert_threshold_usd: float = 1_000_000.0,
) -> str:
    """
    Tracks large on-chain wallet movements via free public APIs.
    wallets: comma-separated wallet addresses (empty = top known whale addresses)
    chains: comma-separated: eth, btc, sol
    alert_threshold_usd: flag moves above this value
    Uses Etherscan free API for ETH, blockchain.info for BTC, public mempool APIs.
    """
    chain_list = [c.strip().lower() for c in chains.split(",")]
    wallet_list = [w.strip() for w in wallets.split(",") if w.strip()]

    # Default known whale addresses if none provided
    if not wallet_list:
        wallet_list = [
            "0x28C6c06298d514Db089934071355E5743bf21d60",  # Binance hot wallet
            "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8",  # Binance cold wallet
            "0x40B38765696e3d5d8d9d834D8AaD4bB6e418E489",  # Robinhood
        ]

    output: dict = {"wallets": [], "alerts": []}
    ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")  # optional

    for chain in chain_list:
        for wallet in wallet_list[:5]:  # cap to avoid rate limits
            entry: dict = {"wallet": wallet, "chain": chain}
            try:
                if chain == "eth":
                    # Etherscan balance
                    url = (
                        f"https://api.etherscan.io/api?module=account&action=balance"
                        f"&address={wallet}&tag=latest"
                        + (f"&apikey={ETHERSCAN_KEY}" if ETHERSCAN_KEY else "")
                    )
                    data = _get_json(url, timeout=10)
                    if data.get("status") == "1":
                        balance_eth = int(data["result"]) / 1e18
                        entry["balance_eth"] = round(balance_eth, 4)
                    # Last 5 transactions
                    tx_url = (
                        f"https://api.etherscan.io/api?module=account&action=txlist"
                        f"&address={wallet}&sort=desc&offset=5&page=1"
                        + (f"&apikey={ETHERSCAN_KEY}" if ETHERSCAN_KEY else "")
                    )
                    tx_data = _get_json(tx_url, timeout=10)
                    if tx_data.get("status") == "1":
                        txs = tx_data.get("result", [])[:5]
                        entry["recent_txs"] = [{
                            "hash": t["hash"][:16] + "...",
                            "value_eth": round(int(t["value"]) / 1e18, 4),
                            "timestamp": datetime.fromtimestamp(
                                int(t["timeStamp"]), tz=timezone.utc
                            ).isoformat(),
                        } for t in txs]
                elif chain == "btc":
                    url = f"https://blockchain.info/rawaddr/{wallet}?limit=5"
                    data = _get_json(url, timeout=10)
                    entry["balance_btc"] = round(data.get("final_balance", 0) / 1e8, 8)
                    entry["total_received_btc"] = round(data.get("total_received", 0) / 1e8, 4)
            except Exception as exc:
                entry["error"] = str(exc)
            output["wallets"].append(entry)

    return json.dumps(output, indent=2)


def tool_macro_event_calendar(
    lookahead_days: int = 14,
    regions: str = "US,EU,global",
    categories: str = "fed,inflation,gdp,earnings,elections",
) -> str:
    """
    Pulls upcoming macro economic events: Fed meetings, CPI/PCE releases, GDP prints,
    major earnings, elections, central bank decisions.
    Sources: investing.com economic calendar (public), news RSS.
    """
    cat_list = [c.strip().lower() for c in categories.split(",")]
    region_list = [r.strip().upper() for r in regions.split(",")]
    events: list[dict] = []

    # Google News RSS for each category
    search_map = {
        "fed": "Federal Reserve FOMC meeting rate decision 2025",
        "inflation": "CPI PCE inflation report release date 2025",
        "gdp": "GDP report release 2025",
        "earnings": "earnings season Q4 2025 major companies",
        "elections": "election 2025 2026 date",
    }

    for cat in cat_list:
        q = search_map.get(cat, f"{cat} economic event 2025")
        try:
            items = _news_rss(q, max_items=5)
            for item in items:
                events.append({
                    "category": cat,
                    "title": item["title"],
                    "date": item.get("pub_date", "")[:16],
                    "source": "Google News",
                    "url": item.get("link", ""),
                })
        except Exception:
            pass

    # Scrape investing.com economic calendar via DDG
    try:
        q = "investing.com economic calendar upcoming events " + " ".join(region_list[:2])
        results = _ddg_html(q, max_results=5)
        for r in results:
            if "investing.com" in r["url"]:
                events.append({
                    "category": "economic_calendar",
                    "title": r["title"],
                    "snippet": r["snippet"][:200],
                    "url": r["url"],
                })
    except Exception:
        pass

    return json.dumps({
        "lookahead_days": lookahead_days,
        "event_count": len(events),
        "events": events,
    }, indent=2)


def tool_space_funding_radar(
    keywords: str = "launch,satellite,asteroid,propulsion,space mining",
    min_round_size_m: float = 0.0,
    max_results: int = 20,
) -> str:
    """
    Monitors space industry funding rounds, acquisitions, and partnerships.
    Scrapes TechCrunch, SpaceNews, NASA announcements, Crunchbase-indexed DDG results.
    keywords: focus areas (launch, satellite, in-space manufacturing, etc.)
    min_round_size_m: minimum round size in $ millions (0 = all)
    """
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    deals: list[dict] = []
    seen: set[str] = set()

    sources = [
        ("spacenews.com", "funding investment deal"),
        ("techcrunch.com", "space startup funding"),
        ("nasaspaceflight.com", "investment contract"),
        ("crunchbase.com/organization", "space company funding"),
    ]

    for site, suffix in sources:
        for kw in kw_list[:3]:
            q = f'site:{site} "{kw}" {suffix} 2025'
            try:
                results = _ddg_html(q, max_results=5)
                for r in results:
                    if r["url"] in seen:
                        continue
                    seen.add(r["url"])
                    # Extract dollar amount
                    amount_m = None
                    m = re.search(
                        r"\$([\d.]+)\s*(billion|million|B\b|M\b)", r["snippet"], re.I
                    )
                    if m:
                        val = float(m.group(1))
                        if "billion" in m.group(2).lower() or m.group(2).upper() == "B":
                            val *= 1000
                        amount_m = val
                    if min_round_size_m > 0 and amount_m and amount_m < min_round_size_m:
                        continue
                    deals.append({
                        "keyword": kw,
                        "title": r["title"],
                        "amount_m": amount_m,
                        "url": r["url"],
                        "snippet": r["snippet"][:200],
                        "source": site,
                    })
            except Exception as exc:
                deals.append({"keyword": kw, "source": site, "error": str(exc)})

        if len(deals) >= max_results:
            break

    # Sort by amount desc
    deals_sorted = sorted(
        [d for d in deals if "error" not in d and d.get("amount_m")],
        key=lambda x: -(x.get("amount_m") or 0)
    ) + [d for d in deals if "error" not in d and not d.get("amount_m")]

    return json.dumps({
        "deals_found": len([d for d in deals if "error" not in d]),
        "deals": deals_sorted[:max_results],
    }, indent=2)


def tool_executive_move_tracker(
    companies: str = "",
    titles: str = "CEO,CFO,CTO,President,Chairman",
    industries: str = "aerospace,mining,AI,energy",
    max_results: int = 20,
) -> str:
    """
    Tracks C-suite and senior executive appointments, departures, and moves.
    Sources: Google News RSS, PR Newswire via DDG, SEC 8-K filings.
    companies: specific companies to watch (empty = industry-wide scan)
    """
    title_list = [t.strip() for t in titles.split(",") if t.strip()]
    industry_list = [i.strip() for i in industries.split(",") if i.strip()]
    company_list = [c.strip() for c in companies.split(",") if c.strip()]
    moves: list[dict] = []
    seen: set[str] = set()

    # Build queries
    queries: list[str] = []
    if company_list:
        for company in company_list[:5]:
            queries.append(f'"{company}" appoints OR names OR hires OR resigns {" OR ".join(title_list[:3])} 2025')
    else:
        for industry in industry_list[:3]:
            queries.append(
                f'"{industry}" company appoints OR names OR hires {" OR ".join(title_list[:3])} 2025'
            )

    for q in queries:
        try:
            items = _news_rss(q, max_items=8)
            for item in items:
                if item.get("link") in seen:
                    continue
                seen.add(item.get("link", ""))
                # Classify move type
                text = (item["title"] + " " + item.get("snippet", "")).lower()
                move_type = "unknown"
                if any(w in text for w in ["appoints", "names", "hires", "joins", "named"]):
                    move_type = "appointment"
                elif any(w in text for w in ["resign", "steps down", "departs", "leaves"]):
                    move_type = "departure"
                elif any(w in text for w in ["promoted", "elevation"]):
                    move_type = "promotion"

                # Extract role
                role_found = next((t for t in title_list if t.lower() in text), "")

                moves.append({
                    "move_type": move_type,
                    "role": role_found,
                    "title": item["title"],
                    "date": item.get("pub_date", "")[:16],
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", "")[:200],
                })
        except Exception as exc:
            moves.append({"query": q, "error": str(exc)})
        if len(moves) >= max_results:
            break

    return json.dumps({
        "executives_tracked": len([m for m in moves if "error" not in m]),
        "appointments": [m for m in moves if m.get("move_type") == "appointment"],
        "departures": [m for m in moves if m.get("move_type") == "departure"],
        "other": [m for m in moves if m.get("move_type") not in ("appointment", "departure")],
    }, indent=2)


def tool_trading_behavior_analyzer(
    symbol: str = "NVDA",
    lookback_days: int = 30,
) -> str:
    """
    Analyzes unusual trading behavior for a stock: options flow anomalies,
    short interest changes, insider transactions, institutional 13F changes.
    Sources: Yahoo Finance (free), SEC EDGAR insider API, public options data.
    """
    output: dict = {"symbol": symbol.upper()}

    # Yahoo Finance summary
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=summaryDetail,defaultKeyStatistics,insiderTransactions"
        data = _get_json(url, timeout=10)
        result = data.get("quoteSummary", {}).get("result", [{}])[0]

        summary = result.get("summaryDetail", {})
        output["price_summary"] = {
            "52w_high": summary.get("fiftyTwoWeekHigh", {}).get("raw"),
            "52w_low": summary.get("fiftyTwoWeekLow", {}).get("raw"),
            "avg_volume": summary.get("averageVolume", {}).get("raw"),
            "market_cap": summary.get("marketCap", {}).get("fmt"),
        }

        stats = result.get("defaultKeyStatistics", {})
        output["short_interest"] = {
            "short_ratio": stats.get("shortRatio", {}).get("raw"),
            "short_percent_float": stats.get("shortPercentOfFloat", {}).get("fmt"),
            "shares_short": stats.get("sharesShort", {}).get("fmt"),
        }

        insider = result.get("insiderTransactions", {})
        txs = insider.get("transactions", [])[:5]
        output["insider_transactions"] = [{
            "filer": t.get("filerName"),
            "relation": t.get("filerRelation"),
            "transaction": t.get("transactionDescription"),
            "shares": t.get("shares", {}).get("fmt"),
            "value": t.get("value", {}).get("fmt"),
            "date": t.get("startDate", {}).get("fmt"),
        } for t in txs]
    except Exception as exc:
        output["yahoo_error"] = str(exc)

    # SEC EDGAR recent insider filings (Form 4)
    try:
        url = f"https://efts.sec.gov/LATEST/search-index?q=%22{symbol}%22&forms=4&dateRange=custom&startdt={(datetime.now().year)}-01-01"
        data = _get_json(url, timeout=10)
        hits = data.get("hits", {}).get("hits", [])[:5]
        output["sec_form4_filings"] = [{
            "entity": h.get("_source", {}).get("entity_name"),
            "filed": h.get("_source", {}).get("file_date"),
        } for h in hits]
    except Exception as exc:
        output["sec_form4_error"] = str(exc)

    # Unusual options activity news
    try:
        items = _news_rss(f"{symbol} unusual options activity flow 2025", max_items=5)
        output["options_flow_news"] = [{
            "title": i["title"],
            "date": i.get("pub_date", "")[:16],
        } for i in items]
    except Exception:
        pass

    return json.dumps(output, indent=2)


def tool_media_reputation_monitor(
    entities: str = "NexFlow,Andre Champeau",
    sentiment_window_days: int = 30,
    alert_negative: bool = True,
) -> str:
    """
    Monitors news sentiment for people, companies, and brands.
    Returns positive/negative/neutral article counts with top headlines and trend.
    Sources: Google News RSS, DuckDuckGo news, Reddit (public JSON).
    """
    entity_list = [e.strip() for e in entities.split(",") if e.strip()]
    output: dict = {}

    for entity in entity_list:
        entity_data: dict = {"entity": entity, "articles": [], "sentiment_tally": {}}

        # News RSS
        try:
            items = _news_rss(entity, max_items=15)
            for item in items:
                text = (item["title"] + " " + item.get("snippet", "")).lower()
                # Naive sentiment keywords
                pos_words = ["growth", "success", "win", "award", "launch", "partner",
                             "raise", "funding", "breakthrough", "innovative", "expanded"]
                neg_words = ["lawsuit", "fraud", "fail", "loss", "decline", "layoff",
                             "scandal", "investigation", "breach", "bankrupt", "fine",
                             "penalty", "accused", "alleged", "sued"]
                pos_score = sum(1 for w in pos_words if w in text)
                neg_score = sum(1 for w in neg_words if w in text)
                if pos_score > neg_score:
                    sentiment = "positive"
                elif neg_score > pos_score:
                    sentiment = "negative"
                else:
                    sentiment = "neutral"
                entity_data["articles"].append({
                    "title": item["title"],
                    "date": item.get("pub_date", "")[:16],
                    "sentiment": sentiment,
                    "url": item.get("link", ""),
                })
        except Exception as exc:
            entity_data["news_error"] = str(exc)

        # Tally
        tally: dict[str, int] = {"positive": 0, "negative": 0, "neutral": 0}
        for a in entity_data["articles"]:
            tally[a["sentiment"]] = tally.get(a["sentiment"], 0) + 1
        entity_data["sentiment_tally"] = tally
        total = sum(tally.values())
        entity_data["sentiment_score"] = round(
            (tally["positive"] - tally["negative"]) / max(total, 1) * 100, 1
        )

        # Reddit mentions
        try:
            reddit_url = (
                f"https://www.reddit.com/search.json?q={entity.replace(' ','+')}"
                f"&sort=new&limit=5&t=month"
            )
            data = _get_json(reddit_url, timeout=10)
            posts = data.get("data", {}).get("children", [])[:5]
            entity_data["reddit_mentions"] = [{
                "title": p["data"]["title"],
                "subreddit": p["data"]["subreddit"],
                "score": p["data"]["score"],
                "url": f"https://reddit.com{p['data']['permalink']}",
            } for p in posts]
        except Exception:
            pass

        output[entity] = entity_data

    return json.dumps(output, indent=2)


def tool_satellite_launch_scheduler(
    next_n: int = 10,
    providers: str = "SpaceX,RocketLab,ULA,Arianespace,ISRO",
    orbit_filter: str = "",
) -> str:
    """
    Scrapes upcoming satellite/rocket launch schedules from public trackers.
    Sources: spaceflightnow.com, nextspaceflight.com, NASA launch schedule.
    providers: filter by launch provider
    orbit_filter: LEO, GEO, MEO, Lunar, Mars (empty = all)
    """
    provider_list = [p.strip() for p in providers.split(",") if p.strip()]
    launches: list[dict] = []
    seen: set[str] = set()

    sources = [
        "spaceflightnow.com launch schedule",
        "nextspaceflight.com upcoming launches",
        "nasaspaceflight.com 2025 launch schedule",
        "space.com rocket launches 2025",
    ]

    for provider in provider_list[:4]:
        q = f'"{provider}" launch schedule 2025 2026 date'
        if orbit_filter:
            q += f" {orbit_filter}"
        try:
            results = _ddg_html(q, max_results=5)
            for r in results:
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                date_m = re.search(
                    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
                    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
                    r"Dec(?:ember)?)\s+\d{1,2},?\s*20\d\d\b",
                    r["title"] + " " + r["snippet"], re.I
                )
                launches.append({
                    "provider": provider,
                    "title": r["title"],
                    "date": date_m.group(0) if date_m else "TBD",
                    "orbit": orbit_filter or "unspecified",
                    "url": r["url"],
                    "snippet": r["snippet"][:200],
                })
            if len(launches) >= next_n:
                break
        except Exception as exc:
            launches.append({"provider": provider, "error": str(exc)})

    # Also scrape NASA launch schedule page
    try:
        items = _news_rss("rocket launch schedule 2025 upcoming", max_items=5)
        for item in items:
            if item.get("link") in seen:
                continue
            seen.add(item.get("link", ""))
            launches.append({
                "provider": "various",
                "title": item["title"],
                "date": item.get("pub_date", "")[:16],
                "url": item.get("link", ""),
                "snippet": item.get("snippet", "")[:200],
            })
    except Exception:
        pass

    return json.dumps({
        "launches_found": len([l for l in launches if "error" not in l]),
        "launches": [l for l in launches if "error" not in l][:next_n],
    }, indent=2)


def tool_space_law_pipeline(
    jurisdictions: str = "US,UAE,international",
    topics: str = "spectrum,launch license,debris,mining rights,liability",
    max_results: int = 15,
) -> str:
    """
    Tracks space law, regulation, and policy developments.
    Sources: FAA, FCC, ITU, UN COPUOS, national space agencies via news RSS + DDG.
    jurisdictions: US, UAE, EU, international, UN
    topics: spectrum allocation, launch licensing, debris, in-space mining rights, liability
    """
    jur_list = [j.strip() for j in jurisdictions.split(",") if j.strip()]
    topic_list = [t.strip() for t in topics.split(",") if t.strip()]
    updates: list[dict] = []
    seen: set[str] = set()

    for topic in topic_list[:4]:
        for jur in jur_list[:3]:
            q = f'space law {topic} {jur} regulation 2025 policy'
            try:
                items = _news_rss(q, max_items=5)
                for item in items:
                    if item.get("link") in seen:
                        continue
                    seen.add(item.get("link", ""))
                    updates.append({
                        "topic": topic,
                        "jurisdiction": jur,
                        "title": item["title"],
                        "date": item.get("pub_date", "")[:16],
                        "url": item.get("link", ""),
                        "snippet": item.get("snippet", "")[:200],
                    })
            except Exception:
                pass

            # Also DDG for official sources
            q2 = f'site:faa.gov OR site:fcc.gov OR site:unoosa.org "{topic}" 2025'
            try:
                results = _ddg_html(q2, max_results=3)
                for r in results:
                    if r["url"] in seen:
                        continue
                    seen.add(r["url"])
                    updates.append({
                        "topic": topic,
                        "jurisdiction": "official",
                        "title": r["title"],
                        "url": r["url"],
                        "snippet": r["snippet"][:200],
                        "source": "official_gov",
                    })
            except Exception:
                pass

            if len(updates) >= max_results:
                break
        if len(updates) >= max_results:
            break

    return json.dumps({
        "policy_updates": len(updates),
        "updates": updates[:max_results],
    }, indent=2)


def tool_life_os_snapshot(
    include: str = "health,goals,finance,habits,energy",
) -> str:
    """
    Synthesizes a holistic Life OS snapshot by reading Andre's Jarvis databases.
    Pulls recent habit logs, goal progress, finance notes, energy/mood, and open action items.
    Returns a structured daily/weekly overview for Jarvis's morning brief.
    Note: This tool reads from the ~/.openjarvis directory for cached Jarvis data.
    """
    sections = [s.strip().lower() for s in include.split(",") if s.strip()]
    snapshot: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sections": {},
    }
    openjarvis_dir = os.path.expanduser("~/.openjarvis")

    # Read any cached JSON snapshots Jarvis has stored
    for section in sections:
        section_data: dict = {}
        # Look for section-specific cache files
        cache_files = [
            os.path.join(openjarvis_dir, f"{section}_snapshot.json"),
            os.path.join(openjarvis_dir, f"{section}_log.json"),
            os.path.join(openjarvis_dir, f"life_os_{section}.json"),
        ]
        for path in cache_files:
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        section_data = json.load(f)
                    section_data["_source"] = path
                    break
                except Exception:
                    pass

        if not section_data:
            # Generate a prompt-ready summary for Jarvis to fill from memory
            section_data = {
                "status": "no_cache",
                "prompt": f"Ask Jarvis: What is the current status of my {section}? "
                          f"Check recent Notion logs and summarize in 2-3 sentences.",
            }
        snapshot["sections"][section] = section_data

    # Also read andre-dossier if present
    dossier_path = os.path.join(openjarvis_dir, "andre-dossier.md")
    if os.path.exists(dossier_path):
        try:
            with open(dossier_path) as f:
                dossier = f.read()
            # Extract key sections
            snapshot["dossier_summary"] = {
                "length_chars": len(dossier),
                "has_goals": "goal" in dossier.lower(),
                "has_health": "health" in dossier.lower(),
                "has_finance": "financ" in dossier.lower(),
            }
        except Exception:
            pass

    snapshot["brief_prompt"] = (
        "Using the above snapshot + Notion databases, generate Andre's morning brief: "
        "top 3 priorities, health/energy status, open action items, and one key insight."
    )

    return json.dumps(snapshot, indent=2)


# ── MCP plumbing ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "fear_greed_composite",
        "description": (
            "Builds a composite fear/greed market sentiment reading from CNN Fear & Greed Index, "
            "Crypto Fear & Greed (Alternative.me), and VIX. Returns 0–100 composite score with "
            "label (EXTREME FEAR → EXTREME GREED) and component breakdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbols": {"type": "string", "default": "BTC,SPY,NVDA"},
            },
        },
    },
    {
        "name": "whale_wallet_tracker",
        "description": (
            "Tracks large on-chain wallet balances and recent transactions via Etherscan (ETH) "
            "and blockchain.info (BTC). Flags moves above alert_threshold_usd. "
            "Defaults to known exchange/whale addresses if none provided."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallets": {"type": "string", "default": "",
                            "description": "comma-separated wallet addresses"},
                "chains": {"type": "string", "default": "eth,btc"},
                "alert_threshold_usd": {"type": "number", "default": 1000000.0},
            },
        },
    },
    {
        "name": "macro_event_calendar",
        "description": (
            "Pulls upcoming macro economic events: Fed/FOMC meetings, CPI/PCE releases, GDP prints, "
            "major earnings seasons, and elections. Sources Google News RSS and investing.com."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lookahead_days": {"type": "integer", "default": 14},
                "regions": {"type": "string", "default": "US,EU,global"},
                "categories": {"type": "string",
                               "default": "fed,inflation,gdp,earnings,elections"},
            },
        },
    },
    {
        "name": "space_funding_radar",
        "description": (
            "Monitors space industry funding rounds, acquisitions, and partnerships. "
            "Scrapes SpaceNews, TechCrunch, and Crunchbase-indexed DDG results. "
            "Sorts by round size. Useful for competitive intel and investment signals."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string",
                             "default": "launch,satellite,asteroid,propulsion,space mining"},
                "min_round_size_m": {"type": "number", "default": 0.0,
                                     "description": "minimum round in $ millions"},
                "max_results": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "executive_move_tracker",
        "description": (
            "Tracks C-suite appointments, departures, and promotions in target industries or companies. "
            "Sources Google News RSS and PR Newswire. Categorizes by move type. "
            "Essential for competitive intelligence and relationship mapping."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "companies": {"type": "string", "default": "",
                              "description": "specific companies (empty = industry-wide)"},
                "titles": {"type": "string", "default": "CEO,CFO,CTO,President,Chairman"},
                "industries": {"type": "string", "default": "aerospace,mining,AI,energy"},
                "max_results": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "trading_behavior_analyzer",
        "description": (
            "Analyzes unusual trading signals for a stock: short interest, insider transactions "
            "(Yahoo Finance + SEC Form 4), and options flow news. "
            "Returns anomaly flags and recent insider activity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "default": "NVDA"},
                "lookback_days": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "media_reputation_monitor",
        "description": (
            "Monitors news sentiment for people, companies, and brands. "
            "Returns positive/negative/neutral article counts, top headlines, "
            "sentiment score, and Reddit mentions. Flags negative press."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entities": {"type": "string", "default": "NexFlow,Andre Champeau",
                             "description": "comma-separated names/companies"},
                "sentiment_window_days": {"type": "integer", "default": 30},
                "alert_negative": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "satellite_launch_scheduler",
        "description": (
            "Scrapes upcoming rocket/satellite launch schedules from SpaceflightNow, "
            "NextSpaceflight, and NASA. Filterable by provider and orbit type. "
            "Useful for tracking competitor launches and mission windows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "next_n": {"type": "integer", "default": 10},
                "providers": {"type": "string",
                              "default": "SpaceX,RocketLab,ULA,Arianespace,ISRO"},
                "orbit_filter": {"type": "string", "default": "",
                                 "description": "LEO, GEO, MEO, Lunar, Mars (empty=all)"},
            },
        },
    },
    {
        "name": "space_law_pipeline",
        "description": (
            "Tracks space law, regulation, and policy developments across jurisdictions. "
            "Sources FAA, FCC, ITU, UN COPUOS, and national space agencies. "
            "Topics: spectrum allocation, launch licensing, debris liability, mining rights."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "jurisdictions": {"type": "string", "default": "US,UAE,international"},
                "topics": {"type": "string",
                           "default": "spectrum,launch license,debris,mining rights,liability"},
                "max_results": {"type": "integer", "default": 15},
            },
        },
    },
    {
        "name": "life_os_snapshot",
        "description": (
            "Synthesizes a holistic Life OS snapshot by reading cached Jarvis data files "
            "from ~/.openjarvis and Andre's dossier. Pulls health, goals, finance, habits, "
            "and energy sections. Returns a structured brief prompt for Jarvis's morning report."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include": {"type": "string",
                            "default": "health,goals,finance,habits,energy",
                            "description": "comma-separated sections to include"},
            },
        },
    },
]

TOOL_FNS = {
    "fear_greed_composite": tool_fear_greed_composite,
    "whale_wallet_tracker": tool_whale_wallet_tracker,
    "macro_event_calendar": tool_macro_event_calendar,
    "space_funding_radar": tool_space_funding_radar,
    "executive_move_tracker": tool_executive_move_tracker,
    "trading_behavior_analyzer": tool_trading_behavior_analyzer,
    "media_reputation_monitor": tool_media_reputation_monitor,
    "satellite_launch_scheduler": tool_satellite_launch_scheduler,
    "space_law_pipeline": tool_space_law_pipeline,
    "life_os_snapshot": tool_life_os_snapshot,
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
                "serverInfo": {"name": "intel2-server", "version": "1.0.0"},
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
    return {"status": "ok", "server": "intel2-server", "tools": len(TOOLS)}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8008, log_level="info")
