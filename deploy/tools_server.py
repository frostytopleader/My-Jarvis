"""
Jarvis Custom Tools MCP Server — port 8003 (internal only)
Tools:
  Core    : read_file, write_note
  Calendar: get_calendar, create_calendar_event
  Browser : web_scrape, screenshot_page, web_form_fill,
            monitor_website, pdf_extractor
  Monitor : watch_url, unwatch_url, list_watches
  Trading : crypto_price, market_news, options_data,
            trading_desk_status, portfolio_snapshot,
            risk_dashboard, strategy_perf

Trading desk bridge tools (last 4) require TRADING_DESK_URL pointing at
deploy/trading_bridge.py running on the Windows trading desk machine.
Google Calendar requires: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
  GOOGLE_REFRESH_TOKEN (optionally GOOGLE_CALENDAR_ID, default "primary")
Twilio SMS alerts require: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
  TWILIO_PHONE_NUMBER, JARVIS_NOTIFY_NUMBER (default +13032699875)
Browser tools require playwright + pypdf (installed in build.sh).
"""
import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

try:
    import pypdf
    PYPDF_OK = True
except ImportError:
    PYPDF_OK = False

# ── Paths ──────────────────────────────────────────────────────────────────────
JARVIS_DIR = Path(os.path.expanduser("~/.openjarvis"))
NOTES_DIR  = JARVIS_DIR / "notes"

# ── Google credentials ─────────────────────────────────────────────────────────
GC_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GC_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GC_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
GC_CALENDAR_ID   = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

GCAL_MISSING = (
    "Google Calendar not configured. "
    "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN "
    "in Railway environment variables."
)

# ── Twilio (for proactive SMS alerts from background monitor) ──────────────────
TWILIO_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM   = os.environ.get("TWILIO_PHONE_NUMBER", "")
TWILIO_TO     = os.environ.get("JARVIS_NOTIFY_NUMBER", "+13032699875")

# How often the background watcher polls (default 10 min; override with env var)
MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL_SECONDS", "600"))

# ── Trading desk bridge ────────────────────────────────────────────────────────
# Set TRADING_DESK_URL to the URL of trading_bridge.py running on the Windows
# trading desk machine (e.g. https://xxxx.ngrok.io or http://local-ip:7001).
TRADING_DESK_URL = os.environ.get("TRADING_DESK_URL", "").rstrip("/")

TRADING_DESK_SETUP_MSG = (
    "Trading desk bridge not configured.\n\n"
    "To connect:\n"
    "1. Run deploy/trading_bridge.py on your Windows trading desk machine\n"
    "2. Expose it via ngrok: ngrok http 7001\n"
    "3. Add TRADING_DESK_URL=https://xxxx.ngrok.io to Railway Variables\n"
    "4. Redeploy — all trading desk tools will activate immediately."
)

# CoinGecko coin ID mapping (ticker → coingecko id)
COIN_IDS: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "AVAX": "avalanche-2", "DOGE": "dogecoin",
    "DOT": "polkadot", "LINK": "chainlink", "MATIC": "matic-network",
    "POL": "matic-network", "UNI": "uniswap", "ATOM": "cosmos", "LTC": "litecoin",
    "BCH": "bitcoin-cash", "NEAR": "near", "APT": "aptos", "ARB": "arbitrum",
    "OP": "optimism", "SUI": "sui", "INJ": "injective-protocol", "TIA": "celestia",
    "JUP": "jupiter-exchange-solana", "PYTH": "pyth-network", "W": "wormhole",
    "FET": "fetch-ai", "RENDER": "render-token", "PEPE": "pepe",
    "WIF": "dogwifcoin", "BONK": "bonk", "HBAR": "hedera-hashgraph",
    "VET": "vechain", "FIL": "filecoin", "ICP": "internet-computer",
    "ALGO": "algorand", "XLM": "stellar", "SAND": "the-sandbox",
    "MANA": "decentraland", "CRV": "curve-dao-token", "AAVE": "aave",
    "MKR": "maker", "SNX": "synthetix-network-token", "COMP": "compound-governance-token",
    "1INCH": "1inch", "SUSHI": "sushi", "LDO": "lido-dao",
}


# ── Tool implementations ───────────────────────────────────────────────────────

def tool_read_file(path: str) -> str:
    target = (JARVIS_DIR / path.lstrip("/")).resolve()
    if not str(target).startswith(str(JARVIS_DIR.resolve())):
        return "Error: access denied — only paths within ~/.openjarvis/ are allowed."
    if not target.exists():
        # Give a helpful listing of what IS there
        items = sorted(JARVIS_DIR.rglob("*"))
        names = [str(p.relative_to(JARVIS_DIR)) for p in items if p.is_file()][:20]
        return f"File not found: {path}\n\nAvailable files:\n" + "\n".join(f"  {n}" for n in names)
    if not target.is_file():
        return f"Error: {path} is a directory, not a file."
    return target.read_text(encoding="utf-8")


def tool_write_note(title: str, content: str) -> str:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:60].strip()
    filename = f"{ts}-{safe_title}.md"
    path = NOTES_DIR / filename
    full = f"# {title}\n_Saved by Jarvis: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n{content}\n"
    path.write_text(full, encoding="utf-8")
    return f"Note saved: {filename}"


