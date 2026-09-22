

import sys
from services.db import get_conn

conn = get_conn()

# Optional limit
limit = int(sys.argv[1]) if len(sys.argv) > 1 else None


def value(v):
    if v is None or v == "":
        return "-"
    return str(v)


def print_table(rows, title):
    print()
    print("=" * 160)
    print(title)
    print("=" * 160)

    if not rows:
        print("No records found.")
        return

    columns = rows[0].keys()

    # Convert everything to strings
    data = []
    for row in rows:
        data.append([value(row[col]) for col in columns])

    # Calculate widths
    widths = []

    for i, col in enumerate(columns):
        width = len(str(col))

        for row in data:
            width = max(width, len(row[i]))

        # Prevent extremely wide columns
        width = min(width, 40)

        widths.append(width)

    # Header
    header = " | ".join(
        str(col)[:widths[i]].ljust(widths[i])
        for i, col in enumerate(columns)
    )

    separator = "-+-".join(
        "-" * widths[i]
        for i in range(len(columns))
    )

    print(header)
    print(separator)

    # Rows
    for row in data:
        print(
            " | ".join(
                row[i][:widths[i]].ljust(widths[i])
                for i in range(len(columns))
            )
        )


# =========================================================
# CASES
# =========================================================

sql = """
SELECT *
FROM cases
ORDER BY created_at DESC
"""

if limit:
    sql += f" LIMIT {limit}"

cases = conn.execute(sql).fetchall()

if not cases:
    print("No cases found.")
    conn.close()
    raise SystemExit


print()
print("=" * 160)
print("CATTLE CLAIMS DATABASE")
print("=" * 160)
print("Database: AWS RDS")
print("Cases shown:", len(cases))
print("Order: NEWEST -> OLDEST")


# Show ALL CASE columns together
print_table(cases, "CASES - ALL COLUMNS")


# =========================================================
# CAPTURES
# =========================================================

captures = conn.execute(
    """
    SELECT *
    FROM captures
    ORDER BY id DESC
    """
).fetchall()

print_table(captures, "CAPTURES - ALL COLUMNS")


# =========================================================
# CASE + CAPTURES TOGETHER
# =========================================================

print()
print("=" * 160)
print("CASE-WISE CAPTURE DETAILS")
print("=" * 160)

for case in cases:

    case_id = case["id"]

    print()
    print("-" * 160)
    print("CASE ID:", case_id)
    print("-" * 160)

    # ALL case columns
    for column in case.keys():
        print(
            f"{column:<25} : {value(case[column])}"
        )

    case_captures = conn.execute(
        """
        SELECT *
        FROM captures
        WHERE case_id = ?
        ORDER BY id DESC
        """,
        (case_id,)
    ).fetchall()

    print()
    print(f"CAPTURES: {len(case_captures)}")

    if not case_captures:
        print("  No captures.")
        continue

    # Print every capture and every column
    for capture_no, capture in enumerate(case_captures, 1):

        print()
        print(
            f"  CAPTURE #{capture_no}"
        )

        for column in capture.keys():
            print(
                f"    {column:<23} : {value(capture[column])}"
            )


# =========================================================
# SUMMARY
# =========================================================
total_cases = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
total_captures = conn.execute("SELECT COUNT(*) AS n FROM captures").fetchone()["n"]
print()
print("=" * 160)
print("DATABASE SUMMARY")
print("=" * 160)
print("Total cases       :", total_cases)
print("Cases displayed   :", len(cases))
print("Total captures    :", total_captures)
print("=" * 160)

conn.close()
