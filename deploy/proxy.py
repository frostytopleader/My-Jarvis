"""
Jarvis proxy server.
- Handles POST /call/me  → triggers ElevenLabs outbound call to Andre
- GET /debug            → shows MCP server health + tool counts
- Proxies everything else → OpenJarvis on localhost:8001
"""
import os, asyncio, json
import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

UPSTREAM = "http://127.0.0.1:8001"
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = "agent_2801m7nv7wmzf9xt3rvk856w2536"
ELEVENLABS_PHONE_NUMBER_ID = "phnum_2401kt2rxp7zfan955wkcx3zj56f"
DEFAULT_TO_NUMBER = "+13032699875"

app = FastAPI()

MCP_SERVERS = {
    "osiris":    "http://127.0.0.1:8002",
    "tools":     "http://127.0.0.1:8003",
    "intel":     "http://127.0.0.1:8004",
}

INIT_PAYLOAD = json.dumps({
    "jsonrpc": "2.0", "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "debug", "version": "0.0.1"}},
    "id": 1,
})

LIST_PAYLOAD = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2})


@app.get("/debug")
async def debug():
    """Check MCP servers are reachable and report tool counts."""
    results = {}
    async with httpx.AsyncClient(timeout=5) as client:
        for name, url in MCP_SERVERS.items():
            try:
                r = await client.post(url, content=INIT_PAYLOAD,
                                      headers={"Content-Type": "application/json"})
                if r.status_code != 200:
                    results[name] = {"status": "error", "http": r.status_code}
                    continue
                r2 = await client.post(url, content=LIST_PAYLOAD,
                                       headers={"Content-Type": "application/json"})
                tools = r2.json().get("result", {}).get("tools", [])
                results[name] = {"status": "ok", "tools": len(tools),
                                 "names": [t["name"] for t in tools]}
            except Exception as exc:
                results[name] = {"status": "unreachable", "error": str(exc)}

    # Also ping OpenJarvis
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            oj = await client.get("http://127.0.0.1:8001/health")
            results["openjarvis"] = {"status": "ok", "http": oj.status_code}
    except Exception as exc:
        results["openjarvis"] = {"status": "unreachable", "error": str(exc)}

    all_ok = all(v.get("status") == "ok" for v in results.values())
    return JSONResponse({"healthy": all_ok, "servers": results})


@app.post("/call/me")
async def call_me(request: Request):
    """Trigger Jarvis to call Andre via ElevenLabs outbound calling."""
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"error": "ELEVENLABS_API_KEY not set"}, status_code=503)

    try:
        body = await request.json()
        to_number = body.get("to", DEFAULT_TO_NUMBER)
    except Exception:
        to_number = DEFAULT_TO_NUMBER

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.elevenlabs.io/v1/convai/twilio/outbound_call",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "agent_id": ELEVENLABS_AGENT_ID,
                "agent_phone_number_id": ELEVENLABS_PHONE_NUMBER_ID,
                "to_number": to_number,
            },
        )

    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request):
    """Forward all other requests to OpenJarvis."""
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.request(
            method=request.method,
            url=f"{UPSTREAM}/{path}",
            headers=headers,
            content=await request.body(),
            params=dict(request.query_params),
            follow_redirects=False,
        )
    skip = {"transfer-encoding", "connection", "keep-alive", "te", "trailers", "upgrade"}
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in skip}
    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
