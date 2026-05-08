"""
webhook_server.py — FastAPI inbound webhook receiver
GHL sends inbound SMS replies to this server.
Handles idempotency, batching, and routes to agent.handle_inbound().
"""

import time
import logging
import threading
from collections import defaultdict
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
import uvicorn

from config import WEBHOOK_PORT, WEBHOOK_SECRET, INBOUND_BATCH_WINDOW
from database import is_message_processed, mark_message_processed, init_db
from agent import handle_inbound
from optimizer import initialize_variants

logger = logging.getLogger(__name__)
app = FastAPI(title="SMS Bot Webhook")

# Inbound message buffer — batches rapid messages from same contact
# {contact_id: [{"message_id": ..., "body": ..., "timestamp": ...}]}
_inbound_buffer = defaultdict(list)
_buffer_lock = threading.Lock()


@app.post("/webhook/inbound")
async def inbound_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive inbound SMS from GHL.
    GHL sends a POST with the message data when a contact replies.
    """
    # Optional: verify webhook secret
    if WEBHOOK_SECRET:
        auth = request.headers.get("X-Webhook-Secret", "")
        if auth != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"[WEBHOOK] Inbound payload received: {str(payload)[:200]}")

    # Extract fields from GHL webhook payload
    # GHL InboundMessage webhook format:
    raw_message_id = payload.get("messageId") or payload.get("id", "")
    # GHL sometimes sends messageId as the string "null" — treat that as no ID
    message_id = raw_message_id if (raw_message_id and str(raw_message_id).lower() != "null") else ""
    contact_id = (
        payload.get("contactId") or
        payload.get("contact", {}).get("id", "")
    )
    body = (
        payload.get("body") or
        payload.get("message") or
        payload.get("text", "")
    )
    message_type = payload.get("type", "SMS")

    # Only process SMS/MMS
    if message_type not in ("SMS", "MMS", ""):
        return JSONResponse({"status": "ignored", "reason": "not SMS"})

    if not contact_id or not body:
        logger.warning(f"[WEBHOOK] Missing contactId or body: {payload}")
        return JSONResponse({"status": "ignored", "reason": "missing fields"})

    # Idempotency check — ignore duplicate webhook fires
    # When GHL sends messageId="null", fall back to a body+contact+timestamp fingerprint
    # to deduplicate rapid double-fires without blocking distinct real messages.
    if message_id:
        if is_message_processed(message_id):
            logger.info(f"[WEBHOOK] Already processed messageId={message_id} — skipping")
            return JSONResponse({"status": "duplicate", "messageId": message_id})
        mark_message_processed(message_id)
    else:
        # No real message ID — use a 120-second window fingerprint to catch GHL double-fires
        import hashlib
        window = int(time.time() / 120)  # 120-second bucket
        fingerprint = hashlib.md5(f"{contact_id}:{body}:{window}".encode()).hexdigest()
        if is_message_processed(fingerprint):
            logger.info(f"[WEBHOOK] Duplicate (no messageId) — fingerprint={fingerprint[:8]} — skipping")
            return JSONResponse({"status": "duplicate", "fingerprint": fingerprint[:8]})
        mark_message_processed(fingerprint)
        logger.info(f"[WEBHOOK] No messageId from GHL — using fingerprint dedup: {fingerprint[:8]}")

    # CRITICAL: Set last_inbound_at and clear next_action_at IMMEDIATELY on inbound reply
    # This prevents the race condition where follow-ups fire after a reply arrives.
    # The scheduler checks last_inbound_at > last_outbound_at and skips if true.
    try:
        from database import set_last_inbound_at as _set_inbound
        _set_inbound(contact_id)
        logger.info(f"[WEBHOOK] Set last_inbound_at and cleared next_action_at for {contact_id}")
    except Exception as e:
        logger.warning(f"[WEBHOOK] Could not set last_inbound_at for {contact_id}: {e}")

    # Determine batch window — test mode uses 2 seconds, live uses configured window
    try:
        from database import get_contact as _gc
        _c = _gc(contact_id)
        batch_window = 2 if (_c and _c.get('test_mode')) else INBOUND_BATCH_WINDOW
    except Exception:
        batch_window = INBOUND_BATCH_WINDOW

    # Add to buffer (handles rapid double-texts from same contact)
    with _buffer_lock:
        _inbound_buffer[contact_id].append({
            "message_id": message_id,
            "body": body,
            "timestamp": time.time()
        })

    # Schedule processing after batch window
    background_tasks.add_task(
        _process_after_batch_window,
        contact_id=contact_id,
        body=body,
        batch_window=batch_window
    )

    return JSONResponse({"status": "queued", "contactId": contact_id})


async def _process_after_batch_window(contact_id: str, body: str, batch_window: float = None):
    """
    Wait for batch window, then process the most recent message.
    This prevents double-responses if a contact sends two messages quickly.
    Test mode uses 2 seconds; live mode uses INBOUND_BATCH_WINDOW.
    """
    if batch_window is None:
        batch_window = INBOUND_BATCH_WINDOW
    await _async_sleep(batch_window)

    with _buffer_lock:
        messages = _inbound_buffer.pop(contact_id, [])

    if not messages:
        return

    # Process only the most recent message in the batch
    latest = max(messages, key=lambda m: m["timestamp"])
    logger.info(f"[WEBHOOK] Processing batched message for {contact_id}: '{latest['body'][:50]}'")

    try:
        handle_inbound(contact_id, latest["body"])
    except Exception as e:
        logger.error(f"[WEBHOOK] Error handling inbound for {contact_id}: {e}")


async def _async_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": time.time()}


@app.get("/stats")
async def stats():
    """Quick stats endpoint for monitoring."""
    from database import get_conn
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='active'").fetchone()[0]
    booked = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='booked'").fetchone()[0]
    lost = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='lost'").fetchone()[0]
    opted_out = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='opted_out'").fetchone()[0]
    messages_sent = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='outbound'").fetchone()[0]
    messages_received = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='inbound'").fetchone()[0]
    conn.close()

    return {
        "contacts": {
            "total": total,
            "active": active,
            "booked": booked,
            "lost": lost,
            "opted_out": opted_out
        },
        "messages": {
            "sent": messages_sent,
            "received": messages_received
        }
    }


@app.get("/report")
async def performance_report():
    """Bandit performance report."""
    from optimizer import get_performance_report
    return {"report": get_performance_report()}


@app.post("/admin/add_contact")
async def admin_add_contact(request: Request):
    """Add a contact to the bot queue (for testing or manual import).
    
    If the contact already has a conversation in GHL, reads the history and
    resumes at the correct sequence step instead of restarting from step 0.
    """
    import time
    from database import upsert_contact, set_next_action, get_contact
    from config import GHL_LOCATION_ID
    from ghl_client import get_conversation_messages
    payload = await request.json()
    ghl_contact_id = payload.get("ghl_contact_id")
    phone = payload.get("phone")
    first_name = payload.get("first_name", "")
    test_mode = payload.get("test_mode", False)
    location_id = payload.get("location_id") or GHL_LOCATION_ID
    # delay_seconds: how many seconds until the next action fires
    delay_seconds = payload.get("delay_seconds", 5 if test_mode else 10)
    if not ghl_contact_id or not phone:
        return {"error": "ghl_contact_id and phone are required"}

    # Upsert the contact (preserves existing step/status on conflict)
    upsert_contact(
        ghl_contact_id=ghl_contact_id,
        phone=phone,
        first_name=first_name,
        location_id=location_id,
        test_mode=1 if test_mode else 0
    )

    # Check if this contact already has a conversation in GHL
    # If so, infer the correct sequence step from history instead of restarting
    inferred_step = 0
    action = "send_opener"
    try:
        messages = get_conversation_messages(ghl_contact_id, limit=20)
        if messages:
            # Build a simple summary of the conversation for the LLM
            from openai import OpenAI
            from config import OPENAI_API_KEY, CLASSIFIER_MODEL
            oai = OpenAI(api_key=OPENAI_API_KEY)
            convo_lines = []
            for m in messages[-15:]:
                direction = "THEM" if m.get("direction") == "inbound" else "BOT"
                body = m.get("body", "").strip()
                if body:
                    convo_lines.append(f"{direction}: {body}")
            convo_text = "\n".join(convo_lines)

            system_prompt = """You are analyzing an SMS conversation between a bot and a tree service business owner.

