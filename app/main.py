from contextlib import asynccontextmanager
from hashlib import sha1
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, init_db
from app.integrations import uls
from app.models import User
from app.routes import activity, auth, hams, lookup, operators, posts, users
from app.scheduler import start_uls_scheduler

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _static_version() -> str:
    """Short content hash of the static assets. Every new image build changes
    it, so index.html references /static/app.js?v=<hash> and CDN/browser
    caches can never serve a stale frontend after a deploy."""
    h = sha1()
    for p in sorted(STATIC_DIR.glob("*.*")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:10]


STATIC_VERSION = _static_version()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    uls.migrate_hams_table()
    start_uls_scheduler()  # imports FCC ULS data on first run, then weekly
    yield


app = FastAPI(title="hamdoor", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(lookup.router)
app.include_router(users.router)
app.include_router(operators.router)
app.include_router(posts.router)
app.include_router(activity.router)
app.include_router(hams.router)


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    """Liveness + a user count, so deployments can verify the database
    actually persisted across container recreates."""
    return {
        "status": "ok",
        "service": "hamdoor",
        "users": db.scalar(select(func.count()).select_from(User)),
        "database": get_settings().database_url.rsplit("/", 1)[-1],
    }


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        # safe to cache: URLs carry the content-hash version query
        resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.get("/", include_in_schema=False)
def index():
    # no-cache: this is what points browsers at the current versioned assets
    html = (STATIC_DIR / "index.html").read_text()
    html = html.replace("/static/app.js", f"/static/app.js?v={STATIC_VERSION}")
    html = html.replace("/static/style.css", f"/static/style.css?v={STATIC_VERSION}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
