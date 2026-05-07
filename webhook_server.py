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
    message_id = payload.get("messageId") or payload.get("id", "")
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
    if message_id and is_message_processed(message_id):
        logger.info(f"[WEBHOOK] Already processed messageId={message_id} — skipping")
        return JSONResponse({"status": "duplicate", "messageId": message_id})

    # Mark as processed immediately
    if message_id:
        mark_message_processed(message_id)

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
        body=body
    )

    return JSONResponse({"status": "queued", "contactId": contact_id})


async def _process_after_batch_window(contact_id: str, body: str):
    """
    Wait for batch window, then process the most recent message.
    This prevents double-responses if a contact sends two messages quickly.
    """
    await _async_sleep(INBOUND_BATCH_WINDOW)

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


def start_webhook_server():
    """Start the webhook server."""
    init_db()
    initialize_variants()
    logger.info(f"[WEBHOOK] Starting server on port {WEBHOOK_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=WEBHOOK_PORT, log_level="warning")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_webhook_server()