The bot's sequence has these steps:
- step 0: No opener sent yet — bot should send the opener
- step 1: Opener sent, waiting for reply
- step 2: Contact replied to opener, bot sent reintro/qualifier, waiting for qualifier reply
- step 3: Contact confirmed tree biz, bot scheduled AI curiosity message
- step 4: AI curiosity sent, waiting for reply
- step 5: Contact replied to AI curiosity, bot sent nurture/social proof, scheduled soft close
- step 6: Soft close sent, contact replied — bot is in LLM-driven conversation
- step 7+: Deep LLM conversation, trying to book a call

Based on the conversation below, return a JSON object:
{
  "step": <integer 0-7>,
  "action": "send_opener" | "wait_for_reply" | "resume_llm" | "already_lost",
  "reason": "<one sentence explaining why>"
}

Rules:
- If no messages exist or only a bot opener with no reply: step=1, action=wait_for_reply
- If contact has replied and conversation is ongoing: determine step from context
- If contact said stop/unsubscribe/not interested: action=already_lost
- If conversation is deep (multiple back-and-forths): step=7, action=resume_llm"""

            resp = oai.chat.completions.create(
                model=CLASSIFIER_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Conversation:\n{convo_text}"}
                ],
                max_tokens=150,
                temperature=0
            )
            import json as _json
            result = _json.loads(resp.choices[0].message.content.strip())
            inferred_step = result.get("step", 0)
            action = result.get("action", "send_opener")
            logger.info(f"[ADD_CONTACT] {ghl_contact_id} | inferred step={inferred_step} action={action} | {result.get('reason','')}")

            # Update the contact's step in DB to match inferred state
            if inferred_step > 0:
                import sqlite3
                from database import get_conn
                conn = get_conn()
                conn.execute(
                    "UPDATE contacts SET sequence_step=? WHERE ghl_contact_id=?",
                    (inferred_step, ghl_contact_id)
                )
                conn.commit()
                conn.close()

    except Exception as e:
        logger.warning(f"[ADD_CONTACT] Could not infer step for {ghl_contact_id}: {e}")
        inferred_step = 0
        action = "send_opener"

    # Decide what to do next
    if action == "already_lost":
        from database import update_contact_status
        update_contact_status(ghl_contact_id, "lost")
        return {"status": "skipped", "reason": "contact previously opted out", "ghl_contact_id": ghl_contact_id}
    elif action == "wait_for_reply":
        # Opener already sent — just wait, don't reschedule
        return {"status": "ok", "ghl_contact_id": ghl_contact_id, "phone": phone,
                "test_mode": test_mode, "action": "waiting_for_reply", "step": inferred_step}
    elif action == "resume_llm":
        # Deep in conversation — set next_action_at so scheduler can handle follow-up if no reply
        set_next_action(ghl_contact_id, time.time() + delay_seconds)
        return {"status": "ok", "ghl_contact_id": ghl_contact_id, "phone": phone,
                "test_mode": test_mode, "action": "resume_llm", "step": inferred_step}
    else:
        # send_opener (step 0, no prior conversation)
        set_next_action(ghl_contact_id, time.time() + delay_seconds)
        return {"status": "ok", "ghl_contact_id": ghl_contact_id, "phone": phone,
                "test_mode": test_mode, "fires_in_seconds": delay_seconds, "action": "send_opener"}


def start_webhook_server():
    """Start the webhook server."""
    init_db()
    initialize_variants()
    logger.info(f"[WEBHOOK] Starting server on port {WEBHOOK_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=WEBHOOK_PORT, log_level="warning")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_webhook_server()