def _gcal_access_token() -> str:
    if not all([GC_CLIENT_ID, GC_CLIENT_SECRET, GC_REFRESH_TOKEN]):
        raise RuntimeError(GCAL_MISSING)
    data = urllib.parse.urlencode({
        "client_id":     GC_CLIENT_ID,
        "client_secret": GC_CLIENT_SECRET,
        "refresh_token": GC_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def tool_get_calendar(days_ahead: int = 7) -> str:
    try:
        token = _gcal_access_token()
    except RuntimeError as e:
        return str(e)
    now      = datetime.now(timezone.utc)
    time_min = urllib.parse.quote(now.isoformat())
    time_max = urllib.parse.quote((now + timedelta(days=days_ahead)).isoformat())
    cal_enc  = urllib.parse.quote(GC_CALENDAR_ID)
    url = (
        f"https://www.googleapis.com/calendar/v3/calendars/{cal_enc}/events"
        f"?timeMin={time_min}&timeMax={time_max}"
        f"&singleEvents=true&orderBy=startTime&maxResults=25"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    events = data.get("items", [])
    if not events:
        return f"No events in the next {days_ahead} days."
    lines = [f"Next {days_ahead} days — {len(events)} event(s):\n"]
    for e in events:
        start   = e.get("start", {})
        dt      = start.get("dateTime", start.get("date", "?"))
        summary = e.get("summary", "(no title)")
        loc     = e.get("location", "")
        line    = f"• {dt[:16].replace('T', ' ')}  {summary}"
        if loc:
            line += f"  @ {loc}"
        lines.append(line)
    return "\n".join(lines)


def tool_create_calendar_event(
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
) -> str:
    try:
        token = _gcal_access_token()
    except RuntimeError as e:
        return str(e)
    cal_enc = urllib.parse.quote(GC_CALENDAR_ID)
    url     = f"https://www.googleapis.com/calendar/v3/calendars/{cal_enc}/events"
    body    = json.dumps({
        "summary":     summary,
        "description": description,
        "location":    location,
        "start": {"dateTime": start, "timeZone": "America/Denver"},
        "end":   {"dateTime": end,   "timeZone": "America/Denver"},
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        event = json.loads(r.read())
    link = event.get("htmlLink", "")
    return f"Event created: '{summary}' on {start[:16]}.\nCalendar link: {link}"


# ── Browser helpers ────────────────────────────────────────────────────────────

BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
SCREENSHOTS_DIR = JARVIS_DIR / "screenshots"
MONITOR_DB      = JARVIS_DIR / "monitor.db"


def _require_playwright() -> str | None:
    if not PLAYWRIGHT_OK:
        return "Playwright not installed. Run: uv run --project openjarvis pip install playwright && python -m playwright install chromium"
    return None


def _launch_browser(p):
    return p.chromium.launch(headless=True, args=BROWSER_ARGS)


def _fill_field(page, identifier: str, value: str) -> bool:
    """Try multiple locator strategies to fill one form field."""
    strategies = [
        f'[name="{identifier}"]',
        f'[id="{identifier}"]',
        f'[placeholder*="{identifier}" i]',
        f'[aria-label*="{identifier}" i]',
        f'label:has-text("{identifier}") + input',
        f'label:has-text("{identifier}") ~ input',
        f'label:has-text("{identifier}") + textarea',
        identifier,  # treat as direct CSS/XPath selector last
    ]
    for sel in strategies:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.fill(value, timeout=4000)
                return True
        except Exception:
            pass
    return False


# ── Browser tool implementations ───────────────────────────────────────────────

def tool_web_scrape(url: str, goal: str = "") -> str:
    """Fetch a page and return its text content, title, and key links."""
    if err := _require_playwright():
        return err
    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; Jarvis/1.0)")
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        title = page.title()
        # Remove nav/footer noise by targeting main content areas when present
        for sel in ["main", "article", "[role=main]", "body"]:
            if page.locator(sel).count() > 0:
                text = page.locator(sel).first.inner_text()
                break
        # Key links (external hrefs)
        links = page.evaluate("""() =>
            [...document.querySelectorAll('a[href]')]
            .map(a => a.href + '  ' + a.innerText.trim())
            .filter(s => s.startsWith('http') && s.length < 200)
            .slice(0, 20)
        """)
        browser.close()
    out = f"Title: {title}\nURL: {url}\n"
    if goal:
        out += f"Goal: {goal}\n"
    out += f"\n{text[:6000]}"
    if links:
        out += "\n\nKey links:\n" + "\n".join(links[:10])
    return out


def tool_screenshot_page(url: str) -> str:
    """Screenshot a URL, save to ~/.openjarvis/screenshots/, return content summary."""
    if err := _require_playwright():
        return err
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = SCREENSHOTS_DIR / f"{ts}.png"
    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url, timeout=30_000, wait_until="networkidle")
        title   = page.title()
        content = page.inner_text("body")[:3000]
        page.screenshot(path=str(out_path))
        browser.close()
    return (
        f"Screenshot saved: {out_path}\n"
        f"Page title: {title}\n"
        f"URL: {url}\n\n"
        f"Page content (first 3000 chars):\n{content}"
    )


def tool_web_form_fill(
    url: str,
    fields: dict,
    submit: bool = True,
    submit_selector: str = "",
) -> str:
    """Navigate to a URL, fill form fields, and optionally submit."""
    if err := _require_playwright():
        return err
    if not fields:
        return "Error: fields dict is empty."
    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; Jarvis/1.0)")
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")

        filled, failed = [], []
        for identifier, value in fields.items():
            if _fill_field(page, identifier, str(value)):
                filled.append(identifier)
            else:
                failed.append(identifier)

        result_lines = [
            f"URL: {url}",
            f"Filled  : {', '.join(filled) if filled else 'none'}",
            f"Failed  : {', '.join(failed) if failed else 'none'}",
        ]

        if submit:
            candidates = (
                [submit_selector] if submit_selector
                else [
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("Submit")',
                    'button:has-text("Send")',
                    'button:has-text("Continue")',
                    'button:has-text("Next")',
                ]
            )
            submitted = False
            for sel in candidates:
                try:
                    page.click(sel, timeout=4000)
                    page.wait_for_load_state("networkidle", timeout=15_000)
                    result_lines.append(f"Submitted: yes (clicked {sel})")
                    result_lines.append(f"Final URL: {page.url}")
                    submitted = True
                    break
                except Exception:
                    pass
            if not submitted:
                result_lines.append("Submitted: no matching submit button found")

        browser.close()
    return "\n".join(result_lines)


def tool_monitor_website(url: str, reset: bool = False) -> str:
    """
    Check if a page has changed since the last call.
    First call sets a baseline. Subsequent calls diff against it.
    Pass reset=true to clear the baseline and start fresh.
    """
    if err := _require_playwright():
        return err
    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; Jarvis/1.0)")
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        title   = page.title()
        content = page.inner_text("body")
        browser.close()

    content_hash = hashlib.md5(content.encode()).hexdigest()
    snippet      = content[:600]
    now          = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(str(MONITOR_DB)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS monitors
            (url TEXT PRIMARY KEY, hash TEXT, snippet TEXT, checked_at TEXT, title TEXT)""")
        row = db.execute(
            "SELECT hash, snippet, checked_at, title FROM monitors WHERE url=?", (url,)
        ).fetchone()

        if reset or row is None:
            db.execute(
                "INSERT OR REPLACE INTO monitors VALUES (?,?,?,?,?)",
                (url, content_hash, snippet, now, title),
            )
            verb = "Reset" if reset else "Baseline set"
            return f"{verb}: {title}\nURL: {url}\nHash: {content_hash[:8]}…\nAt: {now}"

        old_hash, old_snippet, checked_at, old_title = row
        # Update timestamp + current hash
        db.execute(
            "UPDATE monitors SET hash=?, snippet=?, checked_at=?, title=? WHERE url=?",
            (content_hash, snippet, now, title, url),
        )

    if content_hash == old_hash:
        return f"No change — {title}\nURL: {url}\nLast baseline: {checked_at}"

    # Compute a simple word-level diff summary
    old_words = set(old_snippet.split())
    new_words = set(snippet.split())
    added   = new_words - old_words
    removed = old_words - new_words
    diff_summary = ""
    if added:
        diff_summary += f"New words: {', '.join(list(added)[:15])}\n"
    if removed:
        diff_summary += f"Removed : {', '.join(list(removed)[:15])}"

    return (
        f"CHANGE DETECTED — {title}\n"
        f"URL: {url}\n"
        f"Previous check: {checked_at}\n\n"
        f"{diff_summary}\n\n"
        f"Current content preview:\n{snippet}"
    )


# ── Trading tools ─────────────────────────────────────────────────────────────

def _coingecko_id(symbol: str) -> str:
    """Map ticker symbol to CoinGecko coin id. Falls back to lowercase symbol."""
    return COIN_IDS.get(symbol.upper(), symbol.lower())


def tool_crypto_price(symbols: str) -> str:
    """
    Real-time price, 24h change, volume, and market cap for one or more assets.
    symbols: comma-separated tickers, e.g. "BTC,ETH,SOL" or stock tickers like "AAPL,TSLA".
    Crypto via CoinGecko (no key needed); stocks/ETFs via Yahoo Finance.
    """
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not tickers:
        return "Error: provide at least one symbol."

    lines = []

    # Separate crypto from likely stock tickers
    crypto_tickers = [t for t in tickers if t in COIN_IDS or len(t) <= 5]
    stock_tickers  = [t for t in tickers if t not in COIN_IDS and t not in crypto_tickers]

    # ── Crypto via CoinGecko ─────────────────────────────────────────────────
    crypto_ids = [_coingecko_id(t) for t in tickers if t in COIN_IDS]
    if crypto_ids:
        ids_param = urllib.parse.quote(",".join(crypto_ids))
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids_param}&vs_currencies=usd"
            f"&include_24hr_change=true&include_market_cap=true&include_24hr_vol=true"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                    "User-Agent": "Jarvis/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            for ticker in tickers:
                if ticker not in COIN_IDS:
                    continue
                cg_id = _coingecko_id(ticker)
                if cg_id not in data:
                    lines.append(f"{ticker}: not found on CoinGecko")
                    continue
                d = data[cg_id]
                price  = d.get("usd", 0)
                chg24  = d.get("usd_24h_change", 0)
                mcap   = d.get("usd_market_cap", 0)
                vol24  = d.get("usd_24h_vol", 0)
                arrow  = "▲" if chg24 >= 0 else "▼"
                lines.append(
                    f"{ticker:6s}  ${price:,.4f}  {arrow} {abs(chg24):.2f}%  "
                    f"MCap ${mcap/1e9:.2f}B  Vol ${vol24/1e6:.1f}M"
                )
        except Exception as e:
            lines.append(f"CoinGecko error: {e}")

    # ── Stocks / ETFs / indices via Yahoo Finance ────────────────────────────
    for ticker in tickers:
        if ticker in COIN_IDS:
            continue  # already handled above
        yf_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d"
        req = urllib.request.Request(yf_url, headers={"User-Agent": "Mozilla/5.0",
                                                       "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            meta  = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev  = meta.get("chartPreviousClose", meta.get("previousClose", price))
            chg   = price - prev
            chgp  = (chg / prev * 100) if prev else 0
            arrow = "▲" if chg >= 0 else "▼"
            name  = meta.get("longName") or meta.get("shortName") or ticker
            lines.append(f"{ticker:6s}  ${price:,.2f}  {arrow} {abs(chgp):.2f}%  ({name})")
        except Exception as e:
            lines.append(f"{ticker}: error — {e}")

    if not lines:
        return "No prices returned. Check symbols."
    ts = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    return f"Prices as of {ts}:\n\n" + "\n".join(lines)


def tool_market_news(query: str = "bitcoin crypto", max_items: int = 10) -> str:
    """
    Latest financial/crypto news filtered to a query.
    Uses Google News RSS — no API key needed.
    Example: query="BTC bitcoin halving" or query="Fed rate cut" or query="TSLA earnings"
    """
    encoded = urllib.parse.quote(query)
    url = (
        f"https://news.google.com/rss/search"
        f"?q={encoded}+when:2d&hl=en-US&gl=US&ceid=US:en"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            content = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"News fetch failed: {e}"

    # Parse RSS items (no external XML library needed)
    import re
    titles  = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", content)
    links   = re.findall(r"<link>(https?://[^<]+)</link>", content)
    pubdats = re.findall(r"<pubDate>(.*?)</pubDate>", content)
    sources = re.findall(r"<source[^>]*>(.*?)</source>", content)

    # First <title> is the feed title, skip it
    titles  = titles[1:max_items + 1]
    links   = links[1:max_items + 1]
    pubdats = pubdats[1:max_items + 1]
    sources = sources[:max_items]

    if not titles:
        return f"No recent news for: {query}"

    items = []
    for i, title in enumerate(titles):
        pub  = pubdats[i][:25] if i < len(pubdats) else ""
        src  = sources[i]      if i < len(sources) else ""
        link = links[i]        if i < len(links)   else ""
        item = f"• {title}"
        if src:
            item += f" [{src}]"
        if pub:
            item += f"  {pub}"
        if link:
            item += f"\n  {link}"
        items.append(item)

    return f"News for '{query}' (last 48h):\n\n" + "\n\n".join(items)


def tool_options_data(symbol: str, expiry: str = "") -> str:
    """
    Options chain for any stock/ETF ticker — strikes, IV, bid/ask, open interest.
    expiry: optional YYYY-MM-DD to filter to a specific expiration.
    Data via Yahoo Finance (no API key needed).
    """
    url = f"https://query1.finance.yahoo.com/v7/finance/options/{symbol.upper()}"
    if expiry:
        # Convert to Unix timestamp for YF
        try:
            ts = int(datetime.strptime(expiry, "%Y-%m-%d").timestamp())
            url += f"?date={ts}"
        except ValueError:
            return "Error: expiry must be YYYY-MM-DD format."

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
    except Exception as e:
        return f"Options fetch failed for {symbol}: {e}"

    result = data.get("optionChain", {}).get("result", [])
    if not result:
        return f"No options data for {symbol}. Check the ticker."

    block   = result[0]
    meta    = block.get("quote", {})
    expiries = block.get("expirationDates", [])
    opts    = block.get("options", [{}])[0]
    calls   = opts.get("calls", [])
    puts    = opts.get("puts", [])

    price  = meta.get("regularMarketPrice", "?")
    name   = meta.get("longName") or meta.get("shortName") or symbol

    exp_dates = [datetime.fromtimestamp(e).strftime("%Y-%m-%d") for e in expiries[:8]]

    def fmt_chain(contracts: list, label: str) -> str:
        if not contracts:
            return f"  {label}: none\n"
        rows = [f"  {'Strike':>8}  {'Bid':>7}  {'Ask':>7}  {'IV':>7}  {'OI':>8}  {'Last':>7}"]
        for c in sorted(contracts, key=lambda x: x.get("strike", 0))[:15]:
            strike = c.get("strike", 0)
            bid    = c.get("bid", 0)
            ask    = c.get("ask", 0)
            iv     = c.get("impliedVolatility", 0)
            oi     = c.get("openInterest", 0)
            last   = c.get("lastPrice", 0)
            atm    = " ◀" if abs(strike - float(price)) / float(price) < 0.03 else ""
            rows.append(
                f"  {strike:>8.1f}  {bid:>7.2f}  {ask:>7.2f}  "
                f"{iv*100:>6.1f}%  {oi:>8,}  {last:>7.2f}{atm}"
            )
        return "\n".join(rows)

    exp_showing = datetime.fromtimestamp(expiries[0]).strftime("%Y-%m-%d") if expiries else "?"

    out = [
        f"Options: {name} ({symbol.upper()})  Spot: ${price}",
        f"Showing expiry: {exp_showing}",
        f"All expiries: {', '.join(exp_dates)}\n",
        "CALLS:",
        fmt_chain(calls, "calls"),
        "\nPUTS:",
        fmt_chain(puts, "puts"),
        f"\n(◀ = near-the-money; IV shown as %; OI = open interest)",
    ]
    return "\n".join(out)


# ── Trading desk bridge tools ──────────────────────────────────────────────────

def _desk_get(path: str) -> dict:
    """GET from TRADING_DESK_URL. Raises RuntimeError with setup instructions if not configured."""
    if not TRADING_DESK_URL:
        raise RuntimeError(TRADING_DESK_SETUP_MSG)
    url = f"{TRADING_DESK_URL}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                "User-Agent": "Jarvis/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def tool_trading_desk_status() -> str:
    """
    Overall status of the trading desk system: stage, active strategies,
    paper/live mode, last trade, uptime, any halt conditions.
    Requires TRADING_DESK_URL (see deploy/trading_bridge.py).
    """
    try:
        d = _desk_get("/status")
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Trading desk unreachable: {e}\n\nMake sure trading_bridge.py is running and TRADING_DESK_URL is set."

    mode   = "🟢 LIVE" if not d.get("paper_trading", True) else "🟡 PAPER"
    halt   = "🔴 HALTED" if d.get("halt_active") else "✅ Running"
    lines  = [
        f"Trading Desk Status  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
        f"Stage         : {d.get('stage','?')}",
        f"Mode          : {mode}",
        f"System        : {halt}",
        f"Strategies    : {d.get('active_strategies','?')} active / {d.get('strategies_loaded','?')} loaded",
        f"Last trade    : {d.get('last_trade','?')}",
        f"Uptime        : {d.get('uptime_hours','?'):.1f}h" if isinstance(d.get('uptime_hours'), (int,float)) else f"Uptime        : {d.get('uptime_hours','?')}",
        f"Last updated  : {d.get('last_updated','?')}",
    ]
    if d.get("halt_reason"):
        lines.append(f"\nHalt reason   : {d['halt_reason']}")
    if d.get("notes"):
        lines.append(f"\nNotes: {d['notes']}")
    return "\n".join(lines)


def tool_portfolio_snapshot() -> str:
    """
    Current portfolio state from the trading desk: positions, unrealized P&L,
    cash, total equity, and per-position risk exposure.
    Requires TRADING_DESK_URL.
    """
    try:
        d = _desk_get("/portfolio")
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Portfolio fetch failed: {e}"

    positions = d.get("positions", [])
    total_pnl = d.get("total_pnl", 0)
    pnl_pct   = d.get("total_pnl_pct", 0)
    equity    = d.get("account_value", 0)
    cash      = d.get("cash", 0)
    ts        = d.get("last_updated", "?")
    pnl_arrow = "▲" if total_pnl >= 0 else "▼"

    lines = [
        f"Portfolio Snapshot  {ts}",
        f"",
        f"Account equity : ${equity:,.2f}",
        f"Cash           : ${cash:,.2f}",
        f"Total P&L      : {pnl_arrow} ${abs(total_pnl):,.2f}  ({pnl_arrow} {abs(pnl_pct):.2f}%)",
        f"Positions      : {len(positions)}",
        f"",
    ]

    if positions:
        lines.append(f"  {'Symbol':<8}  {'Size':>10}  {'P&L':>10}  {'P&L %':>7}  {'Entry':>10}  {'Current':>10}")
        lines.append(f"  {'-'*8}  {'-'*10}  {'-'*10}  {'-'*7}  {'-'*10}  {'-'*10}")
        for p in positions:
            sym   = p.get("symbol", "?")
            size  = p.get("size", 0)
            pnl   = p.get("pnl", 0)
            pp    = p.get("pnl_pct", 0)
            entry = p.get("entry_price", 0)
            cur   = p.get("current_price", 0)
            arrow = "▲" if pnl >= 0 else "▼"
            lines.append(
                f"  {sym:<8}  {size:>10.4f}  {arrow} ${abs(pnl):>8.2f}  "
                f"{arrow} {abs(pp):>5.2f}%  ${entry:>9.2f}  ${cur:>9.2f}"
            )
    else:
        lines.append("  No open positions.")

    return "\n".join(lines)


def tool_risk_dashboard() -> str:
    """
    Real-time risk state: daily drawdown %, 5% halt trigger status,
    max exposure, position sizing, and risk limit violations.
    Answers 'are we safe?' instantly.
    Requires TRADING_DESK_URL.
    """
    try:
        d = _desk_get("/risk")
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Risk data fetch failed: {e}"

    dd_pct    = d.get("daily_drawdown_pct", 0)
    halt_thr  = d.get("daily_drawdown_halt_threshold", 5.0)
    halt_act  = d.get("halt_active", False)
    peak_eq   = d.get("peak_equity", 0)
    cur_eq    = d.get("current_equity", 0)
    max_pos   = d.get("max_position_size", 0)
    exposure  = d.get("total_exposure", 0)
    ts        = d.get("last_updated", "?")

    # Safety assessment
    pct_to_halt = halt_thr - dd_pct
    if halt_act:
        safety = "🔴 HALTED — trading stopped"
    elif pct_to_halt < 1.0:
        safety = f"🟠 WARNING — {pct_to_halt:.2f}% from halt"
    elif dd_pct > 2.5:
        safety = f"🟡 ELEVATED — {dd_pct:.2f}% drawdown today"
    else:
        safety = f"🟢 SAFE — {dd_pct:.2f}% drawdown today"

    dd_bar_len = int((dd_pct / halt_thr) * 20)
    dd_bar = "█" * dd_bar_len + "░" * (20 - dd_bar_len)

    lines = [
        f"Risk Dashboard  {ts}",
        f"",
        f"Status         : {safety}",
        f"",
        f"Daily Drawdown : {dd_pct:.2f}% / {halt_thr:.1f}% limit",
        f"               [{dd_bar}] {dd_pct/halt_thr*100:.0f}%",
        f"Peak equity    : ${peak_eq:,.2f}",
        f"Current equity : ${cur_eq:,.2f}",
        f"",
        f"Max position   : {max_pos:.1%}" if max_pos else f"Max position   : ?",
        f"Total exposure : {exposure:.1%}" if exposure else f"Total exposure : ?",
    ]

    violations = d.get("violations", [])
    if violations:
        lines.append(f"\nActive violations:")
        for v in violations:
            lines.append(f"  ⚠️  {v}")

    return "\n".join(lines)


def tool_strategy_perf(limit: int = 10, min_sharpe: float = 0.0) -> str:
    """
    Stage14B strategy performance — Sharpe, win rate, expectancy, max drawdown.
    Shows top performers by Sharpe, filtered by min_sharpe.
    Tracks path to Dec 1 readiness gates.
    Requires TRADING_DESK_URL.
    """
    try:
        path = f"/strategies?limit={limit}&min_sharpe={min_sharpe}"
        d    = _desk_get(path)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Strategy data fetch failed: {e}"

    strategies = d.get("strategies", [])
    total      = d.get("total_strategies", "?")
    ts         = d.get("last_updated", "?")
    seed       = d.get("seed", 20261026)
    stage      = d.get("stage", "Stage14B")

    lines = [
        f"{stage} Performance  seed={seed}  {ts}",
        f"Total strategies: {total}  |  Showing: top {len(strategies)} (Sharpe ≥ {min_sharpe})",
        f"",
        f"  {'#':>3}  {'ID':<10}  {'Sharpe':>7}  {'Win%':>6}  {'Expect':>7}  {'MaxDD':>7}  {'Trades':>7}",
        f"  {'─'*3}  {'─'*10}  {'─'*7}  {'─'*6}  {'─'*7}  {'─'*7}  {'─'*7}",
    ]

    for i, s in enumerate(strategies, 1):
        sid     = str(s.get("id", "?"))[:10]
        sharpe  = s.get("sharpe", 0)
        win     = s.get("win_rate", 0)
        expect  = s.get("expectancy", 0)
        mdd     = s.get("max_drawdown", 0)
        trades  = s.get("trades", 0)
        star    = " ⭐" if sharpe >= 2.0 else ""
        lines.append(
            f"  {i:>3}  {sid:<10}  {sharpe:>7.2f}  "
            f"{win*100:>5.1f}%  {expect:>7.3f}  "
            f"{mdd*100:>6.1f}%  {trades:>7}{star}"
        )

    # Readiness gates summary
    gates = d.get("readiness_gates", {})
    if gates:
        lines.append(f"\nDec 1 Readiness Gates:")
        for gate, status in gates.items():
            icon = "✅" if status else "❌"
            lines.append(f"  {icon} {gate}")

    return "\n".join(lines)


# ── Monitor DB schema helper ───────────────────────────────────────────────────

def _ensure_monitor_schema(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS monitors (
        url          TEXT PRIMARY KEY,
        hash         TEXT,
        snippet      TEXT,
        checked_at   TEXT,
        title        TEXT,
        watch_active INTEGER DEFAULT 0,
        label        TEXT    DEFAULT ''
    )""")
    for col, defn in [("watch_active", "INTEGER DEFAULT 0"), ("label", "TEXT DEFAULT ''")]:
        try:
            db.execute(f"ALTER TABLE monitors ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass  # already exists


# ── Background monitor ─────────────────────────────────────────────────────────

def _send_alert_sms(body: str):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        print("[monitor] SMS skipped — set TWILIO_AUTH_TOKEN + JARVIS_NOTIFY_NUMBER")
        return
    data = urllib.parse.urlencode({"To": TWILIO_TO, "From": TWILIO_FROM, "Body": body}).encode()
    auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    req  = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
        data=data,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            sid = json.loads(r.read()).get("sid", "?")
        print(f"[monitor] Alert SMS sent ({sid})")
    except Exception as e:
        print(f"[monitor] SMS failed: {e}")


def _run_monitor_cycle():
    if not MONITOR_DB.exists():
        return
    with sqlite3.connect(str(MONITOR_DB)) as db:
        _ensure_monitor_schema(db)
        rows = db.execute(
            "SELECT url, label FROM monitors WHERE watch_active=1"
        ).fetchall()
    if not rows:
        return
    print(f"[monitor] Checking {len(rows)} watched URL(s)…")
    for url, label in rows:
        try:
            result = tool_monitor_website(url)
            if result.startswith("CHANGE DETECTED"):
                name = label or url
                # Keep SMS concise — first 320 chars of result
                sms = f"Jarvis: Change on {name}\n{url}\n\n{result[result.find(chr(10))+1:][:280]}"
                _send_alert_sms(sms)
                print(f"[monitor] Change → alert sent for {url}")
            else:
                print(f"[monitor] No change: {url}")
        except Exception as e:
            print(f"[monitor] Error on {url}: {e}")


def _monitor_loop():
    print(f"[monitor] Background watcher started — interval {MONITOR_INTERVAL}s")
    time.sleep(20)  # let the server finish starting
    while True:
        try:
            _run_monitor_cycle()
        except Exception as e:
            print(f"[monitor] Cycle error: {e}")
        time.sleep(MONITOR_INTERVAL)


# ── Watch management tools ─────────────────────────────────────────────────────

def tool_watch_url(url: str, label: str = "") -> str:
    """Add a URL to background monitoring — Jarvis texts Andre when it changes."""
    if err := _require_playwright():
        return err
    # Scrape once to set baseline
    baseline = tool_monitor_website(url, reset=True)
    # Mark as active watch
    with sqlite3.connect(str(MONITOR_DB)) as db:
        _ensure_monitor_schema(db)
        db.execute(
            "UPDATE monitors SET watch_active=1, label=? WHERE url=?",
            (label or url, url),
        )
    name = label or url
    return (
        f"Now watching: {name}\n"
        f"Baseline captured. Jarvis will check every {MONITOR_INTERVAL // 60} minute(s) "
        f"and text {TWILIO_TO} if anything changes.\n\n"
        f"Baseline: {baseline}"
    )


def tool_unwatch_url(url: str) -> str:
    """Remove a URL from background monitoring."""
    if not MONITOR_DB.exists():
        return f"Not watching any URLs yet."
    with sqlite3.connect(str(MONITOR_DB)) as db:
        _ensure_monitor_schema(db)
        row = db.execute("SELECT label FROM monitors WHERE url=?", (url,)).fetchone()
        if not row:
            return f"URL not found in watch list: {url}"
        db.execute("UPDATE monitors SET watch_active=0 WHERE url=?", (url,))
    name = row[0] or url
    return f"Stopped watching: {name}"


def tool_list_watches() -> str:
    """List all URLs currently being watched by the background monitor."""
    if not MONITOR_DB.exists():
        return "No URLs are being watched. Use watch_url to add one."
    with sqlite3.connect(str(MONITOR_DB)) as db:
        _ensure_monitor_schema(db)
        active = db.execute(
            "SELECT url, label, checked_at FROM monitors WHERE watch_active=1 ORDER BY checked_at DESC"
        ).fetchall()
        inactive = db.execute(
            "SELECT url, label FROM monitors WHERE watch_active=0"
        ).fetchall()
    if not active and not inactive:
        return "No URLs in monitor database. Use watch_url to add one."
    lines = []
    if active:
        lines.append(f"Actively watching ({len(active)}):")
        for url, label, checked_at in active:
            lines.append(f"  ✓ {label or url}\n    {url}\n    Last checked: {checked_at or 'pending'}")
    if inactive:
        lines.append(f"\nPaused / manual-only ({len(inactive)}):")
        for url, label in inactive:
            lines.append(f"  — {label or url}")
    lines.append(f"\nInterval: every {MONITOR_INTERVAL // 60} minute(s)")
    lines.append(f"Alerts → {TWILIO_TO}")
    return "\n".join(lines)


def tool_pdf_extractor(url: str, max_pages: int = 15) -> str:
    """Download a PDF from a URL and extract its text content."""
    if not PYPDF_OK:
        return "pypdf not installed. Run: uv run --project openjarvis pip install pypdf"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)"}
    )
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp = f.name
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                f.write(resp.read())
        except Exception as e:
            os.unlink(tmp)
            return f"Download failed: {e}"

    try:
        reader    = pypdf.PdfReader(tmp)
        total     = len(reader.pages)
        to_read   = min(total, max_pages)
        parts     = []
        for i in range(to_read):
            text = reader.pages[i].extract_text() or ""
            if text.strip():
                parts.append(f"[Page {i+1}]\n{text.strip()}")
        full_text = "\n\n".join(parts)
        return (
            f"PDF: {url}\n"
            f"Pages: {total} total, showing {to_read}\n\n"
            f"{full_text[:8000]}"
        )
    except Exception as e:
        return f"PDF parse error: {e}"
    finally:
        os.unlink(tmp)


