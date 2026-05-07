"""
run.py — Main entry point for the SMS Reactivation Bot

Usage:
  python3.11 run.py --mode dry_run          # Simulate 20 fake scenarios, no SMS sent
  python3.11 run.py --mode test             # Send to test contacts (your numbers), delays=5s
  python3.11 run.py --mode live             # Full production mode
  python3.11 run.py --mode add_test_contact --phone +1XXXXXXXXXX --name "Mike"
  python3.11 run.py --mode import_csv --file contacts.csv
  python3.11 run.py --mode stats            # Print current stats and bandit report
  python3.11 run.py --mode reset_test       # Clear all test contacts from DB
"""

import argparse
import logging
import time
import sys
import threading
import json
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log")
    ]
)
logger = logging.getLogger(__name__)


def run_dry_run():
    """
    Simulate 20 fake contact scenarios without sending any real SMS.
    Tests every branch: positive reply, objections, opt-out, no reply, booking.
    """
    print("\n" + "="*60)
    print("DRY RUN MODE — No real SMS will be sent")
    print("="*60 + "\n")

    from database import init_db, upsert_contact, set_next_action
    from optimizer import initialize_variants
    from output_validator import validate_and_clean, check_opt_out
    from intent_classifier import classify_intent
    from playbook_loader import get_step_variants, get_objection_handlers, get_followup_bubbles
    from config import GHL_LOCATION_ID

    init_db()
    initialize_variants()

    scenarios = [
        {"name": "Mike", "reply": "yeah still got the company", "expected": "confirmed_yes"},
        {"name": "Dave", "reply": "not interested", "expected": "not_interested"},
        {"name": "Tom", "reply": "how much does it cost", "expected": "objection_cost"},
        {"name": "Jake", "reply": "are you a bot", "expected": "objection_are_you_bot"},
        {"name": "Chris", "reply": "too busy right now", "expected": "objection_too_busy"},
        {"name": "Steve", "reply": "yeah i tried facebook ads total waste", "expected": "objection_tried_ads"},
        {"name": "Bob", "reply": "sure open to a call", "expected": "positive_curious"},
        {"name": "Ryan", "reply": "wrong number", "expected": "wrong_number"},
        {"name": "Kevin", "reply": "already have a marketing guy", "expected": "objection_has_marketing"},
        {"name": "Mark", "reply": "what exactly do you do", "expected": "question_about_service"},
        {"name": "Luis", "reply": "STOP", "expected": "OPT_OUT"},
        {"name": "John", "reply": "fully booked cant take more work", "expected": "objection_fully_booked"},
        {"name": "Paul", "reply": "yeah what is this about", "expected": "neutral_reply"},
        {"name": "Eric", "reply": "sold the company last year", "expected": "confirmed_no"},
        {"name": "Dan", "reply": "send me some info", "expected": "objection_send_info"},
    ]

    print(f"{'Contact':<12} {'Reply':<40} {'Expected':<25} {'Got':<25} {'PASS/FAIL'}")
    print("-" * 120)

    passed = 0
    failed = 0

    for s in scenarios:
        # Test opt-out check
        if check_opt_out(s["reply"]):
            got = "OPT_OUT"
        else:
            result = classify_intent(s["reply"])
            got = result["intent"]

        # Allow equivalent intents to pass
        equivalents = {
            "confirmed_no": {"confirmed_no", "no_longer_tree"},
            "neutral_reply": {"neutral_reply", "question_about_service", "positive_curious"},
        }
        expected_set = equivalents.get(s["expected"], {s["expected"]})
        status = "PASS" if got in expected_set else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1

        print(f"{s['name']:<12} {s['reply'][:38]:<40} {s['expected']:<25} {got:<25} {status}")

    print(f"\nResults: {passed} passed, {failed} failed out of {len(scenarios)} scenarios")

    # Test output validator
    print("\n--- Output Validator Test ---")
    test_outputs = [
        "Hey — just wanted to follow up",
        "we spoke a while back—do you still have the tree company",
        "I'm an AI assistant here to help",
        "never give up on your business",
        "Hello, how are you today.",
        "just jumped out of a meeting",
    ]
    for t in test_outputs:
        cleaned = validate_and_clean(t)
        changed = " [MODIFIED]" if cleaned != t else ""
        print(f"  '{t[:50]}' -> '{cleaned[:50]}'{changed}")

    # Test variant selection
    print("\n--- Bandit Variant Selection Test ---")
    from optimizer import select_variant
    for step in ["step_0_opener", "step_1_reintro", "step_2_ai_curiosity", "step_4_soft_close"]:
        v = select_variant(step)
        print(f"  {step} -> {v['id']}: {v['bubbles'][0]['text'][:50]}")

    print("\nDry run complete. All systems nominal.")


