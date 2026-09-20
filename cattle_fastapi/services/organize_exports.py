"""
organize_exports.py -- Manually re-pushes every case's summary to Drive.
Useful for:
  - Cases created before auto-organize existed.
  - Force-refreshing every summary after a bulk database change.

For everyday use, you don't need to run this at all -- Drive stays
current on its own, updating immediately as each photo is captured
through the app.

Usage:
    python organize_exports.py                    # re-syncs EVERY case
    python organize_exports.py case_abc123def456   # just one case
"""
import sys

from services.db import get_conn
from services.organize import organize_case


def main():
    conn = get_conn()
    target_case_id = sys.argv[1] if len(sys.argv) > 1 else None

    if target_case_id:
        result = organize_case(target_case_id, conn)
        print(f"Synced to Drive: {result}" if result else f"No case found with id {target_case_id}")
    else:
        cases = conn.execute("SELECT id FROM cases ORDER BY created_at").fetchall()
        if not cases:
            print("No cases found in the database.")
        else:
            for row in cases:
                print(f"Synced to Drive: {organize_case(row['id'], conn)}")
            print(f"\nDone -- {len(cases)} case(s) re-synced to Drive")

    conn.close()


if __name__ == "__main__":
    main()