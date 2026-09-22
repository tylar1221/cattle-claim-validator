from services.db import get_conn
from services.auth import verify_password

conn = get_conn()

rows = conn.execute(
    "SELECT id, username, password_hash, is_active, role FROM users WHERE username = ?",
    ("admin",)
).fetchall()

print("Number of admin users:", len(rows))

for row in rows:
    print("\nID:", row["id"])
    print("Username:", row["username"])
    print("Active:", row["is_active"])
    print("Role:", row["role"])
    print("Hash:", row["password_hash"])
    
    try:
        print("Password 'admin' correct:", verify_password("admin", row["password_hash"]))
    except Exception as e:
        print("Password verification ERROR:", e)

conn.close()