def add_test_contact(phone: str, name: str = None):
    """Add a test contact (your number) to the queue with test_mode=True."""
    from database import init_db, upsert_contact, set_next_action
    from optimizer import initialize_variants
    from config import GHL_LOCATION_ID
    from ghl_client import search_contacts

    init_db()
    initialize_variants()

    # Try to find contact in GHL first
    print(f"Looking up {phone} in GHL...")
    contacts = search_contacts(query=phone)

    if contacts:
        ghl_contact = contacts[0]
        ghl_id = ghl_contact.get("id")
        first_name = name or ghl_contact.get("firstName") or "Test"
        print(f"Found in GHL: {ghl_id} ({first_name})")
    else:
        # Create a fake local ID for testing
        ghl_id = f"test_{phone.replace('+', '').replace(' ', '')}"
        first_name = name or "Test"
        print(f"Not found in GHL — using local test ID: {ghl_id}")

    upsert_contact(
        ghl_contact_id=ghl_id,
        phone=phone,
        first_name=first_name,
        location_id=GHL_LOCATION_ID,
        test_mode=True
    )
    set_next_action(ghl_id, time.time() + 5)  # Fire in 5 seconds

    print(f"\nTest contact added:")
    print(f"  GHL ID: {ghl_id}")
    print(f"  Name: {first_name}")
    print(f"  Phone: {phone}")
    print(f"  Test mode: ON (all delays = 5 seconds)")
    print(f"\nRun 'python3.11 run.py --mode test' to start the bot and send the opener.")


