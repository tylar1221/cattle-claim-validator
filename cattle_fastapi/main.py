import os

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded


# ---------------------------------------------------------
# APP
# ---------------------------------------------------------

app = FastAPI(title="Cattle Claim Validator")


# ---------------------------------------------------------
# RATE LIMITING
# ---------------------------------------------------------

# ---------------------------------------------------------
# RATE LIMITING
# ---------------------------------------------------------

from limiter import limiter          # <-- NEW: import from the new file

app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    _rate_limit_exceeded_handler
)


# ---------------------------------------------------------
# IMPORT ROUTERS
# ---------------------------------------------------------
# IMPORTANT:
# limiter must already exist before auth.py imports it.

from api import captures, cases, auth
from services.db import init_db


# ---------------------------------------------------------
# STARTUP
# ---------------------------------------------------------

@app.on_event("startup")
async def startup():
    init_db()


# ---------------------------------------------------------
# API ROUTERS
# ---------------------------------------------------------

app.include_router(captures.router)
app.include_router(cases.router)
app.include_router(auth.router)


# ---------------------------------------------------------
# STATIC FILES
# ---------------------------------------------------------

app.mount(
    "/static",
    StaticFiles(directory="static", html=True),
    name="static"
)


# ---------------------------------------------------------
# ROOT
# ---------------------------------------------------------

@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


# ---------------------------------------------------------
# RUN
# ---------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))

    print(
        f"\n📍 Server: http://localhost:{port}/static/index.html"
    )

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False
    )