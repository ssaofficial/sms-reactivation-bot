"""
agent.py — LangGraph Agent Brain
The core state machine. Handles all conversation logic, persona enforcement,
sequence routing, memory injection, and response generation.
"""

import json
import random
import logging
import time
from datetime import datetime
from typing import TypedDict, Annotated, Optional
from openai import OpenAI

from config import (
    MAIN_MODEL, OPENAI_API_KEY,
    SCORE_TO_BOOK_CALL, SCORE_WEIGHTS,
    SEQUENCE_DELAYS, TEST_MODE_DELAY, TEST_FOLLOWUP_DELAY,
    BUBBLE_DELAY_MIN, BUBBLE_DELAY_MAX
)
from database import (
    get_contact, update_contact_status, update_contact_score,
    advance_sequence_step, set_next_action, increment_followup_attempts,
    log_message, get_recent_messages, get_facts, save_fact,
    lock_contact, unlock_contact
)
from ghl_client import send_sms_bubbles, add_tag, set_contact_dnd
from output_validator import validate_bubbles, check_opt_out
from intent_classifier import classify_intent, is_positive_intent, is_objection, is_terminal
from playbook_loader import (
    get_persona, get_sequence, get_objection_handlers,
    get_step_variants, get_followup_bubbles, get_tone_rules,
    get_humor_library, get_score_events
)
from optimizer import select_variant, record_result

logger = logging.getLogger(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)


# ─── IDENTITY CARD (injected at top of every LLM call) ───────────────────────

def build_identity_card(contact: dict) -> str:
    persona = get_persona()
    first_name = contact.get("first_name") or "there"
    facts = get_facts(contact["ghl_contact_id"])
    facts_str = "\n".join(f"- {f}" for f in facts) if facts else "- No facts stored yet"

    hour = datetime.now().hour
    if hour < 11:
        time_context = persona["context_injections"]["morning"]
    elif hour < 13:
        time_context = persona["context_injections"]["midday"]
    elif hour < 17:
        time_context = persona["context_injections"]["afternoon"]
    else:
        time_context = persona["context_injections"]["evening"]

    tone_rules = "\n".join(f"- {r}" for r in get_tone_rules())

    return f"""IDENTITY (never change this):
You are {persona['name']}, running {persona['agency']}.
{persona['description']}
Current context: {time_context}

TONE RULES (hard rules, never violate):
{tone_rules}

CONTACT FACTS (what you know about this person):
First name: {first_name}
{facts_str}

CRITICAL: You are texting from your personal phone. Keep it real."""


# ─── MAIN AGENT FUNCTIONS ─────────────────────────────────────────────────────

