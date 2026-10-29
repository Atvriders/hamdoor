"""Release hardening (final verification pass): the signing key, sessions
after a password change, throttles on the anonymous endpoints, request and
field size limits, bidi controls, security headers, posts outside your range,
nearby's range cap and SQLite's journal mode.

Members live around Anchorage and Fairbanks, AK (KL7Hxx callsigns), so
nothing another module writes is in their range. No network."""

import os
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest

from app import ratelimit, security
from app.config import get_settings
from app.db import engine
from app.integrations import geocode
from app.models import User
from app.security import create_token, hash_password, password_fingerprint
from tests.conftest import auth

_PW_HASH = hash_password("password123")
ANCHORAGE = (61.2181, -149.9003)
FAIRBANKS = (64.8378, -147.7164)  # ~260 mi from Anchorage


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    def no_network(self, request):
        raise AssertionError(f"network access in a test: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)


def member(db, callsign, lat=ANCHORAGE[0], lon=ANCHORAGE[1], range_miles=25, **fields) -> User:
    u = db.query(User).filter_by(callsign=callsign).one_or_none()
    if u is None:
        u = User(callsign=callsign, password_hash=_PW_HASH, email=f"{callsign.lower()}@example.com",
                 name=f"Name {callsign}")
        db.add(u)
    u.lat, u.lon, u.range_miles, u.map_precision = lat, lon, range_miles, "address"
    for k, v in fields.items():
        setattr(u, k, v)
    db.commit()
    db.refresh(u)
    return u


def hdr(user: User) -> dict:
    return auth(create_token(user))


@pytest.fixture()
def throttled(monkeypatch):
    """The throttles on (conftest switches them off for every other test)."""
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


# ---------------------------------------------------------------- SEC-01 signing key


@pytest.fixture()
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "_data_dir", lambda: str(tmp_path))
    security.reset_signing_key_cache()
    yield tmp_path
    security.reset_signing_key_cache()


@pytest.mark.parametrize("weak", ["change-me-to-a-long-random-string", "change-me-in-production", "",
                                  "short-but-not-empty"])
def test_a_placeholder_or_short_secret_key_is_never_used(weak, key_dir, monkeypatch):
    """SEC-01 regression: the compose file's published SECRET_KEY signed
    every session, so anyone could forge a login for any member."""
    monkeypatch.setattr(get_settings(), "secret_key", weak)
    key = security.signing_key()
    assert key != weak and len(key) == 64
    f = key_dir / security.SECRET_KEY_FILE
    assert f.read_text().strip() == key
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    security.reset_signing_key_cache()
    assert security.signing_key() == key  # kept across restarts


def test_a_real_secret_key_is_used_as_is(key_dir, monkeypatch):
    strong = "x9" * 24
    monkeypatch.setattr(get_settings(), "secret_key", strong)
    assert security.signing_key() == strong
    assert not (key_dir / security.SECRET_KEY_FILE).exists()


def test_refuses_to_start_when_no_key_can_be_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "_data_dir", lambda: str(tmp_path / "missing" / "dir"))
    monkeypatch.setattr(get_settings(), "secret_key", "change-me-to-a-long-random-string")
    security.reset_signing_key_cache()
    try:
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            security.signing_key()
    finally:
        security.reset_signing_key_cache()


def test_a_token_forged_with_the_placeholder_is_refused(client, db, key_dir, monkeypatch):
    placeholder = "change-me-to-a-long-random-string"
    monkeypatch.setattr(get_settings(), "secret_key", placeholder)
    u = member(db, "KL7HSK")
    now = datetime.now(timezone.utc)
    forged = jwt.encode({"sub": u.callsign, "uid": u.id, "pwv": password_fingerprint(u),
                         "iat": now, "exp": now + timedelta(hours=1)}, placeholder, algorithm="HS256")
    assert client.get("/api/me", headers=auth(forged)).status_code == 401  # old code: 200
    assert client.get("/api/me", headers=hdr(u)).status_code == 200


# ---------------------------------------------------------------- SEC-02 / V1-08 sessions


def test_changing_the_password_ends_every_other_session(client, db):
    """V1-08 / SEC-02 regression: tokens outlived a password change by up
    to 14 days. The session that changed it carries on with a new token."""
    u = member(db, "KL7HPW")
    other = hdr(u)  # e.g. a stolen token
    mine = hdr(u)
    r = client.post("/api/me/password", headers=mine,
                    json={"current_password": "password123", "new_password": "a-new-password"})
    assert r.status_code == 200, r.text
    fresh = r.json()["token"]
    assert r.json()["user"]["callsign"] == "KL7HPW"
    assert client.get("/api/me", headers=other).status_code == 401  # old code: 200
    assert client.get("/api/me", headers=mine).status_code == 401
    assert client.get("/api/me", headers=auth(fresh)).status_code == 200
    assert client.post("/api/auth/login", json={"callsign": "KL7HPW", "password": "a-new-password"}
                       ).status_code == 200
    db.refresh(u)
    u.password_hash = _PW_HASH
    db.commit()


