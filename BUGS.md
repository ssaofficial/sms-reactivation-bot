# Bug Tracker — SMS Reactivation Bot

Running log of every bug found, root cause, and fix applied.
All fixes are committed to `main` on GitHub.

---

## BUG-001: Race Condition — Follow-ups firing after reply received
**Status:** Fixed  
**File:** `agent.py`, `webhook_server.py`  
**Root cause:** `set_last_outbound_at()` was only called in `send_opener`, not in the other 6 send functions. The scheduler's race condition guard (`last_inbound > last_outbound`) never triggered because `last_outbound_at` was always stale.  
**Fix:** Added `set_last_outbound_at()` after every `send_sms_bubbles()` call in all 7 send functions.

---

## BUG-002: Stale Bandit Variants Persisting in DB
**Status:** Fixed  
**File:** `optimizer.py`  
**Root cause:** `initialize_variants()` used `INSERT OR IGNORE`, so deleted playbook variants stayed in the DB and kept getting selected by the bandit.  
**Fix:** Delete DB rows not present in current playbook before inserting, so stale variants are purged on every restart.

---

## BUG-003: messageId="null" Idempotency Bug — All replies after first dropped
**Status:** Fixed  
**File:** `webhook_server.py`  
**Root cause:** GHL sends `messageId: "null"` (the literal string "null") for all inbound SMS. The first message was marked processed under the key "null", so every subsequent message with the same "null" ID was silently skipped.  
**Fix:** Treat `messageId="null"` as no ID. Fall back to a body+contact+timestamp fingerprint for dedup. Extended fingerprint window from 3s to 120s to catch delayed GHL double-fires.

---

## BUG-004: Duplicate Bot Instances Running Simultaneously
**Status:** Fixed  
**File:** `run.py`  
**Root cause:** Old bot process from previous session kept running after restart. Both instances processed the same contacts, causing duplicate sends.  
**Fix:** Added startup guard in `run_test_mode` and `run_live_mode` to kill any existing process on port 8000 before starting.

---

## BUG-005: Lost Contact Not Reactivated on Reply
**Status:** Fixed  
**File:** `agent.py`  
**Root cause:** If a contact replied after being marked `lost` (e.g., replied during the final follow-up send), `handle_inbound` would process the reply but the contact status stayed `lost`, blocking future sends.  
**Fix:** Added reactivation logic in `handle_inbound`: if contact is `lost` and a reply comes in, reset status to `active`, clear `next_action_at`, and continue processing.

---

## BUG-006: Follow-up Delays Too Short (5 seconds in test, no production cadence)
**Status:** Fixed  
**File:** `config.py`  
**Root cause:** `TEST_MODE_DELAY` was 5 seconds for everything. Production `SEQUENCE_DELAYS` were not set to the correct cadence.  
**Fix:** Split into `TEST_FOLLOWUP_DELAY` (10 minutes) and `TEST_MODE_DELAY` (5 seconds for conversation steps). Production cadence set to 12h / 24h / 12h / 24h.

---

## BUG-007: Scheduler Follow-up Branch Firing Mid-Conversation
**Status:** Fixed  
**File:** `scheduler.py`  
**Root cause:** `process_contact` only handled `step==0` and `step==3` explicitly. All other steps fell through to `elif followup_attempts < 4` which sent no-reply follow-ups regardless of whether the contact had replied.  
**Fix:** Rewrote `process_contact` with explicit step routing. Follow-ups only fire at `step==1`. Steps 2, 4, 6+ clear `next_action_at` and wait for reply.

---

## BUG-008: "Nope" Classified as not_interested Instead of no_ai_tools
**Status:** Fixed  
**File:** `intent_classifier.py`  
**Root cause:** Classifier had no context awareness. "Nope" in response to "are you using any AI tools?" was classified as `not_interested` (0.95 confidence), triggering the goodbye sequence.  
**Fix:** Added `no_ai_tools` intent label with explicit context rule: if bot just asked about AI tools and prospect says nope/no/not really, classify as `no_ai_tools`. Tightened `not_interested` to require explicit opt-out language only.

