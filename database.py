"""
database.py — State DB for SMS Reactivation Bot
SQLite-based. Tracks every contact, conversation state, message log,
bandit variant scores, and processed message IDs (idempotency).
"""

import sqlite3
import json
import time
from datetime import datetime
from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_conn()
    c = conn.cursor()

    # Contacts table — one row per contact
    c.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id TEXT PRIMARY KEY,
            ghl_contact_id TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            first_name TEXT,
            location_id TEXT NOT NULL,
            test_mode INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            -- status: active | qualified | booked | showed | lost | not_interested | opted_out | invalid
            sequence_step INTEGER DEFAULT 0,
            followup_attempts INTEGER DEFAULT 0,
            score INTEGER DEFAULT 0,
            next_action_at REAL,
            -- unix timestamp of when next scheduled action fires
            locked INTEGER DEFAULT 0,
            -- 1 = being processed right now (prevents race conditions)
            locked_at REAL,
            created_at REAL DEFAULT (strftime('%s','now')),
            updated_at REAL DEFAULT (strftime('%s','now'))
        )
    """)

    # Messages table — every message sent and received
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            -- direction: outbound | inbound
            body TEXT NOT NULL,
            variant_id TEXT,
            -- which A/B variant was used for outbound messages
            sent_at REAL DEFAULT (strftime('%s','now')),
            ghl_message_id TEXT,
            -- GHL's messageId for idempotency checks
            FOREIGN KEY (contact_id) REFERENCES contacts(id)
        )
    """)

    # Processed messages — idempotency guard (prevents double-processing)
    c.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            ghl_message_id TEXT PRIMARY KEY,
            processed_at REAL DEFAULT (strftime('%s','now'))
        )
    """)

    # Contact facts — extracted by Mem0 / stored locally as backup
    c.execute("""
        CREATE TABLE IF NOT EXISTS contact_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id TEXT NOT NULL,
            fact TEXT NOT NULL,
            created_at REAL DEFAULT (strftime('%s','now')),
            FOREIGN KEY (contact_id) REFERENCES contacts(id)
        )
    """)

    # Bandit variants — Thompson Sampling state per sequence step
    c.execute("""
        CREATE TABLE IF NOT EXISTS bandit_variants (
            id TEXT PRIMARY KEY,
            -- e.g. "step_1_opener_A"
            step_name TEXT NOT NULL,
            variant_label TEXT NOT NULL,
            message_template TEXT NOT NULL,
            alpha REAL DEFAULT 1.0,
            -- successes + 1 (Thompson Sampling)
            beta REAL DEFAULT 1.0,
            -- failures + 1
            total_sent INTEGER DEFAULT 0,
            total_replied INTEGER DEFAULT 0,
            total_positive INTEGER DEFAULT 0,
            created_at REAL DEFAULT (strftime('%s','now')),
            updated_at REAL DEFAULT (strftime('%s','now'))
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] Tables initialized.")


# ─── CONTACT FUNCTIONS ────────────────────────────────────────────────────────

def upsert_contact(ghl_contact_id, phone, first_name=None, location_id=None, test_mode=False):
    """Insert or update a contact. Returns the contact row."""
    conn = get_conn()
    c = conn.cursor()
    now = time.time()
    c.execute("""
        INSERT INTO contacts (id, ghl_contact_id, phone, first_name, location_id, test_mode, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ghl_contact_id) DO UPDATE SET
            phone=excluded.phone,
            first_name=excluded.first_name,
            test_mode=excluded.test_mode,
            updated_at=excluded.updated_at
    """, (ghl_contact_id, ghl_contact_id, phone, first_name, location_id, int(test_mode), now, now))
    conn.commit()
    row = c.execute("SELECT * FROM contacts WHERE ghl_contact_id=?", (ghl_contact_id,)).fetchone()
    conn.close()
    return dict(row)


