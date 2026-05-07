"""
output_validator.py — Hard output rules enforced before every SMS send.
Runs AFTER the LLM generates text, BEFORE it hits send_sms.
No exceptions. No LLM override.
"""

import re
import logging

logger = logging.getLogger(__name__)

# Characters that are NEVER allowed in any outbound message
FORBIDDEN_CHARS = [
    "\u2014",  # em dash —
    "\u2013",  # en dash –
    "\u2012",  # figure dash
    "\u2015",  # horizontal bar
]

# Words that must never appear in outbound messages
FORBIDDEN_WORDS = ["never", "now", "only"]

# Phrases that must never appear
FORBIDDEN_PHRASES = [
    "i'm an ai",
    "i am an ai",
    "as an ai",
    "i'm a bot",
    "i am a bot",
    "artificial intelligence",
    "language model",
    "i hope this message finds you well",
    "hello,",
    "dear ",
    "best regards",
    "kind regards",
    "sincerely,",
]

# Sentences should not end with a dash
TRAILING_DASH_PATTERN = re.compile(r'\s*[-–—]+\s*$', re.MULTILINE)


def validate_and_clean(text: str) -> str:
    """
    Apply all hard rules to a message bubble.
    Returns cleaned text. Raises ValueError if uncorrectable.
    """
    original = text

    # 1. Strip forbidden characters
    for char in FORBIDDEN_CHARS:
        text = text.replace(char, " ")

    # 2. Remove trailing dashes
    text = TRAILING_DASH_PATTERN.sub("", text)

    # 3. Strip forbidden phrases (replace with empty string)
    lower = text.lower()
    for phrase in FORBIDDEN_PHRASES:
        if phrase in lower:
            logger.warning(f"[VALIDATOR] Forbidden phrase detected: '{phrase}' — stripping")
            # Find and remove case-insensitively
            pattern = re.compile(re.escape(phrase), re.IGNORECASE)
            text = pattern.sub("", text)

    # 4. Check for forbidden words (log warning but don't strip — context matters)
    for word in FORBIDDEN_WORDS:
        if re.search(r'\b' + word + r'\b', text, re.IGNORECASE):
            logger.warning(f"[VALIDATOR] Forbidden word detected: '{word}' in: {text[:80]}")
            # Replace with safer alternative
            replacements = {
                "never": "not",
                "now": "these days",
                "only": "just"
            }
            text = re.sub(r'\b' + word + r'\b', replacements.get(word, ""), text, flags=re.IGNORECASE)

    # 5. Clean up double spaces from replacements
    text = re.sub(r'  +', ' ', text).strip()

    # 6. Enforce no trailing punctuation on short casual messages (< 60 chars)
    if len(text) < 60 and text.endswith('.'):
        text = text[:-1]

    # 7. Enforce lowercase first character for casual messages
    # (Only if the message doesn't start with a proper noun — heuristic: if first word is all caps, leave it)
    if text and not text[0].isupper() or (len(text) > 1 and text[1].islower()):
        text = text[0].lower() + text[1:] if text else text

    if text != original:
        logger.info(f"[VALIDATOR] Cleaned: '{original[:60]}' -> '{text[:60]}'")

    return text


def validate_bubbles(bubbles: list) -> list:
    """
    Validate a list of bubble dicts: [{"text": "...", "delay_seconds": N}]
    Returns cleaned list.
    """
    cleaned = []
    for bubble in bubbles:
        text = validate_and_clean(bubble["text"])
        if text:  # Skip empty bubbles after cleaning
            cleaned.append({**bubble, "text": text})
    return cleaned


def check_opt_out(text: str) -> bool:
    """
    Returns True if the inbound message is an opt-out request.
    This runs BEFORE the LLM sees any message. Hard rule.
    """
    from playbook_loader import get_opt_out_keywords
    keywords = get_opt_out_keywords()
    lower = text.lower().strip()
    for keyword in keywords:
        if keyword in lower:
            return True
    # TCPA standard single-word opt-outs
    single_word_optouts = {"stop", "stopall", "unsubscribe", "cancel", "quit"}
    words = set(re.findall(r'\b\w+\b', lower))
    if words & single_word_optouts:
        return True
    return False


if __name__ == "__main__":
    # Test cases
    tests = [
        "Hey — just wanted to follow up",
        "we spoke a while back\u2014do you still have the tree company",
        "I'm an AI assistant here to help",
        "never give up on your business",
        "Hello, how are you today.",
        "just jumped out of a meeting",
        "STOP texting me",
    ]
    for t in tests:
        if check_opt_out(t):
            print(f"OPT-OUT: {t}")
        else:
            cleaned = validate_and_clean(t)
            print(f"IN:  {t}")
            print(f"OUT: {cleaned}")
            print()
