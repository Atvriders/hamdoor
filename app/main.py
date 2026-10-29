from contextlib import asynccontextmanager
from hashlib import sha1
from pathlib import Path
import logging
import mimetypes
import re

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_db, init_db
from app.integrations import aprs_feed, uls
from app.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.models import User
from app.security import signing_key
from app.routes import activity, admin, aprs, auth, hams, lookup, meta, operators, places, posts, repeaters
from app.routes import users as users_route
from app.scheduler import start_repeaters_scheduler, start_uls_scheduler

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _static_version() -> str:
    """Short content hash of every static asset. Every new image build changes
    it, so index.html references /static/...js?v=<hash> and CDN/browser
    caches can never serve a stale frontend after a deploy."""
    h = sha1()
    for p in sorted(STATIC_DIR.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(STATIC_DIR).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


STATIC_VERSION = _static_version()


def register_web_types() -> None:
    """Web fonts by their own types. The python:3.11-slim image has no
    /etc/mime.types and Python's table has no .woff2, so StaticFiles sent the
    fonts as application/octet-stream, which GZipMiddleware and Cloudflare
    then compressed again (font/woff2 is left alone)."""
    mimetypes.add_type("font/woff2", ".woff2")
    mimetypes.add_type("font/woff", ".woff")


register_web_types()

# make importer/geocoder progress visible in `docker compose logs`, independent
# of uvicorn's logging config (which can swallow root/basicConfig output)
_app_log = logging.getLogger("app")
if not any(isinstance(h, logging.StreamHandler) for h in _app_log.handlers):
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _app_log.addHandler(_h)
_app_log.setLevel(logging.INFO)
_app_log.propagate = False


def _place_mispinned_members():
    from app.integrations.geocode import place_mispinned_members

    with SessionLocal() as db:
        place_mispinned_members(db)


def _start_once(name: str, fn) -> None:
    import threading

    threading.Thread(target=fn, daemon=True, name=name).start()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # a published placeholder SECRET_KEY is replaced by a random key kept in
    # the data dir; refuses to start when that can't be done
    signing_key()
    init_db()
    uls.migrate_hams_table()
    with SessionLocal() as db:
        # `docker compose logs` shows that ADMIN_CALLSIGNS took effect, and warns about a listed callsign
        # nobody has signed up with yet
        admin.announce_admins(db)
        users_route.sync_user_locations(db)  # snap users to street-level geocodes
    _start_once("users-replace", _place_mispinned_members)  # may geocode: off the startup path
    start_uls_scheduler()  # imports FCC ULS data on first run, then weekly
    start_repeaters_scheduler()  # HearHam weekly, BrandMeister daily, IRLP twice a day
    aprs_feed.start()  # live APRS-IS consumer (receive-only)
    yield
    aprs_feed.stop()


# FastAPI's stock Swagger/ReDoc pages load unpinned CDN scripts into hamdoor's
# origin, where the session token lives: they stay off, and /api/docs below
# serves Swagger UI pinned to one version and checked by Subresource Integrity
app = FastAPI(title="hamdoor", version="1.0.0", docs_url=None, redoc_url=None,
              openapi_url="/api/openapi.json", lifespan=lifespan)

_SWAGGER = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.34.0"
_SWAGGER_JS_SRI = "sha384-9Td7MKiIr2S83E1kZyyVi8KdJw/S8CpAvu0llLyMeg0xq/hDGiZJfVn0fV4llCmK"
_SWAGGER_CSS_SRI = "sha384-4c3vsE5f+trhFyHrFrLM1fFGUn531NIaC7dcqEWIJdoRQJ5CkJbSuSCwXLpaS1KF"


@app.get("/api/docs", include_in_schema=False)
def api_docs():
    """Interactive API docs (Swagger UI). Calls still need a bearer token:
    paste one from log in into Authorize."""
    js, css = f"{_SWAGGER}/swagger-ui-bundle.js", f"{_SWAGGER}/swagger-ui.css"
    html = get_swagger_ui_html(openapi_url=app.openapi_url, title="hamdoor API",
                               swagger_js_url=js, swagger_css_url=css,
                               swagger_favicon_url="data:,").body.decode()
    html = html.replace(f'src="{js}"', f'src="{js}" integrity="{_SWAGGER_JS_SRI}" crossorigin="anonymous"')
    html = html.replace(f'href="{css}"', f'href="{css}" integrity="{_SWAGGER_CSS_SRI}" crossorigin="anonymous"')
    return HTMLResponse(html)
# map payloads run to ~1.5 MB for a dense view; gzip shrinks them ~8x
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth.router)
app.include_router(lookup.router)
app.include_router(users_route.router)
app.include_router(operators.router)
app.include_router(posts.router)
app.include_router(activity.router)
app.include_router(hams.router)
app.include_router(places.router)
app.include_router(repeaters.router)
app.include_router(aprs.router)
app.include_router(meta.router)
app.include_router(admin.router)


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


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
def favicon():
    """The door mark, for clients that ask for /favicon.ico without reading
    the page's <link rel="icon"> (a 404 here was cached by the CDN)."""
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/", include_in_schema=False)
def index():
    # no-cache: this is what points browsers at the current versioned assets
    html = (STATIC_DIR / "index.html").read_text()
    html = _ASSET_REF.sub(lambda m: f"{m.group(1)}?v={STATIC_VERSION}", html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# every local script/stylesheet reference in index.html gets the version query
_ASSET_REF = re.compile(r"""(/static/[^"'?#\s]+\.(?:js|css))(?=["'])""")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