@pytest.mark.parametrize("drop", ["exp", "iat", "pwv", "uid", "sub"])
def test_tokens_must_carry_every_claim(client, db, drop):
    """A correctly signed token without exp (or the other claims) was accepted."""
    u = member(db, "KL7HCL")
    now = datetime.now(timezone.utc)
    claims = {"sub": u.callsign, "uid": u.id, "pwv": password_fingerprint(u), "iat": now,
              "exp": now + timedelta(hours=1)}
    full = jwt.encode(claims, security.signing_key(), algorithm="HS256")
    assert client.get("/api/me", headers=auth(full)).status_code == 200
    del claims[drop]
    token = jwt.encode(claims, security.signing_key(), algorithm="HS256")
    assert client.get("/api/me", headers=auth(token)).status_code == 401


# ---------------------------------------------------------------- SEC-03 throttles


def test_wrong_passwords_are_throttled_per_callsign(client, db, throttled, monkeypatch):
    """SEC-03 regression: 40 wrong passwords in 10 s all got 401 and the
    right one still worked straight after (~55 guesses a second)."""
    member(db, "KL7HTH")
    login = {"callsign": "KL7HTH", "password": "wrong-password"}
    codes = [client.post("/api/auth/login", json=login).status_code for _ in range(5)]
    assert codes == [401] * 5
    r = client.post("/api/auth/login", json=login)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    # locked: even the right password waits (or guessing would go on)
    right = {"callsign": "KL7HTH", "password": "password123"}
    assert client.post("/api/auth/login", json=right).status_code == 429
    # other members are not affected
    member(db, "KL7HTI")
    assert client.post("/api/auth/login", json={"callsign": "KL7HTI", "password": "password123"}
                       ).status_code == 200
    later = time.monotonic() + 301
    monkeypatch.setattr(ratelimit, "_clock", lambda: later)
    assert client.post("/api/auth/login", json=right).status_code == 200
    assert client.post("/api/auth/login", json=login).status_code == 401  # count started again


def test_log_in_attempts_are_throttled_per_address(client, db, throttled):
    member(db, "KL7HTA")
    ok = {"callsign": "KL7HTA", "password": "password123"}
    unknown = [{"callsign": f"KL7HU{i % 10}", "password": "x"} for i in range(19)]
    assert {client.post("/api/auth/login", json=b).status_code for b in unknown} == {404}
    assert client.post("/api/auth/login", json=ok).status_code == 200  # the 20th
    assert client.post("/api/auth/login", json=ok).status_code == 429


def test_sign_up_and_lookup_are_throttled_per_address(client, throttled):
    codes = [client.get(f"/api/lookup/KL7HL{i % 10}").status_code for i in range(30)]
    assert set(codes) == {200}
    r = client.get("/api/lookup/KL7HLX")
    assert r.status_code == 429 and "retry-after" in r.headers
    body = {"callsign": "KL7HZZ", "password": "password123", "email": "x@example.com"}
    codes = [client.post("/api/auth/signup", json=body).status_code for _ in range(10)]
    assert set(codes) == {400}  # not in FCC records
    assert client.post("/api/auth/signup", json=body).status_code == 429


def test_throttle_window_and_retry_after():
    t = ratelimit.Throttle("t", 2, 60)
    t.add("k", now=0)
    t.add("k", now=10)
    assert t.retry_after("k", now=20) == 40
    assert t.retry_after("k", now=60.5) == 0  # the first event left the window
    assert t.retry_after("other", now=20) == 0


# ---------------------------------------------------------------- SEC-04 / UI-01 sizes and text


def test_post_and_profile_sizes_are_limited(client, db):
    """SEC-04 regression: a 20 MB post body (201) froze every neighbour's
    Feed, and a 1,000,000-character name went into every Nearby response."""
    u = member(db, "KL7HSZ")
    h = hdr(u)
    r = client.post("/api/posts", headers=h, json={"title": "t", "body": "x" * 10001})
    assert r.status_code == 422
    r = client.post("/api/posts", headers=h, json={"title": "t", "body": "x" * 10000})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.patch(f"/api/posts/{pid}", headers=h, json={"body": "y" * 10001}).status_code == 422
    assert client.patch("/api/me", headers=h, json={"name": "n" * 129}).status_code == 422
    assert client.patch("/api/me", headers=h, json={"city": "c" * 129}).status_code == 422
    assert client.patch("/api/me", headers=h, json={"name": "n" * 128}).status_code == 200
    signup = {"callsign": "W1AAA", "password": "password123", "email": "a@example.com",
              "address_line": "a" * 256}
    assert client.post("/api/auth/signup", json=signup).status_code == 422
    assert client.delete(f"/api/posts/{pid}", headers=h).status_code == 204


