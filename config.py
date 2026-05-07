import os
from dotenv import load_dotenv

load_dotenv()

# GHL Bridge
GHL_BRIDGE_URL = os.getenv("GHL_BRIDGE_URL", "https://n8n-ghl-agent.onrender.com/webhook/manus-ghl-bridge")
GHL_BEARER_TOKEN = os.getenv("GHL_BEARER_TOKEN", "Manus-GHL-20261998313-Secure!")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "DGJ1WPh3wDrNxm6D47Gl")

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MAIN_MODEL = "gpt-4.1-mini"
CLASSIFIER_MODEL = "gpt-4.1-nano"

# Mem0
MEM0_API_KEY = os.getenv("MEM0_API_KEY")

# Bot Persona
BOT_NAME = "Alex"
BOT_AGENCY = "Increase ROAS"
BOT_CONTEXT = "you help tree service companies get more jobs using AI and paid ads"

# Send Window (local time for contact)
SEND_WINDOW_START_HOUR = 9   # 9am
SEND_WINDOW_END_HOUR = 18    # 6pm
SEND_BLOCKED_DAYS = [6]      # 0=Monday, 6=Sunday — block Sunday

# Throttle
MAX_NEW_CONTACTS_PER_DAY = 200

# Sequence timing (in seconds — real mode)
# No-reply follow-up cadence: 12h -> 24h -> 12h -> 24h -> 24h
# Total window before marking LOST: ~4 days
SEQUENCE_DELAYS = {
    "no_reply_attempt_1": 12 * 3600,        # 12 hours after opener
    "no_reply_attempt_2": 24 * 3600,        # 24 hours after attempt 1
    "no_reply_attempt_3": 12 * 3600,        # 12 hours after attempt 2
    "no_reply_attempt_4": 24 * 3600,        # 24 hours after attempt 3
    "no_reply_attempt_5": 24 * 3600,        # 24 hours after attempt 4 (final)
    "qualifier_delay": 45 * 60,             # 45 mins after positive reply
    "ai_curiosity_delay": 45 * 60,          # 45 mins
    "next_day_followup": 20 * 3600,         # ~next day
}

# Test mode timing (in seconds)
TEST_MODE_DELAY = 5              # conversation reply delays (qualifier, AI curiosity, nurture)
TEST_FOLLOWUP_DELAY = 600        # 10 minutes between no-reply follow-ups in test mode

# Scoring thresholds
SCORE_TO_BOOK_CALL = 5
SCORE_WEIGHTS = {
    "replied_to_opener": 1,
    "confirmed_tree_biz": 2,
    "expressed_ai_curiosity": 2,
    "asked_followup_question": 1,
    "replied_day2_or_later": 1,
}

# Max follow-up attempts before marking Lost
MAX_FOLLOWUP_ATTEMPTS = 5

# Message bubble delays (seconds) — randomized within range
BUBBLE_DELAY_MIN = 7
BUBBLE_DELAY_MAX = 15
TEST_BUBBLE_DELAY = 2

# Hard output rules
FORBIDDEN_CHARACTERS = ["—", "–"]   # em dash, en dash
FORBIDDEN_WORDS_IN_SMS = ["never", "now", "only"]  # per existing rules

# Webhook
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", 8000))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Database
# On Render, use the persistent disk mount path; locally use current dir
DB_PATH = os.path.join(os.environ.get("RENDER_DISK_PATH", "."), "sms_bot.db")

# Inbound message batching window (seconds) — prevents double-response on rapid texts
INBOUND_BATCH_WINDOW = 60
