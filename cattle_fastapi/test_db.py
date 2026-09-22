from services.db import init_db, get_conn

init_db()
conn = get_conn()
print("Connected. Tables:", [r["table_name"] for r in conn.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
).fetchall()])
conn.close()