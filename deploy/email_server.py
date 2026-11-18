"""
Jarvis Email Server — Gmail integration on port 8005.
Uses OAuth2 refresh token flow (no extra libs needed).

Required Railway Variables:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  (same credentials as Calendar — scope must include gmail.modify)
"""
from __future__ import annotations
import asyncio, base64, json, os, traceback, urllib.request, urllib.parse, uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

GC_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GC_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GC_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

SESSION_ID = str(uuid.uuid4())


def _get_access_token() -> str:
    if not GC_CLIENT_ID or not GC_REFRESH_TOKEN:
        raise RuntimeError("Google credentials not configured")
    payload = urllib.parse.urlencode({
        "client_id":     GC_CLIENT_ID,
        "client_secret": GC_CLIENT_SECRET,
        "refresh_token": GC_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def _gmail(method: str, path: str, body=None, params: dict = None) -> dict:
    token = _get_access_token()
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _decode_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace") if data else ""
    for part in payload.get("parts", []):
        text = _decode_body(part)
        if text:
            return text
    return ""


def _headers(msg: dict) -> dict:
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


def tool_gmail_read(max_messages: int = 10, label: str = "INBOX", unread_only: bool = False) -> str:
    if not GC_REFRESH_TOKEN:
        return "ERROR: Google credentials not set (GOOGLE_REFRESH_TOKEN)."
    try:
        q = "is:unread" if unread_only else ""
        result = _gmail("GET", "messages", params={
            "maxResults": max_messages, "labelIds": label,
            **({"q": q} if q else {})
        })
        messages = result.get("messages", [])
        if not messages:
            return "No messages found."
        lines = [f"Recent {len(messages)} email(s) from {label}:"]
        for m in messages:
            msg = _gmail("GET", f"messages/{m['id']}", params={"format": "metadata",
                "metadataHeaders": "From,Subject,Date"})
            h = _headers(msg)
            snippet = msg.get("snippet", "")[:100]
            lines.append(f"\n  ID: {m['id']}")
            lines.append(f"  From: {h.get('From','?')}")
            lines.append(f"  Subject: {h.get('Subject','(no subject)')}")
            lines.append(f"  Date: {h.get('Date','?')}")
            lines.append(f"  Preview: {snippet}")
        return "\n".join(lines)
    except Exception:
        return f"Gmail read error:\n{traceback.format_exc()}"


def tool_gmail_read_message(message_id: str) -> str:
    if not GC_REFRESH_TOKEN:
        return "ERROR: Google credentials not set."
    try:
        msg = _gmail("GET", f"messages/{message_id}", params={"format": "full"})
        h = _headers(msg)
        body = _decode_body(msg.get("payload", {}))
        lines = [
            f"From:    {h.get('From','?')}",
            f"To:      {h.get('To','?')}",
            f"Subject: {h.get('Subject','?')}",
            f"Date:    {h.get('Date','?')}",
            f"",
            body[:3000] or "(empty body)",
        ]
        return "\n".join(lines)
    except Exception:
        return f"Gmail read_message error:\n{traceback.format_exc()}"


def tool_gmail_search(query: str, max_results: int = 10) -> str:
    if not GC_REFRESH_TOKEN:
        return "ERROR: Google credentials not set."
    try:
        result = _gmail("GET", "messages", params={"q": query, "maxResults": max_results})
        messages = result.get("messages", [])
        if not messages:
            return f"No emails matching: {query}"
        lines = [f"Search '{query}' — {len(messages)} result(s):"]
        for m in messages:
            msg = _gmail("GET", f"messages/{m['id']}", params={"format": "metadata",
                "metadataHeaders": "From,Subject,Date"})
            h = _headers(msg)
            snippet = msg.get("snippet", "")[:80]
            lines.append(f"\n  ID: {m['id']}")
            lines.append(f"  From: {h.get('From','?')}")
            lines.append(f"  Subject: {h.get('Subject','?')}")
            lines.append(f"  Date: {h.get('Date','?')}")
            lines.append(f"  Preview: {snippet}")
        return "\n".join(lines)
    except Exception:
        return f"Gmail search error:\n{traceback.format_exc()}"


def tool_gmail_send(to: str, subject: str, body: str, reply_to_id: str = "") -> str:
    if not GC_REFRESH_TOKEN:
        return "ERROR: Google credentials not set."
    try:
        msg = MIMEMultipart()
        msg["To"] = to
        msg["Subject"] = subject
        thread_id = None
        if reply_to_id:
            original = _gmail("GET", f"messages/{reply_to_id}", params={"format": "metadata",
                "metadataHeaders": "Message-ID,References"})
            orig_headers = _headers(original)
            msg["In-Reply-To"] = orig_headers.get("Message-ID", "")
            msg["References"] = orig_headers.get("References", "") + " " + orig_headers.get("Message-ID", "")
            thread_id = original.get("threadId")
        msg.attach(MIMEText(body, "plain"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        result = _gmail("POST", "messages/send", body=payload)
        return f"Email sent. Message ID: {result.get('id','?')}"
    except Exception:
        return f"Gmail send error:\n{traceback.format_exc()}"


def tool_gmail_unread_count() -> str:
    if not GC_REFRESH_TOKEN:
        return "ERROR: Google credentials not set."
    try:
        result = _gmail("GET", "labels/INBOX")
        total = result.get("messagesTotal", "?")
        unread = result.get("messagesUnread", "?")
        return f"Inbox: {total} total, {unread} unread."
    except Exception:
        return f"Gmail count error:\n{traceback.format_exc()}"


TOOLS = [
    {
        "name": "gmail_read",
        "description": "Read recent emails from Gmail. Use unread_only=true for just new mail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_messages": {"type": "integer", "default": 10},
                "label":        {"type": "string", "default": "INBOX"},
                "unread_only":  {"type": "boolean", "default": False},
            },
            "required": [],
        },
    },
    {
        "name": "gmail_read_message",
        "description": "Read the full body of a specific Gmail message by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID from gmail_read or gmail_search"}
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "gmail_search",
        "description": "Search Gmail. Supports Gmail search syntax: from:, subject:, is:unread, after:2024/1/1, etc.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "gmail_send",
        "description": "Send an email from Andre's Gmail account. Always confirm with Andre before sending.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to":           {"type": "string"},
                "subject":      {"type": "string"},
                "body":         {"type": "string"},
                "reply_to_id":  {"type": "string", "description": "Gmail message ID to reply to (optional)"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "gmail_unread_count",
        "description": "Get the total and unread message count for the inbox.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

TOOL_FNS = {
    "gmail_read":         lambda a: tool_gmail_read(**a),
    "gmail_read_message": lambda a: tool_gmail_read_message(**a),
    "gmail_search":       lambda a: tool_gmail_search(**a),
    "gmail_send":         lambda a: tool_gmail_send(**a),
    "gmail_unread_count": lambda a: tool_gmail_unread_count(),
}

app = FastAPI()


@app.post("/")
async def handle(request: Request):
    body   = await request.json()
    method = body.get("method", "")
    id_    = body.get("id")

    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0", "id": id_,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jarvis-email", "version": "1.0.0"},
            },
        }, headers={"Mcp-Session-Id": SESSION_ID})

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
            return JSONResponse({"jsonrpc": "2.0", "id": id_,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"}})
        try:
            result = await asyncio.to_thread(lambda: fn(args))
        except Exception:
            result = f"Tool error:\n{traceback.format_exc()}"
        return JSONResponse({"jsonrpc": "2.0", "id": id_,
            "result": {"content": [{"type": "text", "text": result}]}})

    return JSONResponse({"jsonrpc": "2.0", "id": id_,
        "error": {"code": -32601, "message": f"Unknown method: {method}"}})


if __name__ == "__main__":
    creds_ok = bool(GC_REFRESH_TOKEN)
    print(f"Jarvis email server starting on port 8005")
    print(f"  Gmail credentials: {'configured' if creds_ok else 'NOT configured (set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN)'}")
    uvicorn.run(app, host="127.0.0.1", port=8005, log_level="info")