# ── MCP manifest ───────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read any file from ~/.openjarvis/. "
            "Use path='andre-dossier.md' to read the full personal dossier. "
            "Use path='notes/' prefix for saved notes. "
            "If file not found, returns a list of available files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within ~/.openjarvis/"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_note",
        "description": (
            "Write a persistent note to ~/.openjarvis/notes/. "
            "Use to record decisions, commitments, research findings, "
            "or facts Andre tells you that you want to remember."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title":   {"type": "string", "description": "Short descriptive title for the note"},
                "content": {"type": "string", "description": "Note body in markdown"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "get_calendar",
        "description": "Get Andre's upcoming Google Calendar events. Requires GOOGLE_* env vars.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days ahead to look (default 7)",
                    "default": 7,
                }
            },
            "required": [],
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Create a Google Calendar event for Andre. "
            "Requires explicit authorization from Andre before use. "
            "start/end must be ISO 8601 e.g. 2026-11-19T10:00:00-07:00 (Mountain Time is UTC-7)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary":     {"type": "string", "description": "Event title"},
                "start":       {"type": "string", "description": "ISO 8601 start datetime"},
                "end":         {"type": "string", "description": "ISO 8601 end datetime"},
                "description": {"type": "string", "default": "", "description": "Optional event notes"},
                "location":    {"type": "string", "default": "", "description": "Optional location"},
            },
            "required": ["summary", "start", "end"],
        },
    },
    # ── Trading tools ────────────────────────────────────────────────────────
    {
        "name": "crypto_price",
        "description": (
            "Real-time price, 24h change, volume, and market cap for any crypto or stock. "
            "Pass a comma-separated list: 'BTC,ETH,SOL' or 'AAPL,TSLA,SPY'. "
            "Crypto via CoinGecko, stocks via Yahoo Finance. No API key needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "string",
                    "description": "Comma-separated tickers, e.g. 'BTC,ETH' or 'AAPL,TSLA'",
                },
            },
            "required": ["symbols"],
        },
    },
    {
        "name": "market_news",
        "description": (
            "Latest financial news for any query — crypto, macro, earnings, regulations. "
            "Examples: 'BTC bitcoin ETF', 'Fed rate cut inflation', 'AAPL earnings', "
            "'SEC crypto enforcement'. Returns up to 10 articles from the last 48 hours."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":     {"type": "string", "description": "Search query, e.g. 'bitcoin halving' or 'Fed FOMC meeting'"},
                "max_items": {"type": "integer", "default": 10, "description": "Max articles (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "options_data",
        "description": (
            "Real-time options chain for any stock/ETF — strikes, IV, Greeks, open interest. "
            "Shows near-the-money contracts flagged with ◀. "
            "Use for hedging decisions, IV crush plays, and institutional flow analysis. "
            "Data via Yahoo Finance, no API key needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock/ETF ticker, e.g. 'SPY', 'AAPL', 'BTC-USD'"},
                "expiry": {"type": "string", "default": "", "description": "Filter to expiry date YYYY-MM-DD (optional)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "trading_desk_status",
        "description": (
            "Overall status of the Windows trading desk — stage (Stage14B), "
            "paper vs live mode, halt status, active strategy count, uptime, last trade. "
            "Requires TRADING_DESK_URL in Railway Variables (see deploy/trading_bridge.py)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "portfolio_snapshot",
        "description": (
            "Current portfolio state from the trading desk: open positions, entry prices, "
            "unrealized P&L per position and in total, cash, and account equity. "
            "Requires TRADING_DESK_URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "risk_dashboard",
        "description": (
            "Real-time risk state: daily drawdown % with progress bar toward the 5% halt threshold, "
            "peak equity, halt trigger status, position sizing limits, and any active violations. "
            "Answers 'are we safe?' in one call. Requires TRADING_DESK_URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "strategy_perf",
        "description": (
            "Stage14B strategy performance — Sharpe ratio, win rate, expectancy, max drawdown, trade count. "
            "Shows top performers. Stars strategies with Sharpe ≥ 2.0. "
            "Also shows Dec 1 readiness gate status when available. "
            "Requires TRADING_DESK_URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit":      {"type": "integer", "default": 10,  "description": "Max strategies to show"},
                "min_sharpe": {"type": "number",  "default": 0.0, "description": "Filter: only show strategies above this Sharpe"},
            },
            "required": [],
        },
    },
    # ── Browser tools ────────────────────────────────────────────────────────
    {
        "name": "web_scrape",
        "description": (
            "Navigate to any URL and extract its full text content, title, and key links. "
            "Use for real-time research, competitor pages, news articles, pricing, job postings, "
            "real estate listings, and any page that needs reading. "
            "Optionally pass goal to focus the extraction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url":  {"type": "string", "description": "Full URL to scrape"},
                "goal": {"type": "string", "description": "What you're looking for (optional)", "default": ""},
            },
            "required": ["url"],
        },
    },
    {
        "name": "screenshot_page",
        "description": (
            "Take a full screenshot of any URL and save it to ~/.openjarvis/screenshots/. "
            "Returns the page title and text content. Useful for verifying form submissions, "
            "auditing competitor UIs, and capturing state of web apps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to screenshot"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "web_form_fill",
        "description": (
            "Navigate to a URL, fill form fields, and submit. "
            "Fields dict maps field name/label/placeholder to its value. "
            "REQUIRES EXPLICIT AUTHORIZATION FROM ANDRE before submitting anything. "
            "Confirm the fields and URL with Andre before calling with submit=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url":             {"type": "string", "description": "Form page URL"},
                "fields":          {
                    "type": "object",
                    "description": "Map of field identifier → value. Identifier can be name, id, placeholder text, aria-label, or label text.",
                    "additionalProperties": {"type": "string"},
                },
                "submit":          {"type": "boolean", "default": True, "description": "Whether to click the submit button"},
                "submit_selector": {"type": "string",  "default": "",   "description": "CSS selector for a specific submit button (optional)"},
            },
            "required": ["url", "fields"],
        },
    },
    {
        "name": "monitor_website",
        "description": (
            "Check if a webpage has changed since the last time it was checked. "
            "First call sets a baseline. Subsequent calls detect additions, removals, and rewrites. "
            "Great for tracking competitor pricing, SEC EDGAR filings, job postings, and news. "
            "Pass reset=true to wipe the baseline and start fresh."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url":   {"type": "string",  "description": "URL to check"},
                "reset": {"type": "boolean", "default": False, "description": "Reset the stored baseline"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "pdf_extractor",
        "description": (
            "Download a PDF from any URL and extract its full text. "
            "Use for SEC filings, investor presentations, term sheets, research reports, "
            "government documents, and any PDF that needs reading."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url":       {"type": "string",  "description": "Direct URL to the PDF"},
                "max_pages": {"type": "integer", "default": 15, "description": "Max pages to extract (default 15)"},
            },
            "required": ["url"],
        },
    },
    # ── Proactive background monitor tools ───────────────────────────────────
    {
        "name": "watch_url",
        "description": (
            "Add a URL to Jarvis's background monitor. "
            "Jarvis will check it every 10 minutes and TEXT ANDRE via SMS if it changes. "
            "Perfect for internship/job postings at Apple, SpaceX, etc.; competitor pricing; "
            "news articles; real estate listings; SEC EDGAR filings. "
            "Pass a human-readable label so the SMS alert is clear (e.g. 'SpaceX Internships')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url":   {"type": "string", "description": "Full URL to watch"},
                "label": {"type": "string", "default": "", "description": "Short human label for SMS alerts (e.g. 'Apple Jobs')"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "unwatch_url",
        "description": "Stop watching a URL — remove it from background monitoring.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to stop watching"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "list_watches",
        "description": (
            "List all URLs Jarvis is currently monitoring in the background, "
            "plus paused/manual ones and the check interval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

TOOL_FNS = {
    "read_file":              lambda a: tool_read_file(**a),
    "write_note":             lambda a: tool_write_note(**a),
    "get_calendar":           lambda a: tool_get_calendar(**a),
    "create_calendar_event":  lambda a: tool_create_calendar_event(**a),
    "web_scrape":             lambda a: tool_web_scrape(**a),
    "screenshot_page":        lambda a: tool_screenshot_page(**a),
    "web_form_fill":          lambda a: tool_web_form_fill(**a),
    "monitor_website":        lambda a: tool_monitor_website(**a),
    "pdf_extractor":          lambda a: tool_pdf_extractor(**a),
    "watch_url":              lambda a: tool_watch_url(**a),
    "unwatch_url":            lambda a: tool_unwatch_url(**a),
    "list_watches":           lambda a: tool_list_watches(),
    # Trading
    "crypto_price":           lambda a: tool_crypto_price(**a),
    "market_news":            lambda a: tool_market_news(**a),
    "options_data":           lambda a: tool_options_data(**a),
    "trading_desk_status":    lambda a: tool_trading_desk_status(),
    "portfolio_snapshot":     lambda a: tool_portfolio_snapshot(),
    "risk_dashboard":         lambda a: tool_risk_dashboard(),
    "strategy_perf":          lambda a: tool_strategy_perf(**a),
}

SESSION_ID = "jarvis-tools-1"

# ── MCP HTTP server ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()
    yield


app = FastAPI(lifespan=lifespan)


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
                    "serverInfo": {"name": "jarvis-custom-tools", "version": "1.0.0"},
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
            # Run in thread so sync playwright doesn't block the event loop
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
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Jarvis tools server starting on port 8003")
    print(f"  Notes dir : {NOTES_DIR}")
    print(f"  Google Cal: {'configured' if GC_REFRESH_TOKEN else 'NOT configured (set GOOGLE_* vars)'}")
    print(f"  Monitor   : every {MONITOR_INTERVAL}s → SMS to {TWILIO_TO}")
    print(f"  Twilio    : {'configured' if TWILIO_TOKEN else 'NOT configured (set TWILIO_AUTH_TOKEN)'}")
    uvicorn.run(app, host="127.0.0.1", port=8003, log_level="info")
