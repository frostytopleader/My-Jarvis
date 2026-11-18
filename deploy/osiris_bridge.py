"""
OSIRIS MCP bridge — runs on port 8002 (internal only).
Injects the OSIRIS AI headers so OpenJarvis can use the full
prediction engine with the user's existing Anthropic key.
Free tools (osiris_world_brief, osiris_markets, osiris_trending)
work even without a key.
"""
import os
import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

OSIRIS_MCP_URL = "https://osirisai.live/api/mcp"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

app = FastAPI()


@app.api_route("/", methods=["GET", "POST", "DELETE"])
async def bridge(request: Request):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if ANTHROPIC_API_KEY:
        headers.update({
            "X-OI-Provider": "anthropic",
            "X-OI-Key": ANTHROPIC_API_KEY,
            "X-OI-Model": "claude-haiku-4-5-20251001",
        })
    mcp_session = request.headers.get("mcp-session-id")
    if mcp_session:
        headers["Mcp-Session-Id"] = mcp_session

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.request(
            method=request.method,
            url=OSIRIS_MCP_URL,
            headers=headers,
            content=await request.body(),
        )

    out_headers = {"content-type": resp.headers.get("content-type", "application/json")}
    if "mcp-session-id" in resp.headers:
        out_headers["mcp-session-id"] = resp.headers["mcp-session-id"]
    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8002, log_level="info")
