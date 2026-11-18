"""
Jarvis Custom Tools MCP Server — port 8003 (internal only)
Tools: read_file, write_note, get_calendar, create_calendar_event

Google Calendar requires three Railway env vars:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  Optionally: GOOGLE_CALENDAR_ID (defaults to "primary")

File tools work immediately with no credentials.
"""
import json
import os
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
]

TOOL_FNS = {
    "read_file":             lambda a: tool_read_file(**a),
    "write_note":            lambda a: tool_write_note(**a),
    "get_calendar":          lambda a: tool_get_calendar(**a),
    "create_calendar_event": lambda a: tool_create_calendar_event(**a),
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
            result = fn(args)
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