def handle_inbound(ghl_contact_id: str, inbound_text: str) -> bool:
    """
    Process an inbound message from a contact.
    Returns True if handled successfully.
    """
    contact = get_contact(ghl_contact_id)
    if not contact:
        logger.warning(f"[AGENT] Contact not found: {ghl_contact_id}")
        return False

    test_mode = bool(contact.get("test_mode"))

    # STEP 1: Opt-out check — runs before ANYTHING else
    if check_opt_out(inbound_text):
        logger.info(f"[AGENT] Opt-out detected for {ghl_contact_id}")
        _handle_opt_out(contact)
        return True

    # STEP 2: Lock contact to prevent race conditions
    if not lock_contact(ghl_contact_id):
        logger.warning(f"[AGENT] Could not acquire lock for {ghl_contact_id} — skipping")
        return False

    try:
        # STEP 3: Log the inbound message
        log_message(ghl_contact_id, "inbound", inbound_text)

        # STEP 3b: Reset followup_attempts and reactivate if contact was marked lost
        # (can happen if reply arrives mid-send of the final follow-up bubble)
        from database import get_conn
        _conn = get_conn()
        current_status = contact.get("status", "active")
        if current_status in ("lost", "not_interested"):
            _conn.execute(
                "UPDATE contacts SET followup_attempts=0, status='active' WHERE ghl_contact_id=?",
                (ghl_contact_id,)
            )
            logger.info(f"[AGENT] Reactivated {ghl_contact_id} (was {current_status}) — they replied")
        else:
            _conn.execute("UPDATE contacts SET followup_attempts=0 WHERE ghl_contact_id=?", (ghl_contact_id,))
        _conn.commit()
        _conn.close()
        logger.info(f"[AGENT] Reset followup_attempts for {ghl_contact_id} on inbound reply")

        # STEP 4: Get conversation history for context
        history = get_recent_messages(ghl_contact_id, limit=6)

        # STEP 5: Classify intent
        classification = classify_intent(inbound_text, history)
        intent = classification["intent"]
        sentiment = classification["sentiment"]

        logger.info(f"[AGENT] {ghl_contact_id} | intent={intent} | sentiment={sentiment}")

        # STEP 6: Extract and store facts from the message
        _extract_and_store_facts(ghl_contact_id, inbound_text, intent)

        # STEP 7: Update score based on intent
        _update_score(ghl_contact_id, intent, contact)

        # STEP 8: Route to correct handler
        if is_terminal(intent):
            _handle_terminal(contact, intent)
        elif intent == "wants_to_book":
            _handle_booking_request(contact, test_mode)
        elif intent == "no_ai_tools":
            # Treat as objection-style handler but it's actually an opportunity
            _handle_objection(contact, intent, test_mode)
        elif is_objection(intent):
            _handle_objection(contact, intent, test_mode)
        else:
            # Positive or neutral — advance sequence
            _advance_conversation(contact, intent, inbound_text, history, test_mode)

        return True

    finally:
        unlock_contact(ghl_contact_id)


def send_opener(ghl_contact_id: str, test_mode: bool = False):
    """
    Send the initial opener message to a contact.
    Called by the scheduler for new contacts.
    """
    contact = get_contact(ghl_contact_id)
    if not contact:
        return

    first_name = contact.get("first_name") or ""

    # Select variant via Thompson Sampling bandit
    variant = select_variant("step_0_opener")
    bubbles = variant["bubbles"].copy()

    # Inject first name — always lowercase so validator doesn't preserve capital mid-sentence
    first_name_lower = first_name.lower() if first_name else ""
    bubbles = [
        {**b, "text": b["text"].replace("{first_name}", first_name_lower).replace("{First_name}", first_name.capitalize())}
        for b in bubbles
    ]

    # Validate output
    bubbles = validate_bubbles(bubbles)

    # Send
    logger.info(f"[AGENT] Sending opener to {ghl_contact_id} (variant={variant['id']}, test={test_mode})")
    responses = send_sms_bubbles(ghl_contact_id, bubbles, test_mode=test_mode)

    # Log messages
    for bubble, resp in zip(bubbles, responses):
        msg_id = resp.get("messageId") if resp else None
        log_message(ghl_contact_id, "outbound", bubble["text"],
                    variant_id=variant["id"], ghl_message_id=msg_id)

    # Record variant send
    from optimizer import record_send
    record_send(variant["id"])

    # Mark outbound timestamp (used by race condition guard in scheduler)
    from database import set_last_outbound_at
    set_last_outbound_at(ghl_contact_id)

    # Schedule follow-up if no reply
    delay = TEST_FOLLOWUP_DELAY if test_mode else SEQUENCE_DELAYS["no_reply_attempt_1"]
    set_next_action(ghl_contact_id, time.time() + delay)
    advance_sequence_step(ghl_contact_id, time.time() + delay)


