
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cattle_claims.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

sql = "SELECT * FROM cases ORDER BY created_at DESC, rowid DESC"
if limit:
    sql += " LIMIT " + str(limit)
cases = conn.execute(sql).fetchall()

if not cases:
    print("No cases found.")
    raise SystemExit

print("Database:", DB)
print("Total cases shown:", len(cases))
print()

def show(row, indent):
    width = max(len(k) for k in row.keys())
    for k in row.keys():
        v = row[k]
        print(indent + k.ljust(width) + " : " + ("-" if v is None else str(v)))

for case in cases:
    print("=" * 70)
    print("CASE", case["id"])
    print("=" * 70)
    show(case, "  ")

    caps = conn.execute(
        "SELECT * FROM captures WHERE case_id = ? ORDER BY id DESC", (case["id"],)
    ).fetchall()
    print()
    print("  --- CAPTURES (" + str(len(caps)) + ") ---")
    if not caps:
        print("    (none)")
    for c in caps:
        print()
        print("    capture " + str(c["id"]) + ": " + str(c["step_id"]))
        show(c, "      ")
    print()

conn.close()
