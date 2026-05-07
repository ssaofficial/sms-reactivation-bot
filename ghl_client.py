"""
ghl_client.py — GHL Bridge Wrapper
All GHL interactions go through this module.
Routes via: https://n8n-ghl-agent.onrender.com/webhook/manus-ghl-bridge
"""

import requests
import time
import random
import logging
from config import GHL_BRIDGE_URL, GHL_BEARER_TOKEN, GHL_LOCATION_ID

logger = logging.getLogger(__name__)


def _call(tool: str, parameters: dict, location_id: str = None) -> dict:
    """
    Core bridge call. Returns parsed result or raises on failure.
    Retries up to 3 times on network errors.
    """
    loc = location_id or GHL_LOCATION_ID
    payload = {
        "locationId": loc,
        "tool": tool,
        "parameters": parameters
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GHL_BEARER_TOKEN}"
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                GHL_BRIDGE_URL,
                json=payload,
                headers=headers,
                timeout=60
            )
            resp.raise_for_status()

            # Bridge returns: [{"result": {"content": [{"type": "text", "text": "...json..."}]}}]
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                content = data[0].get("result", {}).get("content", [])
                if content and content[0].get("type") == "text":
                    import json
                    return json.loads(content[0]["text"])
            return {}

        except requests.exceptions.RequestException as e:
            logger.warning(f"[GHL] Attempt {attempt+1} failed for tool={tool}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise

    return {}


# ─── CONTACTS ────────────────────────────────────────────────────────────────

def get_contact(contact_id: str) -> dict:
    return _call("get_contact", {"contactId": contact_id})


def search_contacts(query: str = None, tags: list = None, limit: int = 20) -> list:
    params = {"limit": limit}
    if query:
        params["query"] = query
    if tags:
        params["tags"] = tags
    result = _call("search_contacts", params)
    return result.get("contacts", [])


def update_contact_tags(contact_id: str, tags: list):
    return _call("update_contact", {
        "contactId": contact_id,
        "tags": tags
    })


def update_contact_field(contact_id: str, **fields):
    params = {"contactId": contact_id}
    params.update(fields)
    return _call("update_contact", params)


def set_contact_dnd(contact_id: str, dnd: bool = True):
    """Mark contact as Do Not Disturb (opt-out)."""
    return _call("update_contact", {
        "contactId": contact_id,
        "dnd": dnd
    })


# ─── CONVERSATIONS & SMS ─────────────────────────────────────────────────────

def get_or_create_conversation(contact_id: str) -> str:
    """Get existing conversation ID or create one. Returns conversationId."""
    result = _call("get_conversation", {"contactId": contact_id})
    if result.get("id"):
        return result["id"]
    # Create new conversation
    created = _call("create_conversation", {"contactId": contact_id})
    return created.get("id") or created.get("conversationId")


def send_sms(contact_id: str, message: str, conversation_id: str = None) -> dict:
    """
    Send a single SMS message to a contact.
    Returns GHL response including messageId.
    """
    if not conversation_id:
        conversation_id = get_or_create_conversation(contact_id)

    return _call("send_message", {
        "type": "SMS",
        "contactId": contact_id,
        "conversationId": conversation_id,
        "message": message
    })


def send_sms_bubbles(contact_id: str, bubbles: list, test_mode: bool = False) -> list:
    """
    Send multiple SMS messages with human-like delays between them.

    bubbles: list of dicts with keys:
        - text: str
        - delay_seconds: int (delay BEFORE sending this bubble)

    Returns list of GHL responses.
    """
    from config import BUBBLE_DELAY_MIN, BUBBLE_DELAY_MAX, TEST_BUBBLE_DELAY

    conversation_id = get_or_create_conversation(contact_id)
    responses = []

    for i, bubble in enumerate(bubbles):
        delay = bubble.get("delay_seconds", 0)

        if i > 0:  # First bubble sends immediately
            if test_mode:
                actual_delay = TEST_BUBBLE_DELAY
            else:
                # Randomize within range for human feel
                actual_delay = random.randint(BUBBLE_DELAY_MIN, BUBBLE_DELAY_MAX)
                if delay > 0:
                    actual_delay = delay + random.randint(-2, 2)
                    actual_delay = max(5, actual_delay)

            logger.info(f"[SMS] Waiting {actual_delay}s before bubble {i+1}")
            time.sleep(actual_delay)

        text = bubble["text"]
        logger.info(f"[SMS] Sending bubble {i+1}/{len(bubbles)}: {text[:50]}")
        resp = send_sms(contact_id, text, conversation_id)
        responses.append(resp)

    return responses


def get_conversation_messages(contact_id: str, limit: int = 20) -> list:
    """Fetch recent messages from GHL conversation."""
    conv_id = get_or_create_conversation(contact_id)
    result = _call("get_messages", {
        "conversationId": conv_id,
        "limit": limit
    })
    return result.get("messages", [])


# ─── OPPORTUNITIES ────────────────────────────────────────────────────────────

def update_opportunity_status(contact_id: str, status: str, pipeline_stage_id: str = None):
    """
    Update opportunity status for a contact.
    status: open | won | lost | abandoned
    """
    params = {
        "contactId": contact_id,
        "status": status
    }
    if pipeline_stage_id:
        params["pipelineStageId"] = pipeline_stage_id
    return _call("update_opportunity", params)


def create_opportunity(contact_id: str, pipeline_id: str, name: str = "SMS Reactivation"):
    return _call("create_opportunity", {
        "contactId": contact_id,
        "pipelineId": pipeline_id,
        "name": name,
        "status": "open"
    })


# ─── CALENDAR / APPOINTMENTS ─────────────────────────────────────────────────

def get_free_slots(calendar_id: str, start_date: str, end_date: str) -> list:
    """
    Get available appointment slots.
    start_date / end_date: ISO format strings e.g. "2026-05-10"
    """
    result = _call("get_free_slots", {
        "calendarId": calendar_id,
        "startDate": start_date,
        "endDate": end_date
    })
    return result.get("slots", [])


def create_appointment(contact_id: str, calendar_id: str, start_time: str, title: str = "Strategy Call") -> dict:
    """
    Book an appointment. start_time: ISO datetime string.
    Bot MUST call this before confirming a booking to the lead.
    """
    return _call("create_appointment", {
        "contactId": contact_id,
        "calendarId": calendar_id,
        "title": title,
        "startTime": start_time
    })


# ─── TAGS ─────────────────────────────────────────────────────────────────────

def add_tag(contact_id: str, tag: str):
    contact = get_contact(contact_id)
    existing_tags = contact.get("tags", [])
    if tag not in existing_tags:
        existing_tags.append(tag)
        update_contact_tags(contact_id, existing_tags)


def remove_tag(contact_id: str, tag: str):
    contact = get_contact(contact_id)
    existing_tags = contact.get("tags", [])
    updated = [t for t in existing_tags if t != tag]
    update_contact_tags(contact_id, updated)


def has_tag(contact_id: str, tag: str) -> bool:
    contact = get_contact(contact_id)
    return tag in contact.get("tags", [])


# ─── BLOCKLIST CHECK ─────────────────────────────────────────────────────────

BLOCKLIST_TAGS = [
    "current_client",
    "do_not_contact",
    "past_client_bad",
    "opted_out",
    "dnd",
    "test_contact"  # test_contact is allowed — handled separately
]

def is_blocked(contact_id: str) -> bool:
    """Returns True if contact should never be messaged."""
    contact = get_contact(contact_id)
    if not contact:
        return True
    if contact.get("dnd"):
        return True
    tags = contact.get("tags", [])
    for tag in BLOCKLIST_TAGS:
        if tag in tags and tag != "test_contact":
            return True
    return False


def is_test_contact(contact_id: str) -> bool:
    """Returns True if contact has the test_contact tag."""
    return has_tag(contact_id, "test_contact")


if __name__ == "__main__":
    # Quick connection test
    print("Testing GHL bridge connection...")
    contacts = search_contacts(limit=2)
    print(f"Found {len(contacts)} contacts. Connection OK.")
    if contacts:
        print(f"Sample: {contacts[0].get('contactName')} | {contacts[0].get('phone')}")