def send_followup(ghl_contact_id: str, attempt_number: int, test_mode: bool = False):
    """
    Send a follow-up message when there has been no reply.
    """
    contact = get_contact(ghl_contact_id)
    if not contact:
        return

    followup_keys = ["followup_1", "followup_2", "followup_3", "followup_4_final"]

    if attempt_number > len(followup_keys):
        # Max attempts reached — mark lost
        logger.info(f"[AGENT] Max follow-ups reached for {ghl_contact_id} — marking LOST")
        update_contact_status(ghl_contact_id, "lost")
        add_tag(ghl_contact_id, "sms_lost")
        return

    key = followup_keys[attempt_number - 1]
    bubbles = get_followup_bubbles(key)
    bubbles = validate_bubbles(bubbles)

    logger.info(f"[AGENT] Sending follow-up #{attempt_number} to {ghl_contact_id}")
    responses = send_sms_bubbles(ghl_contact_id, bubbles, test_mode=test_mode)

    for bubble, resp in zip(bubbles, responses):
        msg_id = resp.get("messageId") if resp else None
        log_message(ghl_contact_id, "outbound", bubble["text"], ghl_message_id=msg_id)

    from database import set_last_outbound_at
    set_last_outbound_at(ghl_contact_id)

    increment_followup_attempts(ghl_contact_id)

    # Schedule next follow-up or mark lost
    delay_keys = [
        "no_reply_attempt_2", "no_reply_attempt_3",
        "no_reply_attempt_4", "no_reply_attempt_5"
    ]
    if attempt_number < len(followup_keys):
        delay_key = delay_keys[min(attempt_number - 1, len(delay_keys) - 1)]
        delay = TEST_FOLLOWUP_DELAY if test_mode else SEQUENCE_DELAYS[delay_key]
        set_next_action(ghl_contact_id, time.time() + delay)
    else:
        # Final follow-up sent — mark lost after window
        delay = TEST_FOLLOWUP_DELAY if test_mode else SEQUENCE_DELAYS["no_reply_attempt_5"]
        set_next_action(ghl_contact_id, time.time() + delay)
        update_contact_status(ghl_contact_id, "lost")
        add_tag(ghl_contact_id, "sms_lost")


# ─── INTERNAL HANDLERS ────────────────────────────────────────────────────────

def _advance_conversation(contact: dict, intent: str, inbound_text: str,
                           history: list, test_mode: bool):
    """Advance the sequence based on positive/neutral intent."""
    ghl_id = contact["ghl_contact_id"]
    step = contact.get("sequence_step", 0)
    score = contact.get("score", 0)

    # Check if score threshold reached — time to close
    if score >= SCORE_TO_BOOK_CALL:
        _send_soft_close(contact, test_mode)
        return

    # Sequence step map:
    # 0 = new contact (scheduler sends opener)
    # 1 = opener sent, waiting for reply (scheduler sends follow-ups if no reply)
    # 2 = reintro sent, waiting for qualifier reply ("still in tree biz?")
    # 3 = qualifier confirmed — scheduler sends AI curiosity
    # 4 = AI curiosity sent, waiting for reply
    # 5 = scheduler sends soft close / nurture
    # 6+ = LLM contextual responses

    if step <= 1:
        # They replied to opener — send reintro, move to step 2 (wait for qualifier reply)
        _send_reintro(contact, test_mode)
    elif step == 2:
        # They confirmed tree biz — schedule AI curiosity via scheduler, advance to step 3
        delay = TEST_MODE_DELAY if test_mode else SEQUENCE_DELAYS["qualifier_delay"]
        from database import get_conn, set_last_outbound_at
        set_last_outbound_at(ghl_id)  # prevent race condition guard from blocking scheduler
        conn = get_conn()
        conn.execute("UPDATE contacts SET sequence_step=3 WHERE ghl_contact_id=?", (ghl_id,))
        conn.commit()
        conn.close()
        set_next_action(ghl_id, time.time() + delay)
    elif step == 4:
        # They replied to AI curiosity — send nurture, advance to step 5
        _send_ai_nurture(contact, inbound_text, history, test_mode)
    elif step == 5:
        # They replied after AI pitch (no_ai_tools handler) or nurture — send soft close
        _send_soft_close(contact, test_mode)
    else:
        # Steps 6+ — LLM contextual response
        _send_contextual_response(contact, inbound_text, history, test_mode)


