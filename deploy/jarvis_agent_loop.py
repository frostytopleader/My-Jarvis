"""
Jarvis Persistent Agency Loop
Runs continuously alongside the MCP servers. Every 30 minutes Jarvis wakes up,
collects current context (prices, news, fear/greed, Notion health), calls the
Anthropic API, and decides on its own whether to call Andre, send an SMS, write
to Notion, or do nothing.

This is NOT a cron job — it is Jarvis reasoning autonomously between conversations.

Standing permissions (granted by Andre):
- Proactive outbound calls: YES
- Autonomous SMS alerts: YES
- Autonomous Notion writes: YES (Action Log, Health Board)
- Alert calls on significant events: YES
"""
import json
import logging
import os
import sqlite3
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agency-loop] %(levelname)s %(message)s",
)
log = logging.getLogger("agency-loop")

# ── Config ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = "agent_2801m7nv7wmzf9xt3rvk856w2536"
ELEVENLABS_PHONE_ID = "phnum_2401kt2rxp7zfan955wkcx3zj56f"
TWILIO_SID          = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN        = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM         = os.environ.get("TWILIO_PHONE_NUMBER", "")
ANDRE_PHONE         = os.environ.get("ANDRE_PHONE_NUMBER", "+17209493506")

JARVIS_DIR  = Path(os.path.expanduser("~/.openjarvis"))
NOTES_DIR   = JARVIS_DIR / "notes"
DB_PATH     = str(JARVIS_DIR / "agency.db")

LOOP_INTERVAL_SEC   = 1800   # run every 30 minutes
CALL_COOLDOWN_SEC   = 7200   # never call more than once per 2 hours
SMS_COOLDOWN_SEC    = 3600   # never SMS more than once per hour
QUIET_HOURS_START   = 23     # 11 PM MT (06:00 UTC next day) — no calls/SMS
QUIET_HOURS_END     = 13     # 6 AM MT (13:00 UTC) — resume


# ── DB helpers ─────────────────────────────────────────────────────────────────
def _db():
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agency_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            reason TEXT,
            fired_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        )
    """)
    conn.commit()
    return conn


def _last_action_time(action: str) -> datetime | None:
    conn = _db()
    row = conn.execute(
        "SELECT fired_at FROM agency_log WHERE action=? ORDER BY fired_at DESC LIMIT 1",
        (action,)
    ).fetchone()
    conn.close()
    if row:
        try:
            return datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _log_action(action: str, reason: str):
    conn = _db()
    conn.execute("INSERT INTO agency_log (action, reason) VALUES (?,?)", (action, reason))
    conn.commit()
    conn.close()


# ── Quiet-hours check (UTC) ────────────────────────────────────────────────────
def _in_quiet_hours() -> bool:
    h = datetime.now(timezone.utc).hour
    if QUIET_HOURS_START <= QUIET_HOURS_END:
        return QUIET_HOURS_START <= h < QUIET_HOURS_END
    return h >= QUIET_HOURS_START or h < QUIET_HOURS_END


def _can_act(action: str, cooldown_sec: int) -> bool:
    if _in_quiet_hours():
        return False
    last = _last_action_time(action)
    if last is None:
        return True
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return elapsed >= cooldown_sec


# ── MCP helpers ────────────────────────────────────────────────────────────────
def _mcp(port: int, tool: str, args: dict) -> str:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args}
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
            content = data.get("result", {}).get("content", [])
            return content[0].get("text", "") if content else str(data)
    except Exception as e:
        return f"[unavailable: {e}]"


# ── Action executors ───────────────────────────────────────────────────────────
def _send_sms(msg: str):
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM):
        log.warning("Twilio not configured — SMS skipped")
        return
    import base64
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    data = urllib.parse.urlencode({"To": ANDRE_PHONE, "From": TWILIO_FROM, "Body": msg[:1600]}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    creds = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")
    try:
        urllib.request.urlopen(req, timeout=15)
        log.info("SMS sent")
    except Exception as e:
        log.error(f"SMS failed: {e}")


def _initiate_call():
    if not ELEVENLABS_API_KEY:
        log.warning("ELEVENLABS_API_KEY not set — call skipped")
        return
    payload = json.dumps({
        "agent_id": ELEVENLABS_AGENT_ID,
        "agent_phone_number_id": ELEVENLABS_PHONE_ID,
        "to_number": ANDRE_PHONE,
    }).encode()
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/convai/twilio/outbound_call",
        data=payload,
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            log.info(f"Call initiated: {resp}")
    except Exception as e:
        log.error(f"Call failed: {e}")


def _write_brief(filename: str, content: str):
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    (NOTES_DIR / filename).write_text(content)


# ── Context collector ──────────────────────────────────────────────────────────
def _collect_context() -> dict:
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    btc = _mcp(8003, "crypto_price", {"symbols": "BTC,ETH,SOL"})
    fg  = _mcp(8004, "fear_greed_composite", {"include_breakdown": True})
    news = _mcp(8003, "market_news", {"query": "bitcoin crypto markets macro", "max_items": 5})
    return {"timestamp": ts, "prices": btc, "fear_greed": fg, "news": news}


# ── Anthropic reasoning call ───────────────────────────────────────────────────
def _ask_jarvis(context: dict) -> str:
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — reasoning skipped")
        return "NOOP"

    ts   = context["timestamp"]
    last_call = _last_action_time("call")
    last_sms  = _last_action_time("sms")
    call_ago  = (
        f"{int((datetime.now(timezone.utc) - last_call).total_seconds() / 60)}m ago"
        if last_call else "never"
    )
    sms_ago = (
        f"{int((datetime.now(timezone.utc) - last_sms).total_seconds() / 60)}m ago"
        if last_sms else "never"
    )

    prompt = f"""You are Jarvis running an autonomous agency check at {ts}.

