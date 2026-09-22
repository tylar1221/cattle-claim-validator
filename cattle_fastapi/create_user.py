from services.db import get_conn
from services.auth import hash_password
import uuid

conn = get_conn()
conn.execute(
    "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, ?)",
    ("user_" + uuid.uuid4().hex[:12], "admin", hash_password("admin"), "admin"),
)
conn.commit()
conn.close()
print("User created.")