def _send_reintro(contact: dict, test_mode: bool):
    """Send the reintro + qualifier after opener reply. Advances to step 2 (wait for qualifier reply)."""
    ghl_id = contact["ghl_contact_id"]
    variant = select_variant("step_1_reintro")
    bubbles = validate_bubbles(variant["bubbles"])

    responses = send_sms_bubbles(ghl_id, bubbles, test_mode=test_mode)
    for bubble, resp in zip(bubbles, responses):
        log_message(ghl_id, "outbound", bubble["text"], variant_id=variant["id"])

    from optimizer import record_send
    record_send(variant["id"])
    from database import set_last_outbound_at, get_conn
    set_last_outbound_at(ghl_id)

    # Advance to step 2 and CLEAR next_action_at — wait for their reply, no scheduler action
    conn = get_conn()
    conn.execute("UPDATE contacts SET sequence_step=2, next_action_at=NULL WHERE ghl_contact_id=?", (ghl_id,))
    conn.commit()
    conn.close()


def _send_ai_curiosity(contact: dict, test_mode: bool):
    """Send the AI curiosity plant message. Advances to step 4 (wait for reply)."""
    ghl_id = contact["ghl_contact_id"]
    variant = select_variant("step_2_ai_curiosity")
    bubbles = validate_bubbles(variant["bubbles"])

    responses = send_sms_bubbles(ghl_id, bubbles, test_mode=test_mode)
    for bubble, resp in zip(bubbles, responses):
        log_message(ghl_id, "outbound", bubble["text"], variant_id=variant["id"])

    from optimizer import record_send
    record_send(variant["id"])
    from database import set_last_outbound_at, get_conn
    set_last_outbound_at(ghl_id)

    # Advance to step 4 and CLEAR next_action_at — wait for their reply
    conn = get_conn()
    conn.execute("UPDATE contacts SET sequence_step=4, next_action_at=NULL WHERE ghl_contact_id=?", (ghl_id,))
    conn.commit()
    conn.close()


def _send_ai_nurture(contact: dict, inbound_text: str, history: list, test_mode: bool):
    """Generate a contextual nurture response using the LLM."""
    ghl_id = contact["ghl_contact_id"]

    # Select nurture variant
    variant = select_variant("step_3_nurture_day2")
    bubbles = validate_bubbles(variant["bubbles"])

    responses = send_sms_bubbles(ghl_id, bubbles, test_mode=test_mode)
    for bubble, resp in zip(bubbles, responses):
        log_message(ghl_id, "outbound", bubble["text"], variant_id=variant["id"])

    from optimizer import record_send
    record_send(variant["id"])
    from database import set_last_outbound_at, get_conn
    set_last_outbound_at(ghl_id)

    # Advance to step 5 and schedule soft close via scheduler
    delay = TEST_MODE_DELAY if test_mode else SEQUENCE_DELAYS["next_day_followup"]
    conn = get_conn()
    conn.execute("UPDATE contacts SET sequence_step=5 WHERE ghl_contact_id=?", (ghl_id,))
    conn.commit()
    conn.close()
    set_next_action(ghl_id, time.time() + delay)


def _send_soft_close(contact: dict, test_mode: bool):
    """Send the soft close / booking ask. Advances to step 6 (wait for reply)."""
    ghl_id = contact["ghl_contact_id"]
    variant = select_variant("step_4_soft_close")
    bubbles = validate_bubbles(variant["bubbles"])

    responses = send_sms_bubbles(ghl_id, bubbles, test_mode=test_mode)
    for bubble, resp in zip(bubbles, responses):
        log_message(ghl_id, "outbound", bubble["text"], variant_id=variant["id"])

    update_contact_status(ghl_id, "qualified")
    add_tag(ghl_id, "sms_qualified")

    from optimizer import record_send
    record_send(variant["id"])
    from database import set_last_outbound_at, get_conn
    set_last_outbound_at(ghl_id)

    # Advance to step 6 and CLEAR next_action_at — wait for their reply
    conn = get_conn()
    conn.execute("UPDATE contacts SET sequence_step=6, next_action_at=NULL WHERE ghl_contact_id=?", (ghl_id,))
    conn.commit()
    conn.close()