---

## BUG-009: no_ai_tools Pitch Repeating on Every Reply
**Status:** Fixed  
**File:** `agent.py`  
**Root cause:** `_handle_objection` sent the AI pitch but did not advance `sequence_step`. Contact stayed at step 4 forever, so every subsequent reply re-triggered the same pitch.  
**Fix:** After `no_ai_tools` pitch fires, advance `sequence_step` to 5 and clear `next_action_at`. Added step 5 branch in `_advance_conversation` to send soft close on next reply.

---

## BUG-010: Score Threshold Bypassing Entire Sequence
**Status:** Fixed  
**File:** `agent.py`  
**Root cause:** `_advance_conversation` checked `if score >= SCORE_TO_BOOK_CALL` at the top, before the step map. If score was high from a previous test run (score not wiped on reset), it jumped straight to soft close at step 2, skipping AI curiosity and the AI pitch entirely.  
**Fix:** Removed global score check from top of `_advance_conversation`. Score check now only applies at step 7+ (after the full sequence has run). Steps 2-6 always follow the explicit step map.

---

## BUG-011: Soft Close Repeating on Every Reply
**Status:** Fixed  
**File:** `agent.py`  
**Root cause:** `_send_soft_close` was advancing to step 6 and clearing `next_action_at`, but `_advance_conversation` had no `step==6` branch. Replies at step 6 fell through to the score check, which re-triggered soft close if score was high.  
**Fix:** Added explicit `step==6` branch in `_advance_conversation` routing to `_send_contextual_response` (LLM). Score check now only applies at step 7+.

---

## BUG-012: LLM Repeating Booking Ask Instead of Answering Product Questions
**Status:** Fixed  
**File:** `agent.py`  
**Root cause:** LLM system prompt told it to "move toward booking a call" with no instruction to answer direct product questions first. When prospect asked "what are you selling?", LLM interpreted it as an opportunity to push the call harder.  
**Fix:** Rewrote LLM system prompt with explicit rule: if prospect asks what you sell or what this is about, answer first (explain the AI system for tree companies), then bridge to call. Added hard guardrails: no link drops unless call agreed, no pricing unless asked, max 2 bubbles.

---

## BUG-013: Score Not Reset on Test Contact Reset
**Status:** Fixed  
**File:** `run.py`  
**Root cause:** `reset_test_contacts()` deleted the contact row but did not wipe messages or processed_message fingerprints. Score accumulated across test runs and polluted the step routing.  
**Fix:** `reset_test_contacts()` now also deletes all messages and processed_message fingerprints for test contacts.

---

## BUG-014: Race Condition Check Using > Instead of >=
**Status:** Fixed  
**File:** `scheduler.py`  
**Root cause:** Race condition guard used `last_inbound > last_outbound`. If inbound and outbound timestamps were identical (same second), the guard did not trigger and a follow-up could fire.  
**Fix:** Changed to `last_inbound >= last_outbound and last_inbound > 0` so same-second inbound always wins.

---

## BUG-015: No Re-engagement After Soft Close Ghost
**Status:** Fixed  
**File:** `scheduler.py`  
**Root cause:** If prospect received the soft close (booking ask) and ghosted, the contact sat at step 6 with `next_action_at=NULL` forever with no follow-up.  
**Fix:** Added step 6 branch in `process_contact`: if no reply after soft close, send one final nudge ("still want me to show you what this looks like for your company?") after 24h, then mark lost.

---

## Known Remaining Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| LLM off-script on highly unusual replies | Low | System prompt guardrails. Monitor logs. |
| GHL webhook URL changes on sandbox restart | High | Deploy to persistent server (VPS/Railway). |
| Dedup window (120s) blocking legitimate rapid replies | Very Low | Fingerprint includes body text, so different messages in same window are processed. |
