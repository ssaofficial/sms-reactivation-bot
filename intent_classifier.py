"""
intent_classifier.py — Classifies every inbound message before the main LLM.
Uses GPT-4.1-nano (ultra-cheap). Returns a structured intent label.
Runs AFTER opt-out check, BEFORE the agent brain.
"""

import json
import logging
from openai import OpenAI
from config import CLASSIFIER_MODEL, OPENAI_API_KEY

logger = logging.getLogger(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# All possible intent labels
INTENT_LABELS = [
    "confirmed_yes",          # Confirmed they still run tree biz / said yes
    "confirmed_no",           # No longer in business / sold / retired
    "positive_curious",       # Expressed interest or curiosity (not full yes)
    "no_ai_tools",            # Says they have no AI tools / haven't used AI (opportunity signal)
    "objection_tried_ads",    # Tried ads before and it didn't work
    "objection_too_busy",     # Too busy right now
    "objection_cost",         # Asking about price / cost
    "objection_has_marketing",# Already has a marketing person/agency
    "objection_fully_booked", # Already fully booked / too much work
    "objection_are_you_bot",  # Asking if this is a bot or automated
    "objection_send_info",    # Asking to send info/email instead of call
    "not_interested",         # Explicitly not interested — ONLY use when they say stop/no thanks/leave me alone
    "wants_to_book",          # Ready to book a call
    "question_about_service", # Asking a specific question about what you do
    "neutral_reply",          # Replied but unclear intent — continue sequence
    "angry_or_hostile",       # Angry, aggressive, or rude
    "wrong_number",           # Says wrong number or doesn't know who you are
    "already_client",         # Says they are already a client
    "no_longer_tree",         # No longer in tree business
]


CLASSIFIER_SYSTEM_PROMPT = """You are an intent classifier for an AI SMS outreach bot.
Your job is to classify inbound SMS messages from tree service business owners into one of the predefined intent labels.

Return ONLY a valid JSON object with these fields:
{
  "intent": "<one of the intent labels>",
  "confidence": <0.0 to 1.0>,
  "sentiment": "positive" | "neutral" | "negative",
  "key_signal": "<the specific word or phrase that drove your classification>"
}

Intent labels:
- confirmed_yes: person confirmed they still run tree biz or said yes to a question
- confirmed_no: person said they are no longer in business
- positive_curious: interested or curious but not fully committed
- no_ai_tools: person says they have NO AI tools, haven't used AI, or says 'nope/no/not really' when asked about AI usage. This is an OPPORTUNITY, not a rejection. Use this when the bot just asked about AI tools.
- objection_tried_ads: mentions trying ads before that didn't work
- objection_too_busy: says they are too busy
- objection_cost: asking about price or cost
- objection_has_marketing: says they already have marketing help
- objection_fully_booked: says they are fully booked or overwhelmed with work
- objection_are_you_bot: asking if this is a bot or automated
- objection_send_info: wants info sent to them instead of a call
- not_interested: ONLY use when person explicitly says 'not interested', 'stop texting', 'leave me alone', 'no thanks', or similar opt-out language. A simple 'no' or 'nope' in response to a question is NOT not_interested.
- wants_to_book: ready to schedule a call
- question_about_service: asking what you do or how it works
- neutral_reply: replied but intent is unclear
- angry_or_hostile: angry, aggressive, or rude
- wrong_number: says wrong number
- already_client: says they are already working with you
- no_longer_tree: no longer in tree business

CRITICAL: Context matters. If the bot just asked 'are you using any AI tools?' and the person says 'nope', 'no', 'not really', 'haven't', that is no_ai_tools NOT not_interested.
Be decisive. Pick the single best label. Never return multiple labels."""


def classify_intent(message: str, conversation_history: list = None) -> dict:
    """
    Classify the intent of an inbound message.
    
    Args:
        message: The inbound SMS text
        conversation_history: Optional list of recent messages for context
            [{"direction": "outbound"|"inbound", "body": "..."}]
    
    Returns:
        dict with keys: intent, confidence, sentiment, key_signal
    """
    # Build context string from history
    context = ""
    if conversation_history:
        lines = []
        for msg in conversation_history[-4:]:  # Last 4 messages for context
            prefix = "Bot" if msg["direction"] == "outbound" else "Lead"
            lines.append(f"{prefix}: {msg['body']}")
        context = "\nConversation context:\n" + "\n".join(lines) + "\n"

    user_prompt = f"{context}\nNew inbound message to classify:\n\"{message}\""

    try:
        response = client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1,
            max_tokens=150,
            response_format={"type": "json_object"}
        )
        result = json.loads(response.choices[0].message.content)
        logger.info(f"[CLASSIFIER] '{message[:40]}' -> {result.get('intent')} ({result.get('confidence')})")
        return result

    except Exception as e:
        logger.error(f"[CLASSIFIER] Error classifying message: {e}")
        # Safe fallback — treat as neutral
        return {
            "intent": "neutral_reply",
            "confidence": 0.5,
            "sentiment": "neutral",
            "key_signal": "classifier_error"
        }


def is_positive_intent(intent: str) -> bool:
    """Returns True if the intent should advance the sequence positively."""
    positive_intents = {
        "confirmed_yes",
        "positive_curious",
        "no_ai_tools",
        "wants_to_book",
        "question_about_service",
        "neutral_reply"
    }
    return intent in positive_intents


def is_objection(intent: str) -> bool:
    """Returns True if the intent is an objection that needs handling."""
    return intent.startswith("objection_")


def is_terminal(intent: str) -> bool:
    """Returns True if the intent ends the sequence."""
    terminal_intents = {
        "not_interested",
        "angry_or_hostile",
        "confirmed_no",
        "no_longer_tree",
        "already_client",
        "wrong_number"
    }
    return intent in terminal_intents


if __name__ == "__main__":
    # Test classifier
    test_messages = [
        "yeah still got the company",
        "not interested",
        "how much does it cost",
        "are you a bot",
        "too busy right now",
        "yeah i tried facebook ads before total waste",
        "sure i am open to a call",
        "wrong number",
        "i already have someone doing my marketing",
        "what exactly do you do",
    ]
    for msg in test_messages:
        result = classify_intent(msg)
        print(f"'{msg}' -> {result['intent']} | {result['sentiment']} | signal: {result['key_signal']}")
