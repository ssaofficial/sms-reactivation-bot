"""
scheduler.py — Persistent task scheduler
Polls the State DB every 60 seconds for contacts whose next_action_at has passed.
Fires the correct action (opener, follow-up, AI curiosity, soft close).
Handles send window enforcement (no sends on Sunday, 9am-6pm local time only).
Does NOT use time.sleep() for long delays — uses DB timestamps instead.
"""

import time
import logging
import threading
from datetime import datetime
import pytz

from config import (
    SEND_WINDOW_START_HOUR, SEND_WINDOW_END_HOUR,
    SEND_BLOCKED_DAYS, MAX_NEW_CONTACTS_PER_DAY
)
from database import (
    get_contacts_due_for_action, get_contact,
    update_contact_status, set_next_action, init_db
)
from agent import send_opener, send_followup, _send_ai_curiosity, _send_soft_close
from optimizer import initialize_variants

logger = logging.getLogger(__name__)

# Track daily send count
_daily_send_count = {"date": None, "count": 0}
_scheduler_running = False


def is_within_send_window() -> bool:
    """Returns True if current time is within the allowed send window."""
    now = datetime.now()
    # Check blocked days (Sunday = 6)
    if now.weekday() in SEND_BLOCKED_DAYS:
        return False
    # Check hours
    if now.hour < SEND_WINDOW_START_HOUR or now.hour >= SEND_WINDOW_END_HOUR:
        return False
    return True


def get_daily_count() -> int:
    """Get today's send count, reset if new day."""
    today = datetime.now().date().isoformat()
    if _daily_send_count["date"] != today:
        _daily_send_count["date"] = today
        _daily_send_count["count"] = 0
    return _daily_send_count["count"]


def increment_daily_count():
    today = datetime.now().date().isoformat()
    if _daily_send_count["date"] != today:
        _daily_send_count["date"] = today
        _daily_send_count["count"] = 0
    _daily_send_count["count"] += 1


def process_contact(contact: dict):
    """Process a single contact whose action is due."""
    ghl_id = contact["ghl_contact_id"]
    step = contact.get("sequence_step", 0)
    followup_attempts = contact.get("followup_attempts", 0)
    test_mode = bool(contact.get("test_mode"))
    status = contact.get("status", "active")

    if status != "active":
        logger.info(f"[SCHEDULER] Skipping {ghl_id} — status={status}")
        set_next_action(ghl_id, None)
        return

    # RACE CONDITION GUARD: If contact replied after last outbound, skip this tick.
    # The webhook already cleared next_action_at, but scheduler may have grabbed contact
    # in the same tick before the webhook ran. This is the definitive check.
    last_inbound = contact.get("last_inbound_at") or 0
    last_outbound = contact.get("last_outbound_at") or 0
    if last_inbound > last_outbound and step > 0:
        logger.info(f"[SCHEDULER] {ghl_id} replied after last outbound — skipping follow-up tick")
        set_next_action(ghl_id, None)
        return

    logger.info(f"[SCHEDULER] Processing {ghl_id} | step={step} | attempts={followup_attempts}")

    try:
        if step == 0:
            # New contact — send opener
            send_opener(ghl_id, test_mode=test_mode)
            increment_daily_count()

        elif step == 1:
            # Opener sent, no reply yet — send no-reply follow-ups
            if followup_attempts < 4:
                send_followup(ghl_id, attempt_number=followup_attempts + 1, test_mode=test_mode)
                increment_daily_count()
            else:
                update_contact_status(ghl_id, "lost")
                set_next_action(ghl_id, None)
                logger.info(f"[SCHEDULER] {ghl_id} marked LOST after max follow-ups")

        elif step == 3:
            # Qualifier confirmed — send AI curiosity plant
            _send_ai_curiosity(contact, test_mode=test_mode)

        elif step == 5:
            # AI nurture sent — send soft close
            _send_soft_close(contact, test_mode=test_mode)

        else:
            # Steps 2, 4, 6+ — waiting for reply, nothing to schedule-send
            # next_action_at should have been cleared by inbound handler
            logger.info(f"[SCHEDULER] {ghl_id} step={step} — waiting for reply, clearing next_action_at")
            set_next_action(ghl_id, None)

    except Exception as e:
        logger.error(f"[SCHEDULER] Error processing {ghl_id}: {e}")


def run_scheduler_tick():
    """Single scheduler tick — check for due contacts and process them."""
    due_contacts = get_contacts_due_for_action()
    if not due_contacts:
        return

    # Separate test contacts (bypass send window + daily cap) from live contacts
    test_contacts = [c for c in due_contacts if c.get("test_mode")]
    live_contacts = [c for c in due_contacts if not c.get("test_mode")]

    # Always process test contacts immediately
    for contact in test_contacts:
        logger.info(f"[SCHEDULER] [TEST] Processing {contact['ghl_contact_id']}")
        process_contact(contact)
        time.sleep(1)

    # For live contacts, enforce send window and daily cap
    if not live_contacts:
        return

    if not is_within_send_window():
        logger.debug("[SCHEDULER] Outside send window — skipping live contacts")
        return

    daily_count = get_daily_count()
    if daily_count >= MAX_NEW_CONTACTS_PER_DAY:
        logger.info(f"[SCHEDULER] Daily cap reached ({daily_count}) — skipping live contacts")
        return

    logger.info(f"[SCHEDULER] {len(live_contacts)} live contacts due for action")

    for contact in live_contacts:
        if get_daily_count() >= MAX_NEW_CONTACTS_PER_DAY:
            logger.info("[SCHEDULER] Daily cap hit mid-batch — stopping")
            break
        process_contact(contact)
        time.sleep(2)  # Small delay between contacts to avoid rate limits


def start_scheduler(interval_seconds: int = 60):
    """Start the scheduler loop in a background thread."""
    global _scheduler_running
    _scheduler_running = True

    def loop():
        logger.info(f"[SCHEDULER] Started — polling every {interval_seconds}s")
        while _scheduler_running:
            try:
                run_scheduler_tick()
            except Exception as e:
                logger.error(f"[SCHEDULER] Tick error: {e}")
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def stop_scheduler():
    global _scheduler_running
    _scheduler_running = False
    logger.info("[SCHEDULER] Stopped")


def enqueue_contact(ghl_contact_id: str, first_name: str = None,
                     phone: str = None, test_mode: bool = False,
                     delay_seconds: int = 0):
    """
    Add a new contact to the send queue.
    delay_seconds: how long to wait before sending the opener (0 = send in next tick)
    """
    from database import upsert_contact
    from config import GHL_LOCATION_ID

    contact = upsert_contact(
        ghl_contact_id=ghl_contact_id,
        phone=phone or "",
        first_name=first_name,
        location_id=GHL_LOCATION_ID,
        test_mode=test_mode
    )

    next_action = time.time() + delay_seconds
    set_next_action(ghl_contact_id, next_action)
    logger.info(f"[SCHEDULER] Enqueued {ghl_contact_id} (test={test_mode}, delay={delay_seconds}s)")
    return contact


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    initialize_variants()
    print("[SCHEDULER] Running single tick test...")
    print(f"Send window open: {is_within_send_window()}")
    print(f"Daily count: {get_daily_count()}")
    due = get_contacts_due_for_action()
    print(f"Contacts due: {len(due)}")
