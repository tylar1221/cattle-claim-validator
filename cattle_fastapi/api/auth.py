# cattle_fastapi/api/auth.py
import os

from fastapi import APIRouter, Form, Response, Depends, Request

from services.db import get_conn
from services.auth import verify_password, create_session, require_login, get_user_from_token, delete_session
from limiter import limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])
@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, response: Response, username: str = Form(...), password: str = Form(...)):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, username, password_hash, is_active, role FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    conn.close()

    if not row or not row["is_active"] or not verify_password(password, row["password_hash"]):
        return {"ok": False, "error": "Invalid username or password"}

    token = create_session(row["id"])

    # secure=True means the browser ONLY sends this cookie over HTTPS.
    # ⚠️ DEV-LOCALHOST EXCEPTION: when you're running the app locally at
    # http://localhost:8000, secure=True would prevent the cookie from
    # being set at all (browsers do treat "localhost" as a special case
    # that's ALWAYS allowed to use secure cookies, but older browsers
    # and some in-app webviews don't), so we skip secure only when an
    # env var explicitly says we're in dev. In production (real domain,
    # HTTPS in front of uvicorn), secure=True is unconditional.
    # Set DEV_MODE=1 in your local shell to develop over http://localhost.
    is_dev = os.environ.get("DEV_MODE") == "1"

    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=not is_dev,           # NEW -- was missing entirely
        max_age=7 * 24 * 60 * 60,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "username": row["username"], "role": row["role"]}


@router.post("/logout")
async def logout(request: Request, response: Response):
    # Grab the token from the request BEFORE clearing the cookie, so we
    # can invalidate the server-side row it refers to.
    token = request.cookies.get("session_token")

    # Server-side invalidation -- the important half. Without this, the
    # cookie disappears from the browser but the token itself stays
    # usable for up to 7 days by anyone who has a copy of it.
    delete_session(token)

    # Client-side cleanup -- tell the browser to drop the cookie.
    # secure/path must match what login() set, or some browsers will
    # refuse to actually clear it.
    is_dev = os.environ.get("DEV_MODE") == "1"
    response.delete_cookie(
        key="session_token",
        path="/",
        secure=not is_dev,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    token = request.cookies.get("session_token")
    user = get_user_from_token(token)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not logged in")
    return user