def _send_contextual_response(contact: dict, inbound_text: str,
                               history: list, test_mode: bool):
    """Use LLM to generate a contextual response for later-stage conversations."""
    ghl_id = contact["ghl_contact_id"]
    identity_card = build_identity_card(contact)

    # Build conversation history for LLM
    history_text = "\n".join([
        f"{'You' if m['direction'] == 'outbound' else 'Lead'}: {m['body']}"
        for m in history[-6:]
    ])

    system_prompt = f"""{identity_card}

You are continuing a text conversation with a tree service business owner.
Your goal is to keep them engaged and move toward booking a strategy call.
Do NOT offer to book the call yet unless their score is high.

Respond with a JSON object:
{{
  "bubbles": [
    {{"text": "message text", "delay_seconds": 0}},
    {{"text": "second bubble if needed", "delay_seconds": 10}}
  ]
}}

Rules:
- 1 to 3 bubbles maximum
- Each bubble is one short thought
- No em dashes, no en dashes, no exclamation points
- Lowercase to start
- Sound like a real person texting
- Ask one question to keep the conversation going"""

    user_prompt = f"""Conversation so far:
{history_text}

Latest message from lead: "{inbound_text}"

Generate your response:"""

    try:
        response = client.chat.completions.create(
            model=MAIN_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=300,
            response_format={"type": "json_object"}
        )
        result = json.loads(response.choices[0].message.content)
        bubbles = validate_bubbles(result.get("bubbles", []))

        if bubbles:
            responses = send_sms_bubbles(ghl_id, bubbles, test_mode=test_mode)
            for bubble, resp in zip(bubbles, responses):
                log_message(ghl_id, "outbound", bubble["text"])
            from database import set_last_outbound_at
            set_last_outbound_at(ghl_id)

    except Exception as e:
        logger.error(f"[AGENT] LLM contextual response error: {e}")


def _handle_objection(contact: dict, intent: str, test_mode: bool):
    """Handle a specific objection using the playbook."""
    ghl_id = contact["ghl_contact_id"]

    # Map intent to objection handler key
    intent_to_key = {
        "objection_tried_ads": "tried_ads_before",
        "objection_too_busy": "too_busy",
        "objection_cost": "how_much_does_it_cost",
        "objection_has_marketing": "already_have_marketing",
        "objection_fully_booked": "fully_booked",
        "objection_are_you_bot": "are_you_a_bot",
        "objection_send_info": "send_me_info",
        "no_ai_tools": "no_ai_tools",
    }

    handler_key = intent_to_key.get(intent)
    if not handler_key:
        return

    handlers = get_objection_handlers()
    handler = handlers.get(handler_key, {})
    bubbles = handler.get("bubbles", [])

    if not bubbles:
        return

    bubbles = validate_bubbles(bubbles)
    responses = send_sms_bubbles(ghl_id, bubbles, test_mode=test_mode)
    for bubble, resp in zip(bubbles, responses):
        log_message(ghl_id, "outbound", bubble["text"])

    from database import set_last_outbound_at, get_conn
    set_last_outbound_at(ghl_id)

    # Add objection tag in GHL
    add_tag(ghl_id, f"objection_{handler_key}")

    # For no_ai_tools: advance to step 5 and clear next_action_at
    # so the next reply goes to _advance_conversation -> soft close, not re-pitch
    if intent == "no_ai_tools":
        conn = get_conn()
        conn.execute("UPDATE contacts SET sequence_step=5, next_action_at=NULL WHERE ghl_contact_id=?", (ghl_id,))
        conn.commit()
        conn.close()
    else:
        # For other objections: keep sequence alive, schedule next step
        delay = TEST_MODE_DELAY if test_mode else SEQUENCE_DELAYS["ai_curiosity_delay"]
        set_next_action(ghl_id, time.time() + delay)


