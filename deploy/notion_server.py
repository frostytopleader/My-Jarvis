"""
Jarvis Notion Server — Notion workspace on port 8006.

Required Railway Variable:
  NOTION_API_KEY — from https://www.notion.so/my-integrations
  Share each page/database with the integration inside Notion.
"""
from __future__ import annotations
import asyncio, json, os, traceback, urllib.request, urllib.parse, uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_VERSION = "2022-06-28"
SESSION_ID     = str(uuid.uuid4())


def _notion(method: str, path: str, body=None) -> dict:
    if not NOTION_API_KEY:
        raise RuntimeError("NOTION_API_KEY not configured")
    url  = f"https://api.notion.com/v1/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization":  f"Bearer {NOTION_API_KEY}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type":   "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _rich_text(text: str) -> list:
    return [{"type": "text", "text": {"content": text[:2000]}}]


def _extract_title(page: dict) -> str:
    props = page.get("properties", {})
    for key in ["Name", "Title", "title", "name"]:
        if key in props:
            rt = props[key].get("title", [])
            if rt:
                return "".join(t.get("plain_text", "") for t in rt)
    return page.get("id", "untitled")


def _blocks_to_text(blocks: list) -> str:
    lines = []
    for b in blocks:
        btype   = b.get("type", "")
        content = b.get(btype, {})
        rt      = content.get("rich_text", [])
        text    = "".join(t.get("plain_text", "") for t in rt)
        if btype == "heading_1":
            lines.append(f"# {text}")
        elif btype == "heading_2":
            lines.append(f"## {text}")
        elif btype == "heading_3":
            lines.append(f"### {text}")
        elif btype in ("bulleted_list_item",):
            lines.append(f"  • {text}")
        elif btype == "to_do":
            checked = "✅" if content.get("checked") else "☐"
            lines.append(f"  {checked} {text}")
        elif btype == "numbered_list_item":
            lines.append(f"  1. {text}")
        elif btype == "code":
            lang = content.get("language", "")
            lines.append(f"```{lang}\n{text}\n```")
        elif btype == "divider":
            lines.append("---")
        elif text:
            lines.append(text)
    return "\n".join(lines)


def _clean_id(raw: str) -> str:
    """Extract UUID from a Notion URL or return the raw ID."""
    if "notion.so" in raw:
        raw = raw.rstrip("/").split("?")[0].split("#")[0]
        raw = raw.split("-")[-1] if "-" in raw.split("/")[-1] else raw.split("/")[-1]
    raw = raw.replace("-", "")
    if len(raw) == 32:
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return raw


def tool_notion_search(query: str, max_results: int = 10) -> str:
    if not NOTION_API_KEY:
        return "ERROR: NOTION_API_KEY not set."
    try:
        result = _notion("POST", "search", {"query": query, "page_size": max_results})
        items  = result.get("results", [])
        if not items:
            return f"No Notion pages found matching: {query}"
        lines = [f"Notion search '{query}' — {len(items)} result(s):"]
        for item in items:
            obj_type = item.get("object", "?")
            title    = _extract_title(item)
            lines.append(f"  [{obj_type}] {title}  (id: {item['id']})")
        return "\n".join(lines)
    except Exception:
        return f"Notion search error:\n{traceback.format_exc()}"


def tool_notion_read_page(page_id: str) -> str:
    if not NOTION_API_KEY:
        return "ERROR: NOTION_API_KEY not set."
    try:
        pid    = _clean_id(page_id)
        page   = _notion("GET", f"pages/{pid}")
        title  = _extract_title(page)
        result = _notion("GET", f"blocks/{pid}/children?page_size=100")
        blocks = result.get("results", [])
        content = _blocks_to_text(blocks)
        return f"# {title}\n\n{content}" if content else f"# {title}\n\n(empty page)"
    except Exception:
        return f"Notion read error:\n{traceback.format_exc()}"