def test_request_bodies_over_the_cap_get_413(client, db):
    u = member(db, "KL7HBG")
    big = b'{"title": "t", "body": "' + b"x" * (200 * 1024) + b'"}'
    headers = {**hdr(u), "Content-Type": "application/json"}
    r = client.post("/api/posts", headers=headers, content=big)
    assert r.status_code == 413
    # streamed (chunked, no Content-Length): cut off while it arrives
    r = client.post("/api/posts", headers=headers,
                    content=iter([big[i:i + 16384] for i in range(0, len(big), 16384)]))
    assert r.status_code == 413
    assert client.post("/api/posts", headers=headers, content=b'{"title": "t", "body": "ok"}'
                       ).status_code == 201


def test_bidi_controls_are_stripped_from_member_text(client, db):
    """UI-01 regression: 'Alex\\u202eXYZ' as a name reversed the app's own
    text around it ('Alexroodmah no ,ZYX')."""
    u = member(db, "KL7HBD")
    h = hdr(u)
    r = client.patch("/api/me", headers=h, json={"name": "Alex‮XYZ", "city": "Anch⁦orage⁩"})
    assert (r.json()["name"], r.json()["city"]) == ("AlexXYZ", "Anchorage")
    p = client.post("/api/posts", headers=h, json={"title": "Net ‮tonight", "body": "Body‭"}).json()
    assert (p["title"], p["body"]) == ("Net tonight", "Body")
    c = client.post(f"/api/posts/{p['id']}/comments", headers=h, json={"body": "ok‮"}).json()
    assert c["body"] == "ok"
    client.delete(f"/api/posts/{p['id']}", headers=h)


# ---------------------------------------------------------------- SEC-06 / SEC-07 headers, docs