def get_contact(ghl_contact_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM contacts WHERE ghl_contact_id=?", (ghl_contact_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_contact_status(ghl_contact_id, status):
    conn = get_conn()
    conn.execute(
        "UPDATE contacts SET status=?, updated_at=? WHERE ghl_contact_id=?",
        (status, time.time(), ghl_contact_id)
    )
    conn.commit()
    conn.close()


def update_contact_score(ghl_contact_id, score_delta):
    conn = get_conn()
    conn.execute(
        "UPDATE contacts SET score=score+?, updated_at=? WHERE ghl_contact_id=?",
        (score_delta, time.time(), ghl_contact_id)
    )
    conn.commit()
    conn.close()


def advance_sequence_step(ghl_contact_id, next_action_at):
    conn = get_conn()
    conn.execute("""
        UPDATE contacts SET
            sequence_step=sequence_step+1,
            next_action_at=?,
            updated_at=?
        WHERE ghl_contact_id=?
    """, (next_action_at, time.time(), ghl_contact_id))
    conn.commit()
    conn.close()


def increment_followup_attempts(ghl_contact_id):
    conn = get_conn()
    conn.execute(
        "UPDATE contacts SET followup_attempts=followup_attempts+1, updated_at=? WHERE ghl_contact_id=?",
        (time.time(), ghl_contact_id)
    )
    conn.commit()
    conn.close()


def set_next_action(ghl_contact_id, next_action_at):
    conn = get_conn()
    conn.execute(
        "UPDATE contacts SET next_action_at=?, updated_at=? WHERE ghl_contact_id=?",
        (next_action_at, time.time(), ghl_contact_id)
    )
    conn.commit()
    conn.close()


def lock_contact(ghl_contact_id):
    """Lock a contact for processing. Returns True if lock acquired."""
    conn = get_conn()
    now = time.time()
    # Release stale locks older than 5 minutes
    conn.execute(
        "UPDATE contacts SET locked=0 WHERE locked=1 AND locked_at < ?",
        (now - 300,)
    )
    result = conn.execute("""
        UPDATE contacts SET locked=1, locked_at=?
        WHERE ghl_contact_id=? AND locked=0
    """, (now, ghl_contact_id))
    conn.commit()
    acquired = result.rowcount > 0
    conn.close()
    return acquired


def unlock_contact(ghl_contact_id):
    conn = get_conn()
    conn.execute(
        "UPDATE contacts SET locked=0, locked_at=NULL WHERE ghl_contact_id=?",
        (ghl_contact_id,)
    )
    conn.commit()
    conn.close()


def get_contacts_due_for_action():
    """Return all active contacts whose next_action_at has passed."""
    conn = get_conn()
    now = time.time()
    rows = conn.execute("""
        SELECT * FROM contacts
        WHERE status='active'
        AND next_action_at IS NOT NULL
        AND next_action_at <= ?
        AND locked=0
        ORDER BY next_action_at ASC
    """, (now,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── MESSAGE LOG FUNCTIONS ────────────────────────────────────────────────────

def log_message(contact_id, direction, body, variant_id=None, ghl_message_id=None):
    conn = get_conn()
    conn.execute("""
        INSERT INTO messages (contact_id, direction, body, variant_id, sent_at, ghl_message_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (contact_id, direction, body, variant_id, time.time(), ghl_message_id))
    conn.commit()
    conn.close()


def get_recent_messages(contact_id, limit=6):
    """Get the last N messages for a contact (for LLM context)."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT direction, body, sent_at FROM messages
        WHERE contact_id=?
        ORDER BY sent_at DESC
        LIMIT ?
    """, (contact_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


# ─── IDEMPOTENCY FUNCTIONS ────────────────────────────────────────────────────

def is_message_processed(ghl_message_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM processed_messages WHERE ghl_message_id=?",
        (ghl_message_id,)
    ).fetchone()
    conn.close()
    return row is not None


def mark_message_processed(ghl_message_id):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages (ghl_message_id) VALUES (?)",
        (ghl_message_id,)
    )
    conn.commit()
    conn.close()


# ─── CONTACT FACTS ────────────────────────────────────────────────────────────

def save_fact(contact_id, fact):
    conn = get_conn()
    conn.execute(
        "INSERT INTO contact_facts (contact_id, fact) VALUES (?, ?)",
        (contact_id, fact)
    )
    conn.commit()
    conn.close()


def get_facts(contact_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT fact FROM contact_facts WHERE contact_id=? ORDER BY created_at DESC",
        (contact_id,)
    ).fetchall()
    conn.close()
    return [r["fact"] for r in rows]


# ─── BANDIT FUNCTIONS ────────────────────────────────────────────────────────

def init_bandit_variants(variants: list):
    """Seed the bandit table with initial variants. Skips if already exists."""
    conn = get_conn()
    for v in variants:
        conn.execute("""
            INSERT OR IGNORE INTO bandit_variants
            (id, step_name, variant_label, message_template)
            VALUES (?, ?, ?, ?)
        """, (v["id"], v["step_name"], v["variant_label"], v["message_template"]))
    conn.commit()
    conn.close()


def get_variants_for_step(step_name):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bandit_variants WHERE step_name=?",
        (step_name,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_variant_result(variant_id, positive: bool):
    """Update Thompson Sampling alpha/beta for a variant."""
    conn = get_conn()
    if positive:
        conn.execute("""
            UPDATE bandit_variants SET
                alpha=alpha+1,
                total_replied=total_replied+1,
                total_positive=total_positive+1,
                updated_at=?
            WHERE id=?
        """, (time.time(), variant_id))
    else:
        conn.execute("""
            UPDATE bandit_variants SET
                beta=beta+1,
                total_replied=total_replied+1,
                updated_at=?
            WHERE id=?
        """, (time.time(), variant_id))
    conn.commit()
    conn.close()


def increment_variant_sent(variant_id):
    conn = get_conn()
    conn.execute(
        "UPDATE bandit_variants SET total_sent=total_sent+1, updated_at=? WHERE id=?",
        (time.time(), variant_id)
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("[DB] Database initialized successfully.")