def tool_notion_create_page(parent_id: str, title: str, content: str = "") -> str:
    if not NOTION_API_KEY:
        return "ERROR: NOTION_API_KEY not set."
    try:
        pid  = _clean_id(parent_id)
        body = {
            "parent": {"page_id": pid},
            "properties": {"title": {"title": _rich_text(title)}},
        }
        if content:
            paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
            body["children"] = [
                {"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": _rich_text(p[:2000])}}
                for p in paragraphs[:100]
            ]
        result = _notion("POST", "pages", body)
        return f"Page created: '{title}' (id: {result['id']})"
    except Exception:
        return f"Notion create error:\n{traceback.format_exc()}"


def tool_notion_append_to_page(page_id: str, content: str) -> str:
    if not NOTION_API_KEY:
        return "ERROR: NOTION_API_KEY not set."
    try:
        pid        = _clean_id(page_id)
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        children   = [
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": _rich_text(p[:2000])}}
            for p in paragraphs[:50]
        ]
        _notion("PATCH", f"blocks/{pid}/children", {"children": children})
        return f"Appended {len(children)} block(s) to page {pid}."
    except Exception:
        return f"Notion append error:\n{traceback.format_exc()}"


def tool_notion_read_database(database_id: str, filter_json: str = "") -> str:
    if not NOTION_API_KEY:
        return "ERROR: NOTION_API_KEY not set."
    try:
        did  = _clean_id(database_id)
        body = {"page_size": 50}
        if filter_json:
            body["filter"] = json.loads(filter_json)
        result = _notion("POST", f"databases/{did}/query", body)
        rows   = result.get("results", [])
        if not rows:
            return "Database is empty or no rows match filter."
        lines = [f"Database rows ({len(rows)}):"]
        for row in rows:
            title       = _extract_title(row)
            props       = row.get("properties", {})
            prop_summary = []
            for k, v in list(props.items())[:6]:
                vtype = v.get("type", "")
                if vtype == "title":
                    continue
                raw = v.get(vtype)
                if isinstance(raw, list):
                    text = ", ".join(t.get("plain_text", "") for t in raw if isinstance(t, dict))
                elif isinstance(raw, dict):
                    text = str(raw.get("start", raw.get("name", raw.get("content", ""))))
                else:
                    text = str(raw) if raw is not None else ""
                if text:
                    prop_summary.append(f"{k}: {text[:50]}")
            lines.append(f"  • {title}  —  {' | '.join(prop_summary)}")
        return "\n".join(lines)
    except Exception:
        return f"Notion DB error:\n{traceback.format_exc()}"


TOOLS = [
    {
        "name": "notion_search",
        "description": "Search across all Notion pages and databases the integration has access to.",
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
        "name": "notion_read_page",
        "description": "Read the full content of a Notion page by ID or URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Notion page ID (UUID) or full page URL"}
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "notion_create_page",
        "description": "Create a new Notion page inside a parent page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "parent_id": {"type": "string", "description": "Parent page ID or URL"},
                "title":     {"type": "string"},
                "content":   {"type": "string", "description": "Page body text (use double newlines for paragraphs)"},
            },
            "required": ["parent_id", "title"],
        },
    },
    {
        "name": "notion_append_to_page",
        "description": "Append text blocks to an existing Notion page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["page_id", "content"],
        },
    },
    {
        "name": "notion_read_database",
        "description": "Read rows from a Notion database table. Optionally pass a Notion filter as JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database_id": {"type": "string", "description": "Notion database ID or URL"},
                "filter_json": {"type": "string", "description": "Optional Notion filter object as JSON string"},
            },
            "required": ["database_id"],
        },
    },
]

TOOL_FNS = {
    "notion_search":         lambda a: tool_notion_search(**a),
    "notion_read_page":      lambda a: tool_notion_read_page(**a),
    "notion_create_page":    lambda a: tool_notion_create_page(**a),
    "notion_append_to_page": lambda a: tool_notion_append_to_page(**a),
    "notion_read_database":  lambda a: tool_notion_read_database(**a),
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
                "serverInfo": {"name": "jarvis-notion", "version": "1.0.0"},
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
    print(f"Jarvis Notion server starting on port 8006")
    print(f"  NOTION_API_KEY: {'configured' if NOTION_API_KEY else 'NOT configured — set NOTION_API_KEY in Railway Variables'}")
    uvicorn.run(app, host="127.0.0.1", port=8006, log_level="info")
