# SMS Reactivation Bot — Build Log

## Project Overview
Autonomous AI SMS outreach bot for Increase ROAS.
Runs a 5,000-contact reactivation campaign targeting tree service owners.
Fully autonomous — no human in the loop until a call is booked.

## Stack
- **Language:** Python 3.11
- **Agent Framework:** LangGraph (self-hosted, MIT license)
- **Memory:** Mem0 (Starter plan) → migrate to self-hosted pgvector after Day 10
- **Database:** SQLite (local, upgradeable to PostgreSQL)
- **Task Queue:** Celery + Redis
- **LLM:** GPT-4.1 mini (OpenAI API)
- **Intent Classifier:** GPT-4.1 nano (ultra-cheap, runs before every LLM call)
- **Messaging:** GHL via n8n bridge → `https://n8n-ghl-agent.onrender.com/webhook/manus-ghl-bridge`
- **Webhook Server:** FastAPI
- **Optimizer:** Thompson Sampling bandit (self-built)
- **Hosting:** Manus Cloud Computer (after testing)
- **Repo:** https://github.com/increaseroasir/sms-reactivation-bot

## Key Config
- **Location ID:** DGJ1WPh3wDrNxm6D47Gl
- **GHL Bridge URL:** https://n8n-ghl-agent.onrender.com/webhook/manus-ghl-bridge
- **Bearer Token:** Manus-GHL-20261998313-Secure!
- **Bot Persona Name:** Alex
- **Agency:** Increase ROAS

## Hard Rules (never violate)
- No em dashes (—) or en dashes (–) in any message
- No capital letters to start casual messages
- No exclamation points
- No dashes (-) at end of sentences
- Opt-out check runs BEFORE the LLM sees any message
- Bot never confirms a booking without calling create_appointment first
- Bot never gives a price over SMS
- No "I'm an AI" or "As an AI" ever

## Build Status

### COMPLETED
- [x] GitHub repo created: `increaseroasir/sms-reactivation-bot`
- [x] GHL bridge connection verified — live, pulling real contacts
- [x] Project folder structure created
- [x] BUILDLOG.md created

### IN PROGRESS
- [ ] .env file (credentials)
- [ ] config.py (settings)
- [ ] database.py (State DB schema)
- [ ] ghl_client.py (bridge wrapper)
- [ ] playbook.json (message templates + objection handlers)
- [ ] intent_classifier.py
- [ ] output_validator.py
- [ ] memory.py (Mem0)
- [ ] agent.py (LangGraph brain)
- [ ] scheduler.py (Celery)
- [ ] optimizer.py (Thompson Sampling)
- [ ] webhook_server.py (FastAPI)
- [ ] run.py (entry point)

### PENDING (Phase 2 after core works)
- [ ] Test mode with 5-second delay collapse
- [ ] Real-time log dashboard
- [ ] pgvector migration from Mem0
- [ ] MMS / image attachment support

## Decisions Made
- Use SQLite for State DB (simple, zero cost, upgradeable)
- Use Mem0 Starter ($19/mo) for memory during testing
- Stagger sends at 200/day max to start
- Send window: Mon-Fri 9am-6pm local time only
- No sends on Sunday
- 5 follow-up attempts before marking Lost
- Score threshold of 5 points before bot can send booking link
- Test mode flag: `test_mode = true` in DB or `test_contact` tag in GHL

## Sequence Overview
- Day 1: Opener (2 messages split)
- Day 1 reply: Qualifier ("do you still have the tree company")
- Day 1 qualified: AI curiosity plant ("have you implemented ai into your biz")
- Day 2: Follow-up if no reply
- Day 3: Second follow-up
- Day 5: Third follow-up
- Day 8: Fourth follow-up ("i'll leave you alone after this")
- Day 10: Final ("closing your file out")
- No reply x5: Mark LOST

## File Structure
```
sms-reactivation-bot/
├── BUILDLOG.md
├── .env                  (gitignored)
├── config.py
├── requirements.txt
├── database.py
├── ghl_client.py
├── intent_classifier.py
├── output_validator.py
├── memory.py
├── agent.py
├── scheduler.py
├── optimizer.py
├── webhook_server.py
├── playbook.json
├── run.py
└── logs/
```
