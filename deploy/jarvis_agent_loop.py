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

LOOP_INTERVAL_SEC   = 300    # run every 5 minutes
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
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Market data
    btc  = _mcp(8003, "crypto_price", {"symbols": "BTC,ETH,SOL"})
    fg   = _mcp(8004, "fear_greed_composite", {"include_breakdown": True})
    news = _mcp(8003, "market_news",
                {"query": "bitcoin crypto markets macro asteroid mining space", "max_items": 5})

    # Business intelligence — UnderRoof & NexFlow
    underroof_news = _mcp(8003, "market_news",
                          {"query": "home inventory insurance app homeowner iPhone startup Encircle Sortly", "max_items": 3})
    nexflow_news   = _mcp(8003, "market_news",
                          {"query": "AI automation workflow agency consulting manual process pain small business", "max_items": 3})

    # Notion memory — recent call topics (what has been discussed)
    call_log = _mcp(8006, "notion_read_database",
                    {"database_id": "41c072ec-4279-81f9-95b3-000bc0008c74", "max_rows": 5})

    # Pending / recent actions
    action_log = _mcp(8006, "notion_read_database",
                      {"database_id": "41c072ec-4279-81f7-8f82-000bc6e8ed09", "max_rows": 8})

    # Entities Andre is actively monitoring
    entity_mon = _mcp(8006, "notion_read_database",
                      {"database_id": "41c072ec-4279-81e6-ae63-000b3970e27a", "max_rows": 10})

    # Decision log (what choices have been made recently)
    decisions = _mcp(8006, "notion_read_database",
                     {"database_id": "41c072ec-4279-8129-9584-000bab322463", "max_rows": 5})

    # Andre's full dossier (local file written at startup)
    dossier_path = Path(os.path.expanduser("~/.openjarvis/andre-dossier.md"))
    dossier = dossier_path.read_text()[:1200] if dossier_path.exists() else "[dossier unavailable]"

    return {
        "timestamp":        ts,
        "prices":           btc,
        "fear_greed":       fg,
        "news":             news,
        "underroof_news":   underroof_news,
        "nexflow_news":     nexflow_news,
        "recent_calls":     call_log,
        "pending_actions":  action_log,
        "entity_monitor":   entity_mon,
        "recent_decisions": decisions,
        "dossier":          dossier,
    }


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

    prompt = f"""You are Jarvis — Andre Champeau's full-time AI employee — running an autonomous \
check at {ts}. Andre is NOT on a call. You act on your own initiative. \
Your job is to make Andre money across ALL his ventures.

Last call you placed: {call_ago}. Last SMS: {sms_ago}.
Cooldowns enforced by the system: calls ≥2h apart, SMS ≥1h apart.

━━━ ANDRE + ACTIVE VENTURES ━━━
{context['dossier'][:900]}

━━━ RECENT CALLS (what was discussed) ━━━
{context['recent_calls'][:600]}

━━━ PENDING ACTIONS ━━━
{context['pending_actions'][:500]}

━━━ ENTITIES BEING MONITORED ━━━
{context['entity_monitor'][:400]}

━━━ RECENT DECISIONS ━━━
{context['recent_decisions'][:300]}

━━━ MARKET STATE ━━━
{context['prices']}
Fear/Greed: {context['fear_greed'][:200]}

━━━ CRYPTO & MACRO NEWS ━━━
{context['news'][:500]}

━━━ UNDERROOF & INSURANCE INDUSTRY NEWS ━━━
{context['underroof_news'][:400]}

━━━ AI AUTOMATION & NEXFLOW MARKET NEWS ━━━
{context['nexflow_news'][:400]}

━━━ YOUR JOB RIGHT NOW ━━━
You work ACROSS all these areas — not just crypto. Think like a chief of staff and \
business operator. Ask yourself:

UNDERROOF: Did a competitor (Encircle, Sortly, Lemonade) make a move? Insurance industry news \
relevant to a home inventory product? A partnership angle worth flagging? App store news?

NEXFLOW AI (ACTIVE, OUTREACH PHASE): News about agency/consulting/service business pain points \
(manual workflows, lead handling, onboarding chaos)? A specific company in the news that fits ICP \
(1–20 person agencies, consulting firms, service businesses)? Competitor (Zapier/Make/n8n) doing \
something that creates a sales angle? Log strong prospects as NOTION entries for the Deal Tracker.

TRADING DESK: BTC significant move, fear/greed extreme, macro event, options expiry?

GENERAL BUSINESS: Did something come up in the news that creates an opportunity for Andre? \
Is a pending action overdue? Is there a business idea worth researching?

Reply with EXACTLY ONE line in one of these formats:
  NOOP
  SMS: <message to Andre, plain text, max 300 chars>
  CALL: <one-line reason Jarvis is calling, max 100 chars>
  NOTION: <entry title> | <summary to log to Action Log>

Trigger guidance:
- CALL: BTC ≥5% move, fear/greed extreme (<15 or >85), breaking news directly relevant to UnderRoof/NexFlow/CMC/SAC/trading desk, or a time-sensitive pending action
- SMS: notable market moves (3–5%), competitor news for UnderRoof or NexFlow, insurance industry development, monitored entity update, upcoming deadlines, new business opportunity worth flagging
- NOTION: log significant business intelligence, competitive observations, or market research even when no immediate action is needed
- NOOP: genuinely nothing worth surfacing

One line only. No explanation."""

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
    log.info("Jarvis agency loop started — interval: 5 min")
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
