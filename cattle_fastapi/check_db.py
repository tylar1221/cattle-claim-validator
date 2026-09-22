import sys
from services.db import get_conn

conn = get_conn()

# Optional: python show_latest.py 5   -> only the 5 newest cases
limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

sql = "SELECT * FROM cases ORDER BY created_at DESC"
if limit:
    sql += f" LIMIT {limit}"
cases = conn.execute(sql).fetchall()

if not cases:
    print("No cases found.")
    raise SystemExit

print(f"Database: AWS RDS")
print(f"Total cases shown: {len(cases)}\n")

for case in cases:
    print("=" * 70)
    print(f"CASE {case['id']}")
    print("=" * 70)
    for k in case.keys():
        if case[k] not in (None, ""):
            print(f"  {k}: {case[k]}")

    caps = conn.execute(
        "SELECT * FROM captures WHERE case_id = ? ORDER BY id DESC", (case["id"],)
    ).fetchall()
    print(f"\n  --- CAPTURES ({len(caps)}) ---")
    if not caps:
        print("    (none - no photo reached the server for this case)")
    for c in caps:
        print(f"\n    capture {c['id']}: {c['step_id']}")
        for k in c.keys():
            if c[k] not in (None, ""):
                print(f"      {k}: {c[k]}")
    print()

conn.close()