def _handle_terminal(contact: dict, intent: str):
    """Handle terminal intents — end the sequence."""
    ghl_id = contact["ghl_contact_id"]

    status_map = {
        "not_interested": "not_interested",
        "angry_or_hostile": "not_interested",
        "confirmed_no": "lost",
        "no_longer_tree": "lost",
        "wrong_number": "invalid",
        "already_client": "lost",
    }

    status = status_map.get(intent, "lost")
    update_contact_status(ghl_id, status)
    add_tag(ghl_id, f"sms_{status}")
    set_next_action(ghl_id, None)

    # Send closing message for not_interested
    if intent == "not_interested":
        handlers = get_objection_handlers()
        handler = handlers.get("not_interested", {})
        bubbles = validate_bubbles(handler.get("bubbles", []))
        if bubbles:
            send_sms_bubbles(ghl_id, bubbles, test_mode=False)
            for bubble in bubbles:
                log_message(ghl_id, "outbound", bubble["text"])


def _handle_opt_out(contact: dict):
    """Handle TCPA opt-out — immediate, no LLM involved."""
    ghl_id = contact["ghl_contact_id"]
    update_contact_status(ghl_id, "opted_out")
    set_contact_dnd(ghl_id, True)
    add_tag(ghl_id, "opted_out")
    set_next_action(ghl_id, None)
    logger.info(f"[AGENT] Contact {ghl_id} opted out — DND set, sequence stopped")


def _handle_booking_request(contact: dict, test_mode: bool):
    """Contact wants to book — send calendar link."""
    ghl_id = contact["ghl_contact_id"]
    bubbles = [
        {"text": "perfect", "delay_seconds": 0},
        {"text": "grab a time here that works for you", "delay_seconds": 9},
        {"text": "https://increaseroas.ai/book", "delay_seconds": 6}
    ]
    bubbles = validate_bubbles(bubbles)
    send_sms_bubbles(ghl_id, bubbles, test_mode=test_mode)
    for bubble in bubbles:
        log_message(ghl_id, "outbound", bubble["text"])

    update_contact_status(ghl_id, "booked")
    add_tag(ghl_id, "sms_call_booked")


def _update_score(ghl_contact_id: str, intent: str, contact: dict):
    """Update contact score based on intent."""
    score_events = get_score_events()
    delta = 0

    if intent == "confirmed_yes":
        delta = score_events.get("confirmed_tree_biz", 2)
    elif intent in ("positive_curious", "question_about_service"):
        delta = score_events.get("expressed_ai_curiosity", 2)
    elif intent == "neutral_reply":
        delta = score_events.get("replied_to_opener", 1)
    elif intent == "wants_to_book":
        delta = 3

    if delta > 0:
        update_contact_score(ghl_contact_id, delta)


def _extract_and_store_facts(ghl_contact_id: str, message: str, intent: str):
    """Extract key facts from inbound message and store locally."""
    # Simple rule-based extraction (no LLM cost)
    facts = []

    if intent == "confirmed_yes":
        facts.append("confirmed: still runs tree service company")
    elif intent == "confirmed_no" or intent == "no_longer_tree":
        facts.append("no longer in tree business")
    elif intent == "objection_fully_booked":
        facts.append("currently fully booked with work")
    elif intent == "objection_tried_ads":
        facts.append("has tried paid ads before — had bad experience")
    elif intent == "objection_has_marketing":
        facts.append("already has a marketing person or agency")

    # Revenue signal
    import re
    revenue_match = re.search(r'\$?([\d,]+)k?', message, re.IGNORECASE)
    if revenue_match:
        facts.append(f"mentioned revenue figure: {revenue_match.group(0)}")

    for fact in facts:
        save_fact(ghl_contact_id, fact)