def test_security_headers_and_pinned_swagger_page(client):
    for path in ("/", "/api/health", "/static/js/hd-core.js", "/api/docs"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    # the interactive docs load Swagger UI pinned to one version with
    # Subresource Integrity (the stock page loads an unpinned CDN build)
    html = client.get("/api/docs").text
    assert "swagger-ui-dist@5.34.0/swagger-ui-bundle.js\" integrity=\"sha384-" in html
    assert "swagger-ui-dist@5.34.0/swagger-ui.css\" integrity=\"sha384-" in html
    assert "swagger-ui-dist@5/" not in html and "fastapi.tiangolo.com" not in html
    assert client.get("/api/redoc").status_code == 404
    assert client.get("/api/openapi.json").status_code == 200


def test_hsts_only_on_https(client):
    """QA BE-5: no Strict-Transport-Security, so a first plain-HTTP visit
    could be stripped of TLS. It goes on HTTPS answers only — directly, or
    behind a proxy that says so (X-Forwarded-Proto, Cloudflare's CF-Visitor)
    — never on plain HTTP, where browsers ignore it anyway."""
    from fastapi.testclient import TestClient

    from app.main import app

    hsts = "strict-transport-security"
    for path in ("/", "/api/health", "/static/js/hd-core.js", "/api/nope"):
        assert hsts not in client.get(path).headers, path
        r = client.get(path, headers={"X-Forwarded-Proto": "https"})
        assert r.headers[hsts] == "max-age=31536000", path
        assert r.headers["x-frame-options"] == "DENY"  # the other headers stay
    assert hsts in client.get("/", headers={"CF-Visitor": '{"scheme":"https"}'}).headers
    assert hsts in client.get("/", headers={"X-Forwarded-Proto": "https, http"}).headers  # the first hop
    assert hsts not in client.get("/", headers={"X-Forwarded-Proto": "http"}).headers
    assert hsts not in client.get("/", headers={"CF-Visitor": '{"scheme":"http"}'}).headers
    tls = TestClient(app, base_url="https://testserver")  # served over TLS itself
    assert tls.get("/static/favicon.svg").headers[hsts] == "max-age=31536000"


def test_web_fonts_are_served_with_their_types(client, monkeypatch):
    """QA BE-6: the python:3.11-slim image has no /etc/mime.types and Python's
    own table has no .woff2: fonts went out as application/octet-stream, and
    were gzipped again on the way."""
    import mimetypes

    from app import main

    monkeypatch.setattr(mimetypes, "_db", mimetypes.MimeTypes())  # Python's own table only, as in the image
    main.register_web_types()
    r = client.get("/static/fonts/atkinson-hyperlegible-next-latin-wght-normal.woff2",
                   headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2"
    assert "content-encoding" not in r.headers  # already compressed: sent as is
    assert mimetypes.guess_type("x.woff")[0] == "font/woff"


# ---------------------------------------------------------------- PRIV-02 posts out of range


def test_posts_outside_your_range_are_not_found(client, db):
    """PRIV-02 regression: GET /api/posts/{id} had no range check, so a
    member 1,121 miles away read every post by its sequential id —
    including a grid-precision member's name and subsquare."""
    author = member(db, "KL7HPA")
    near = member(db, "KL7HPN", ANCHORAGE[0] + 0.05, ANCHORAGE[1])
    far = member(db, "KL7HPF", *FAIRBANKS)
    nowhere = member(db, "KL7HPX", None, None)
    p = client.post("/api/posts", headers=hdr(author), json={"title": "Swap meet"}).json()
    url = f"/api/posts/{p['id']}"
    assert client.get(url, headers=hdr(author)).status_code == 200
    assert client.get(url, headers=hdr(near)).status_code == 200
    for outsider in (far, nowhere):
        h = hdr(outsider)
        assert client.get(url, headers=h).status_code == 404            # old code: 200
        assert client.post(url + "/comments", headers=h, json={"body": "hi"}).status_code == 404
        assert client.patch(url, headers=h, json={"title": "x"}).status_code == 404
        assert client.delete(url, headers=h).status_code == 404
    assert client.patch(url, headers=hdr(near), json={"title": "x"}).status_code == 403
    # a member whose range reaches it reads it
    far.range_miles = 500
    db.commit()
    assert client.get(url, headers=hdr(far)).status_code == 200
    assert client.delete(url, headers=hdr(author)).status_code == 204


def test_nearby_range_is_capped_at_the_widest_setting(client, db):
    me = member(db, "KL7HNR")
    distant = member(db, "KL7HND", ANCHORAGE[0] + 9.0, ANCHORAGE[1])  # ~620 mi north
    r = client.get("/api/operators/nearby", params={"range": 1000}, headers=hdr(me))
    assert r.status_code == 200
    assert "KL7HND" not in {m["callsign"] for m in r.json()}  # old code: listed (le=1000)
    distant.lat = ANCHORAGE[0] + 6.0  # ~415 mi
    db.commit()
    r = client.get("/api/operators/nearby", params={"range": 1000}, headers=hdr(me))
    assert "KL7HND" in {m["callsign"] for m in r.json()}


# ---------------------------------------------------------------- V3-F1 SQLite journal mode


def test_the_database_is_in_wal_mode(client):
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"


def test_a_long_read_does_not_lock_out_writers(client, db):
    """V3-F1 regression: in rollback-journal mode the importer's 13 s read of
    the hams table made every save in the app wait 5 s and fail with
    'database is locked' (HTTP 500)."""
    u = member(db, "KL7HWL")
    path = get_settings().database_url.split("///", 1)[-1]
    reader = sqlite3.connect(path, timeout=1, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM users").fetchone()  # holds its read snapshot
        t0 = time.monotonic()
        r = client.patch("/api/me", headers=hdr(u), json={"name": "Written During A Read"})
        took = time.monotonic() - t0
    finally:
        reader.execute("ROLLBACK")
        reader.close()
    assert r.status_code == 200 and took < 3, (r.status_code, took)


def test_sqlite_wal_can_be_switched_off(tmp_path, monkeypatch):
    """SQLITE_WAL=false (a data dir on a network or FUSE filesystem) puts the
    file back in rollback-journal mode at the next start."""
    from sqlalchemy import create_engine

    from app.db import enable_wal

    eng = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    assert enable_wal(eng) == "wal"
    monkeypatch.setattr(get_settings(), "sqlite_wal", False)
    assert enable_wal(eng) == "delete"
    eng.dispose()


def test_client_address_uses_the_trusted_header_only_when_configured(monkeypatch):
    """Behind Cloudflare every request arrives from the tunnel's address; with
    TRUSTED_IP_HEADER set the throttles key on the visitor instead. Without
    the setting the header is ignored (it could be forged)."""
    from starlette.requests import Request

    def req(headers):
        return Request({"type": "http", "method": "GET", "path": "/", "client": ("172.18.0.1", 5000),
                        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})

    s = get_settings()
    monkeypatch.setattr(s, "trusted_ip_header", "")
    assert ratelimit.client_address(req({"CF-Connecting-IP": "203.0.113.7"})) == "172.18.0.1"
    monkeypatch.setattr(s, "trusted_ip_header", "CF-Connecting-IP")
    assert ratelimit.client_address(req({"CF-Connecting-IP": "203.0.113.7"})) == "203.0.113.7"
    assert ratelimit.client_address(req({"CF-Connecting-IP": " 203.0.113.8, 198.51.100.2"})) == "203.0.113.8"
    assert ratelimit.client_address(req({})) == "172.18.0.1"  # header missing: connecting address