Andre has given standing permission for proactive calls, SMS, and Notion writes.
Last call: {call_ago}. Last SMS: {sms_ago}.
Cooldowns: calls every 2h, SMS every 1h.

CURRENT MARKET STATE:
{context['prices']}

FEAR/GREED COMPOSITE:
{context['fear_greed'][:400]}

TOP NEWS:
{context['news'][:800]}

Based on this, decide what action (if any) to take:

Reply with EXACTLY ONE of these formats:
  NOOP
  SMS: [message to send Andre, max 300 chars]
  CALL: [one-line reason why calling, max 100 chars]
  NOTION: [title] | [brief summary to log to Action Log]

Thresholds to guide you:
- Call if BTC moved >5% in 24h, fear/greed is extreme (<15 or >85), or genuinely critical news
- SMS for notable moves (3-5%), interesting macro news, or useful market intel
- NOTION to log significant market events autonomously
- NOOP if nothing warrants attention right now
- Do not call during quiet hours (11pm-6am MT) — the system handles this, so decide as if awake

Respond with ONLY the action line. Nothing else."""

    payload = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 150,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            decision = data["content"][0]["text"].strip()
            log.info(f"Jarvis decision: {decision}")
            return decision
    except Exception as e:
        log.error(f"Anthropic API call failed: {e}")
        return "NOOP"


# ── Decision executor ─────────────────────────────────────────────────────────
def _execute(decision: str, context: dict):
    if decision.startswith("NOOP") or not decision:
        log.info("NOOP — no action taken")
        return

    if decision.startswith("CALL:"):
        reason = decision[5:].strip()
        if not _can_act("call", CALL_COOLDOWN_SEC):
            log.info(f"CALL blocked (quiet hours or cooldown): {reason}")
            # Downgrade to SMS if possible
            if _can_act("sms", SMS_COOLDOWN_SEC):
                _send_sms(f"[Jarvis — deferred call] {reason}\n{context['prices'][:200]}")
                _log_action("sms", f"deferred from call: {reason}")
            return
        # Write brief so Jarvis can reference it on the call
        brief = (f"=== AUTONOMOUS CALL BRIEF ===\n{context['timestamp']}\n\n"
                 f"REASON: {reason}\n\nPRICES:\n{context['prices']}\n\n"
                 f"FEAR/GREED:\n{context['fear_greed'][:300]}\n\n"
                 f"NEWS:\n{context['news'][:600]}")
        _write_brief("autonomous_brief.md", brief)
        _send_sms(f"[Jarvis] Calling now — {reason}")
        time.sleep(4)
        _initiate_call()
        _log_action("call", reason)

    elif decision.startswith("SMS:"):
        msg = decision[4:].strip()
        if not _can_act("sms", SMS_COOLDOWN_SEC):
            log.info(f"SMS blocked (quiet hours or cooldown): {msg[:60]}")
            return
        _send_sms(f"[Jarvis] {msg}")
        _log_action("sms", msg[:200])

    elif decision.startswith("NOTION:"):
        parts = decision[7:].split("|", 1)
        title   = parts[0].strip() if parts else "Autonomous Log"
        summary = parts[1].strip() if len(parts) > 1 else decision
        _mcp(8006, "notion_create_entry_page", {
            "parent_database_id": "41c072ec-4279-81f7-8f82-000bc6e8ed09",
            "title": f"{context['timestamp'][:10]} — {title}",
            "summary": summary,
            "full_notes": f"Prices: {context['prices']}\n\nF/G: {context['fear_greed'][:200]}",
            "jarvis_analysis": "Logged autonomously by Jarvis agency loop",
            "action_items": ""
        })
        _log_action("notion", title)

    else:
        log.warning(f"Unrecognised decision format: {decision}")


# ── Main loop ──────────────────────────────────────────────────────────────────
def run():
    log.info("Jarvis agency loop started — interval: 30 min")
    while True:
        try:
            log.info("Agency check running…")
            context  = _collect_context()
            decision = _ask_jarvis(context)
            _execute(decision, context)
        except Exception as e:
            log.error(f"Agency loop error: {e}")
        time.sleep(LOOP_INTERVAL_SEC)


if __name__ == "__main__":
    run()
