"""Reset test contact for a clean re-run."""
import sqlite3
from database import init_db, DB_PATH

init_db()
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Show column names
cols = conn.execute("PRAGMA table_info(contacts)").fetchall()
print("Contacts columns:", [c["name"] for c in cols])

# Find the test contact
rows = conn.execute("SELECT * FROM contacts").fetchall()
print(f"Total contacts: {len(rows)}")
for r in rows:
    print(dict(r))

# Reset all contacts to fresh state
conn.execute("UPDATE contacts SET sequence_step=0, followup_attempts=0, status='active', next_action_at=NULL, score=0, locked=0")
conn.execute("DELETE FROM messages")
conn.execute("DELETE FROM processed_messages")
conn.execute("DELETE FROM contact_facts")
conn.commit()
conn.close()
print("All test contacts reset.")
