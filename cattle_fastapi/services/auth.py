import secrets
from datetime import datetime, timedelta, timezone
import bcrypt
from fastapi import Request, HTTPException, Depends
from limiter import limiter          # <-- NEW: was "from main import limiter"

from services.db import get_conn

SESSION_DURATION_DAYS = 7


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
    except (ValueError, TypeError):
        return False


def create_session(user_id: str) -> str:
    token = secrets.token_hex(32)   # random, unguessable session id
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DURATION_DAYS)
    conn = get_conn()
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires_at.isoformat()),
    )
    conn.commit()
    conn.close()
    return token


def get_user_from_token(token: str):
    if not token:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT s.user_id, s.expires_at, u.username, u.role, u.is_active "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
        (token,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    if not row["is_active"]:
        return None
    expires_at = row["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None
    return {"id": row["user_id"], "username": row["username"], "role": row["role"]}
def delete_session(token: str) -> None:
    """
    Removes a session row from the DB. Called on logout so the token
    becomes unusable immediately -- previously logout only told the
    BROWSER to forget the cookie, but the token stayed valid on the
    server until it expired 7 days later. Any copy of that token
    (browser history, dev-console screenshot, an intercepted request)
    remained a working credential for the rest of the week. Deleting
    the row server-side closes that gap.
    """
    if not token:
        return
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()

# ---- This is the "guard" you'll attach to every protected route ----
def require_login(request: Request):
    token = request.cookies.get("session_token")
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user