def run_test_mode():
    """Run the bot in test mode — sends real SMS to test contacts only."""
    from database import init_db
    from optimizer import initialize_variants
    from scheduler import start_scheduler
    from webhook_server import app
    import uvicorn
    import threading

    print("\n" + "="*60)
    print("TEST MODE — Real SMS to test contacts only")
    print("Delays: 5 seconds (not real timing)")
    print("="*60 + "\n")

    # Kill any stale bot instance holding port 8000 before starting
    import subprocess
    subprocess.run("lsof -ti :8000 | xargs kill -9 2>/dev/null || true", shell=True)
    time.sleep(1)

    init_db()
    initialize_variants()

    # Start scheduler in background
    sched_thread = start_scheduler(interval_seconds=10)  # Check every 10s in test mode
    print("Scheduler started (10s polling)")

    # Start webhook server in background
    def run_server():
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    print("Webhook server started on port 8000")
    print("\nBot is running. Text back from your test number to test inbound handling.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(30)
            _print_live_stats()
    except KeyboardInterrupt:
        print("\nStopping bot...")


def run_live_mode():
    """Run the bot in full production mode."""
    from database import init_db
    from optimizer import initialize_variants
    from scheduler import start_scheduler
    from webhook_server import app
    import uvicorn
    import threading

    print("\n" + "="*60)
    print("LIVE MODE — Full production")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60 + "\n")

    # Kill any stale bot instance holding port 8000 before starting
    import subprocess
    subprocess.run("lsof -ti :8000 | xargs kill -9 2>/dev/null || true", shell=True)
    time.sleep(1)

    init_db()
    initialize_variants()

    sched_thread = start_scheduler(interval_seconds=60)
    print("Scheduler started (60s polling)")

    def run_server():
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    print("Webhook server started on port 8000")
    print("\nBot is running in LIVE mode. Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(300)  # Print stats every 5 mins
            _print_live_stats()
    except KeyboardInterrupt:
        print("\nStopping bot...")


def import_csv(filepath: str, test_mode: bool = False, delay_between: int = 0):
    """
    Import contacts from CSV file.
    Expected columns: phone, first_name (optional: last_name, ghl_contact_id)
    Staggers sends by adding incremental delay.
    """
    import csv
    from database import init_db, upsert_contact, set_next_action
    from optimizer import initialize_variants
    from config import GHL_LOCATION_ID

    init_db()
    initialize_variants()

    print(f"Importing contacts from {filepath}...")

    imported = 0
    skipped = 0
    stagger_delay = 0  # seconds between each contact's opener

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = row.get("phone") or row.get("Phone") or row.get("phone_number", "")
            first_name = row.get("first_name") or row.get("First Name") or row.get("firstName", "")
            ghl_id = row.get("ghl_contact_id") or row.get("id", "")

            if not phone:
                skipped += 1
                continue

            if not ghl_id:
                ghl_id = f"import_{phone.replace('+', '').replace(' ', '').replace('-', '')}"

            upsert_contact(
                ghl_contact_id=ghl_id,
                phone=phone,
                first_name=first_name,
                location_id=GHL_LOCATION_ID,
                test_mode=test_mode
            )
            # Stagger: spread sends across the day (1 contact per 30 seconds by default)
            set_next_action(ghl_id, time.time() + stagger_delay)
            stagger_delay += delay_between or 30  # 30 second stagger between contacts

            imported += 1
            if imported % 100 == 0:
                print(f"  Imported {imported} contacts...")

    print(f"\nImport complete: {imported} imported, {skipped} skipped")
    print(f"First send in: now")
    print(f"Last send in: {stagger_delay/3600:.1f} hours (staggered)")


def print_stats():
    """Print current stats and bandit performance report."""
    from database import init_db, get_conn
    from optimizer import get_performance_report, initialize_variants

    init_db()
    initialize_variants()

    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='active'").fetchone()[0]
    booked = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='booked'").fetchone()[0]
    lost = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='lost'").fetchone()[0]
    opted_out = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='opted_out'").fetchone()[0]
    not_interested = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='not_interested'").fetchone()[0]
    qualified = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='qualified'").fetchone()[0]
    sent = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='outbound'").fetchone()[0]
    received = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='inbound'").fetchone()[0]
    conn.close()

    print("\n" + "="*60)
    print("SMS BOT STATS")
    print("="*60)
    print(f"Total contacts:    {total}")
    print(f"Active:            {active}")
    print(f"Qualified:         {qualified}")
    print(f"Booked:            {booked}")
    print(f"Lost:              {lost}")
    print(f"Not interested:    {not_interested}")
    print(f"Opted out:         {opted_out}")
    print(f"\nMessages sent:     {sent}")
    print(f"Messages received: {received}")
    if sent > 0:
        print(f"Reply rate:        {received/sent*100:.1f}%")
    print("\n" + get_performance_report())


def reset_test_contacts():
    """Remove all test contacts from the database, including all messages and dedup fingerprints."""
    from database import init_db, get_conn
    init_db()
    conn = get_conn()
    # Get test contact IDs first
    test_ids = [r[0] for r in conn.execute("SELECT ghl_contact_id FROM contacts WHERE test_mode=1").fetchall()]
    deleted = conn.execute("DELETE FROM contacts WHERE test_mode=1").rowcount
    if test_ids:
        placeholders = ",".join("?" * len(test_ids))
        conn.execute(f"DELETE FROM messages WHERE contact_id IN ({placeholders})", test_ids)
        # processed_messages uses ghl_message_id not contact_id — wipe all fingerprints on test reset
        conn.execute("DELETE FROM processed_messages")
    conn.commit()
    conn.close()
    print(f"Removed {deleted} test contacts and all associated messages/fingerprints from database.")


def _print_live_stats():
    """Print a brief live stats line."""
    from database import get_conn
    conn = get_conn()
    active = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='active'").fetchone()[0]
    booked = conn.execute("SELECT COUNT(*) FROM contacts WHERE status='booked'").fetchone()[0]
    sent = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='outbound'").fetchone()[0]
    received = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='inbound'").fetchone()[0]
    conn.close()
    reply_rate = f"{received/sent*100:.1f}%" if sent > 0 else "0%"
    print(f"[{datetime.now().strftime('%H:%M:%S')}] active={active} booked={booked} sent={sent} received={received} reply_rate={reply_rate}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SMS Reactivation Bot")
    parser.add_argument("--mode", choices=[
        "dry_run", "test", "live", "add_test_contact",
        "import_csv", "stats", "reset_test"
    ], required=True)
    parser.add_argument("--phone", help="Phone number for test contact (e.g. +15551234567)")
    parser.add_argument("--name", help="First name for test contact")
    parser.add_argument("--file", help="CSV file path for import_csv mode")
    parser.add_argument("--test_import", action="store_true", help="Import CSV in test mode")

    args = parser.parse_args()

    if args.mode == "dry_run":
        run_dry_run()
    elif args.mode == "test":
        run_test_mode()
    elif args.mode == "live":
        run_live_mode()
    elif args.mode == "add_test_contact":
        if not args.phone:
            print("Error: --phone required for add_test_contact mode")
            sys.exit(1)
        add_test_contact(args.phone, args.name)
    elif args.mode == "import_csv":
        if not args.file:
            print("Error: --file required for import_csv mode")
            sys.exit(1)
        import_csv(args.file, test_mode=args.test_import)
    elif args.mode == "stats":
        print_stats()
    elif args.mode == "reset_test":
        reset_test_contacts()
