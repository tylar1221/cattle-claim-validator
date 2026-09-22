# cattle_fastapi/create_test_user.py
from services.db import get_conn
from services.auth import hash_password
import uuid

conn = get_conn()
try:
    conn.execute(
        "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, ?)",
        ("user_" + uuid.uuid4().hex[:12], "testadmin", hash_password("admin"), "admin"),
    )
    conn.commit()
    print("User 'testadmin' created with password 'admin'.")
except Exception as e:
    print(f"Could not create user: {e}")
finally:
    conn.close()