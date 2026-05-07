"""Debug script to inspect DB state and manually trigger scheduler."""
import time
from database import init_db, get_conn

init_db()
conn = get_conn()

# Get column names
cols = [row[1] for row in conn.execute('PRAGMA table_info(contacts)').fetchall()]
print("Columns:", cols)

# Get all contacts
rows = conn.execute('SELECT * FROM contacts').fetchall()
print(f"\nContacts ({len(rows)}):")
now = time.time()
for row in rows:
    d = dict(zip(cols, row))
    due = d.get('next_action_at')
    due_str = f"{due - now:.0f}s" if due else "None"
    print(f"  {d.get('ghl_contact_id','')[:25]} | {d.get('first_name','')} | {d.get('phone','')} | status={d.get('status','')} | step={d.get('sequence_step','')} | due_in={due_str} | test={d.get('test_mode','')}")

conn.close()
