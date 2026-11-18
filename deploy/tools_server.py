"""
Jarvis Custom Tools MCP Server — port 8003 (internal only)
Tools:
  Core   : read_file, write_note
  Calendar: get_calendar, create_calendar_event
  Browser : web_scrape, screenshot_page, web_form_fill,
            monitor_website, pdf_extractor

Google Calendar requires: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
  GOOGLE_REFRESH_TOKEN (optionally GOOGLE_CALENDAR_ID, default "primary")
Browser tools require playwright + pypdf (installed in build.sh).
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import traceback
import urllib.parse
import urllib.request
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
]

TOOL_FNS = {
    "read_file":             lambda a: tool_read_file(**a),
    "write_note":            lambda a: tool_write_note(**a),
    "get_calendar":          lambda a: tool_get_calendar(**a),
    "create_calendar_event": lambda a: tool_create_calendar_event(**a),
    "web_scrape":            lambda a: tool_web_scrape(**a),
    "screenshot_page":       lambda a: tool_screenshot_page(**a),
    "web_form_fill":         lambda a: tool_web_form_fill(**a),
    "monitor_website":       lambda a: tool_monitor_website(**a),
    "pdf_extractor":         lambda a: tool_pdf_extractor(**a),
}

SESSION_ID = "jarvis-tools-1"

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
    uvicorn.run(app, host="127.0.0.1", port=8003, log_level="info")
