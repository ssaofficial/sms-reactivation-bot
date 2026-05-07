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
    """Add a contact to the bot queue (for testing or manual import)."""
    import time
    from database import upsert_contact, set_next_action
    from config import GHL_LOCATION_ID
    payload = await request.json()
    ghl_contact_id = payload.get("ghl_contact_id")
    phone = payload.get("phone")
    first_name = payload.get("first_name", "")
    test_mode = payload.get("test_mode", False)
    location_id = payload.get("location_id") or GHL_LOCATION_ID
    # delay_seconds: how many seconds until the opener fires (default 5 for test, 10 for live)
    delay_seconds = payload.get("delay_seconds", 5 if test_mode else 10)
    if not ghl_contact_id or not phone:
        return {"error": "ghl_contact_id and phone are required"}
    upsert_contact(
        ghl_contact_id=ghl_contact_id,
        phone=phone,
        first_name=first_name,
        location_id=location_id,
        test_mode=1 if test_mode else 0
    )
    # Schedule the opener to fire immediately (or after delay_seconds)
    set_next_action(ghl_contact_id, time.time() + delay_seconds)
    return {"status": "ok", "ghl_contact_id": ghl_contact_id, "phone": phone, "test_mode": test_mode, "fires_in_seconds": delay_seconds}


def start_webhook_server():
    """Start the webhook server."""
    init_db()
    initialize_variants()
    logger.info(f"[WEBHOOK] Starting server on port {WEBHOOK_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=WEBHOOK_PORT, log_level="warning")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_webhook_server()
