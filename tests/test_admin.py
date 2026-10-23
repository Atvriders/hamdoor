"""Admins (ADMIN_CALLSIGNS): who is one, is_admin and must_change_password on
the member's own profile, the admin endpoints (list members, reset a
password, unlock a log in, delete an account), the privilege checks in front
of every one of them, the reset -> log in -> change flow and the users-table
migration.

Members are NP2xxx callsigns (US Virgin Islands), which no other module uses;
each test starts and ends without any. No network."""

import logging
import re
import sqlite3

import httpx
import pytest
from sqlalchemy import create_engine, delete, or_, select

from app import ratelimit
from app.config import get_settings
from app.integrations import geocode, uls
from app.models import Comment, Ham, Post, User
from app.security import (TEMP_PASSWORD_ALPHABET, TEMP_PASSWORD_LENGTH, create_token, is_admin,
                          hash_password, temporary_password, verify_password)
from tests.conftest import auth

PW = "password123"
_PW_HASH = hash_password(PW)
ADMIN, ADMIN2, MEMBER, OTHER = "NP2ADM", "NP2ADB", "NP2MBR", "NP2OTH"


# ---------------------------------------------------------------- fixtures and helpers


def _purge(db) -> None:
    ids = db.scalars(select(User.id).where(User.callsign.like("NP2%"))).all()
    if ids:
        own_posts = select(Post.id).where(Post.author_id.in_(ids))
        db.execute(delete(Comment).where(or_(Comment.author_id.in_(ids), Comment.post_id.in_(own_posts))))
        db.execute(delete(Post).where(Post.author_id.in_(ids)))
        db.execute(delete(User).where(User.id.in_(ids)))
    db.execute(delete(Ham).where(Ham.callsign.like("NP2%")))
    db.commit()
    db.expire_all()


@pytest.fixture(autouse=True)
def _clean(db, monkeypatch):
    """No NP2 member before or after each test, no network, and the admins
    NP2ADM and NP2ADB (listed in mixed case with a comma and spaces)."""
    def no_network(self, request):
        raise AssertionError(f"network access in a test: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    monkeypatch.setattr(get_settings(), "admin_callsigns", " np2adm,  NP2ADB ")
    _purge(db)
    yield
    _purge(db)
    ratelimit.reset_all()


@pytest.fixture()
def throttled(monkeypatch):
    """The log-in throttles on (conftest switches them off for every other test)."""
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


@pytest.fixture()
def logs():
    """Every record logged under app.* while the test runs."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler, app_log = Keep(level=logging.DEBUG), logging.getLogger("app")
    app_log.addHandler(handler)
    yield records
    app_log.removeHandler(handler)


def member(db, callsign, **fields) -> User:
    u = User(**{"callsign": callsign, "password_hash": _PW_HASH, "email": f"{callsign.lower()}@example.com",
                "name": f"Name {callsign}", "city": "Charlotte Amalie", "state": "VI", **fields})
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def hdr(user: User) -> dict:
    return auth(create_token(user))


def login(client, callsign, password=PW):
    return client.post("/api/auth/login", json={"callsign": callsign, "password": password})


def admin_lines(records) -> list[str]:
    return [r.getMessage() for r in records if r.name == "app.routes.admin" and r.levelno == logging.INFO]


def listing(client, admin, q="NP2", **params):
    r = client.get("/api/admin/users", headers=hdr(admin), params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


def lock_out(client, callsign):
    """Five wrong passwords: the sixth try is refused (429) for 5 minutes."""
    for _ in range(5):
        assert login(client, callsign, "wrong-password").status_code == 401
    assert login(client, callsign, "wrong-password").status_code == 429


ENDPOINTS = [
    ("GET", "/api/admin/users"),
    ("POST", f"/api/admin/users/{MEMBER}/reset-password"),
    ("POST", f"/api/admin/users/{MEMBER}/unlock"),
    ("DELETE", f"/api/admin/users/{MEMBER}"),
]


# ---------------------------------------------------------------- who is an admin


@pytest.mark.parametrize("setting, admins", [
    ("", set()),
    ("   ", set()),
    ("NP2ADM", {ADMIN}),
    ("np2adm", {ADMIN}),
    ("NP2ADM,NP2ADB", {ADMIN, ADMIN2}),
    ("NP2ADM, NP2ADB", {ADMIN, ADMIN2}),
    ("NP2ADM NP2ADB", {ADMIN, ADMIN2}),
    (" ,np2adm,,\tNP2ADB\n", {ADMIN, ADMIN2}),
    ("NP2ADMX, NP2AD", set()),  # whole callsigns only
])
def test_admins_are_the_callsigns_in_admin_callsigns(setting, admins, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_callsigns", setting)
    for call in (ADMIN, ADMIN2, MEMBER):
        assert is_admin(User(callsign=call)) is (call in admins), (setting, call)
    assert is_admin(None) is False
    assert is_admin(User(callsign="")) is False


def test_startup_names_the_admins_and_warns_about_unclaimed_callsigns(db, logs, monkeypatch):
    """Sign up only checks that a callsign is in FCC records, so a listed
    callsign nobody has signed up with yet would make whoever takes it first
    an admin: the startup log says so."""
    import inspect

    from app import main
    from app.routes import admin as admin_routes

    member(db, ADMIN)
    assert admin_routes.announce_admins(db) == [ADMIN2]
    lines = [(r.levelno, r.getMessage()) for r in logs if r.name == "app.routes.admin"]
    assert lines[0] == (logging.INFO, f"[admin] admins (ADMIN_CALLSIGNS): {ADMIN2}, {ADMIN}")
    assert lines[1][0] == logging.WARNING and lines[1][1].startswith(f"[admin] {ADMIN2} is in ADMIN_CALLSIGNS but has "
                                                                     "no account here yet")
    assert len(lines) == 2
    monkeypatch.setattr(get_settings(), "admin_callsigns", "")
    assert admin_routes.announce_admins(db) == [] and len(logs) == 2  # no admins: nothing to say
    assert "admin.announce_admins(db)" in inspect.getsource(main.lifespan)


def test_the_profile_says_who_is_an_admin(client, db, monkeypatch, logs):
    """UserSelf.is_admin and must_change_password on /api/me, log in, PATCH
    /api/me, the password change and sign up (which logs that an admin
    callsign was taken)."""
    admin, plain = member(db, ADMIN), member(db, MEMBER)
    for u, want in ((admin, True), (plain, False)):
        me = client.get("/api/me", headers=hdr(u)).json()
        assert (me["is_admin"], me["must_change_password"]) == (want, False)
        assert login(client, u.callsign).json()["user"]["is_admin"] is want
        assert client.patch("/api/me", headers=hdr(u), json={"range_miles": 30}).json()["is_admin"] is want
        r = client.post("/api/me/password", headers=hdr(u), json={"current_password": PW, "new_password": PW})
        assert r.status_code == 200 and r.json()["user"]["is_admin"] is want

    # sign up (the callsign is in the local FCC directory): an admin from the first response
    monkeypatch.setattr(geocode, "resolve_location", lambda *a: None)
    db.add(Ham(callsign="NP2SGN", name="SAM SIGNUP", street="1 TEST ST", city="CHRISTIANSTED",
               state="VI", zip="00820", license_class="G", expires="01/01/2030"))
    db.commit()
    monkeypatch.setattr(get_settings(), "admin_callsigns", "NP2ADM NP2SGN")
    r = client.post("/api/auth/signup", json={"callsign": "np2sgn", "password": PW, "email": "s@example.com"})
    assert r.status_code == 201, r.text
    assert (r.json()["user"]["is_admin"], r.json()["user"]["must_change_password"]) == (True, False)
    assert [r.getMessage() for r in logs if r.name == "app.routes.auth"] == \
        ["[admin] NP2SGN signed up and is an admin (ADMIN_CALLSIGNS)"]


def test_admin_rights_follow_the_setting(client, db, monkeypatch):
    """Being an admin is read from ADMIN_CALLSIGNS on every request: a member
    taken off the list loses the endpoints with the same token."""
    admin = member(db, ADMIN)
    token = hdr(admin)
    assert client.get("/api/admin/users", headers=token).status_code == 200
    monkeypatch.setattr(get_settings(), "admin_callsigns", "")
    r = client.get("/api/admin/users", headers=token)
    assert (r.status_code, r.json()) == (403, {"detail": "Admins only."})
    assert client.get("/api/me", headers=token).json()["is_admin"] is False


# ---------------------------------------------------------------- privilege checks, every endpoint


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_admin_endpoints_need_a_valid_token(client, db, method, path):
    member(db, MEMBER)
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers=auth("not-a-token")).status_code == 401


@pytest.mark.parametrize("method, path", ENDPOINTS + [
    # an unknown callsign, a bad limit and an over-long query answer 403 too: no one but an
    # admin learns who has an account, or gets as far as validation
    ("POST", "/api/admin/users/NP2ZZZ/reset-password"),
    ("POST", "/api/admin/users/NP2ZZZ/unlock"),
    ("DELETE", "/api/admin/users/NP2ZZZ"),
    ("GET", "/api/admin/users?limit=nope"),
    ("GET", "/api/admin/users?q=" + "x" * 500),
])
def test_admin_endpoints_refuse_every_other_member(client, db, method, path):
    plain, target = member(db, OTHER), member(db, MEMBER)
    r = client.request(method, path, headers=hdr(plain))
    assert (r.status_code, r.json()) == (403, {"detail": "Admins only."})
    # and nothing happened to the target: same password, same account, no flag
    db.refresh(target)
    assert verify_password(PW, target.password_hash) and not target.must_change_password
    assert client.get("/api/me", headers=hdr(target)).status_code == 200


def test_an_admin_of_nothing_is_still_refused(client, db, monkeypatch):
    """No ADMIN_CALLSIGNS at all (the default): nobody is an admin."""
    monkeypatch.setattr(get_settings(), "admin_callsigns", "")
    admin = member(db, ADMIN)
    for method, path in ENDPOINTS:
        assert client.request(method, path, headers=hdr(admin)).status_code == 403


# ---------------------------------------------------------------- GET /api/admin/users


def test_the_member_list(client, db):
    admin = member(db, ADMIN)
    maria = member(db, MEMBER, name="María Núñez", email="maria@example.com", city="Christiansted")
    otto = member(db, OTHER, name="Otto Other", map_precision="grid")
    p1 = Post(author_id=maria.id, title="Antenna party", body="", category="general")
    p2 = Post(author_id=maria.id, title="For sale", body="", category="for-sale")
    p3 = Post(author_id=otto.id, title="Net tonight", body="", category="nets-events")
    db.add_all([p1, p2, p3])
    db.commit()
    db.add_all([Comment(post_id=p3.id, author_id=maria.id, body="See you there"),
                Comment(post_id=p1.id, author_id=otto.id, body="Count me in"),
                Comment(post_id=p1.id, author_id=otto.id, body="Bringing a mast")])
    db.commit()

    body = listing(client, admin)
    assert body["total"] == 3
    rows = body["users"]
    assert [u["callsign"] for u in rows] == [ADMIN, MEMBER, OTHER]  # sorted by callsign
    by = {u["callsign"]: u for u in rows}
    assert set(by[MEMBER]) == {"callsign", "name", "email", "city", "state", "created_at", "is_admin", "posts",
                               "comments", "map_precision", "must_change_password", "locked"}
    m = by[MEMBER]
    assert (m["name"], m["email"], m["city"], m["state"]) == ("María Núñez", "maria@example.com", "Christiansted", "VI")
    assert (m["posts"], m["comments"], m["is_admin"], m["map_precision"]) == (2, 1, False, "address")
    assert (m["must_change_password"], m["locked"]) == (False, False)
    assert m["created_at"].startswith(maria.created_at.strftime("%Y-%m-%dT%H:%M"))
    assert (by[OTHER]["posts"], by[OTHER]["comments"], by[OTHER]["map_precision"]) == (1, 2, "grid")
    assert (by[ADMIN]["posts"], by[ADMIN]["comments"], by[ADMIN]["is_admin"]) == (0, 0, True)


@pytest.mark.parametrize("q, found", [
    ("np2m", [MEMBER]),              # a callsign prefix, any case
    ("  NP2MBR  ", [MEMBER]),        # spaces around it don't count
    ("2MBR", []),                    # inside a callsign is not a prefix
    ("núñez", [MEMBER]),             # part of a name, any case, Unicode too
    ("NÚÑEZ", [MEMBER]),
    ("ría", [MEMBER]),
    ("otto", [OTHER]),
    ("GROSSMANN", [OTHER]),          # full Unicode case folding: ß is ss
    ("großmann", [OTHER]),
    ("name", [ADMIN]),               # "Name NP2ADM"
    ("NP2", [ADMIN, MEMBER, OTHER]),
    ("%", []),                       # no SQL wildcards
    ("_", []),
    ("zzz", []),
])
def test_q_matches_a_callsign_prefix_or_part_of_a_name(client, db, q, found):
    admin = member(db, ADMIN)
    member(db, MEMBER, name="María Núñez")
    member(db, OTHER, name="Otto Großmann")
    body = listing(client, admin, q=q)
    assert [u["callsign"] for u in body["users"]] == found
    assert body["total"] == len(found)


def test_an_empty_q_lists_every_member_and_limit_is_clamped(client, db):
    admin = member(db, ADMIN)
    member(db, MEMBER)
    member(db, OTHER)
    everyone = len(db.scalars(select(User.callsign)).all())  # other modules' members too
    body = listing(client, admin, q="", limit=200)
    assert body["total"] == everyone
    calls = [u["callsign"] for u in body["users"]]
    assert calls == sorted(calls) and len(calls) == min(everyone, 200)
    assert len(listing(client, admin, q="")["users"]) == min(everyone, 50)  # the default limit
    for limit, size in ((1, 1), (2, 2), (0, 1), (-5, 1)):
        body = listing(client, admin, limit=limit)
        assert (len(body["users"]), body["total"]) == (size, 3), limit
    assert len(listing(client, admin, q="", limit=100000)["users"]) == min(everyone, 200)


def test_at_most_200_members_come_back(client, db):
    admin = member(db, ADMIN)
    db.add_all([User(callsign=f"NP2L{i:03d}", password_hash=_PW_HASH, email=f"l{i}@example.com", name=f"Lots {i}")
                for i in range(205)])
    db.commit()
    for limit, size in ((100000, 200), (201, 200), (200, 200), (None, 50)):
        body = listing(client, admin, q="np2l", **({} if limit is None else {"limit": limit}))
        assert (len(body["users"]), body["total"]) == (size, 205), limit
    assert body["users"][0]["callsign"] == "NP2L000" and body["users"][-1]["callsign"] == "NP2L049"


def test_locked_out_members_and_unlock(client, db, throttled, logs):
    admin, plain = member(db, ADMIN), member(db, MEMBER)
    member(db, OTHER)
    lock_out(client, MEMBER)
    assert login(client, MEMBER).status_code == 429  # even the right password waits
    by = {u["callsign"]: u["locked"] for u in listing(client, admin)["users"]}
    assert by == {ADMIN: False, MEMBER: True, OTHER: False}

    r = client.post(f"/api/admin/users/{MEMBER.lower()}/unlock", headers=hdr(admin))
    assert r.status_code == 204 and r.content == b""
    assert {u["callsign"]: u["locked"] for u in listing(client, admin)["users"]}[MEMBER] is False
    assert login(client, MEMBER).status_code == 200
    assert admin_lines(logs) == [f"[admin] {ADMIN} unlocked {MEMBER}"]
    db.refresh(plain)
    assert verify_password(PW, plain.password_hash)  # unlocking changes nothing else


def test_locked_is_false_while_the_throttles_are_off(client, db):
    """locked means "log in is refused right now": with RATE_LIMIT_ENABLED
    off nothing is, whatever the counters hold."""
    admin = member(db, ADMIN)
    member(db, MEMBER)
    for _ in range(6):
        assert login(client, MEMBER, "wrong-password").status_code == 401
    for _ in range(ratelimit.LOGIN_FAILS_SHORT.limit):
        ratelimit.LOGIN_FAILS_SHORT.add(MEMBER)
    assert not any(u["locked"] for u in listing(client, admin)["users"])
    assert login(client, MEMBER).status_code == 200  # and indeed it isn't refused
    assert client.post(f"/api/admin/users/{MEMBER}/unlock", headers=hdr(admin)).status_code == 204


def test_locked_counts_only_the_callsign_throttles(client, db, throttled, monkeypatch):
    """A busy address (20 attempts a minute) is not a locked member."""
    admin = member(db, ADMIN)
    member(db, MEMBER)
    for _ in range(ratelimit.LOGIN_PER_ADDRESS.limit):
        ratelimit.LOGIN_PER_ADDRESS.add("testclient")
    assert login(client, MEMBER).status_code == 429
    assert {u["callsign"]: u["locked"] for u in listing(client, admin)["users"]}[MEMBER] is False


# ---------------------------------------------------------------- reset password


def test_reset_password(client, db, logs):
    admin, plain = member(db, ADMIN), member(db, MEMBER)
    before = hdr(plain)  # a session the member has open, e.g. on their phone
    assert client.get("/api/me", headers=before).status_code == 200

    r = client.post(f"/api/admin/users/{MEMBER.lower()}/reset-password", headers=hdr(admin))
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"callsign", "temporary_password"} and body["callsign"] == MEMBER
    temp = body["temporary_password"]
    assert len(temp) == 12 and set(temp) <= set(TEMP_PASSWORD_ALPHABET)

    # stored like any password (bcrypt), never in plain text; the flag is set
    db.refresh(plain)
    assert plain.password_hash.startswith("$2") and temp not in plain.password_hash
    assert verify_password(temp, plain.password_hash) and plain.must_change_password == 1
    # the token's password fingerprint no longer matches: every open session has ended
    assert client.get("/api/me", headers=before).status_code == 401
    assert login(client, MEMBER).status_code == 401  # the old password is gone
    r = login(client, MEMBER, temp)
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    assert client.get("/api/me", headers=auth(r.json()["token"])).json()["must_change_password"] is True
    assert {u["callsign"]: u["must_change_password"] for u in listing(client, admin)["users"]} == \
        {ADMIN: False, MEMBER: True}

    # one INFO line names the admin and the member; the password is in no log line at all
    assert admin_lines(logs) == [f"[admin] {ADMIN} reset the password of {MEMBER}"]
    assert not any(temp in r.getMessage() for r in logs)

    # each reset is a new password
    again = client.post(f"/api/admin/users/{MEMBER}/reset-password", headers=hdr(admin)).json()
    assert again["temporary_password"] != temp


def test_temporary_passwords_avoid_look_alike_characters():
    assert not set("0O1lI") & set(TEMP_PASSWORD_ALPHABET)
    assert TEMP_PASSWORD_LENGTH == 12
    seen = {temporary_password() for _ in range(300)}
    assert len(seen) == 300  # never repeats
    assert all(len(p) == 12 for p in seen)
    assert set("".join(seen)) <= set(TEMP_PASSWORD_ALPHABET)
    assert len(set("".join(seen))) > 40  # drawn from the whole alphabet


def test_a_reset_clears_the_lockout(client, db, throttled):
    admin = member(db, ADMIN)
    member(db, MEMBER)
    lock_out(client, MEMBER)
    temp = client.post(f"/api/admin/users/{MEMBER}/reset-password", headers=hdr(admin)).json()["temporary_password"]
    assert login(client, MEMBER, temp).status_code == 200  # not 429


def test_the_member_clears_must_change_password_by_changing_it(client, db):
    admin = member(db, ADMIN)
    member(db, MEMBER)
    temp = client.post(f"/api/admin/users/{MEMBER}/reset-password", headers=hdr(admin)).json()["temporary_password"]
    token = auth(login(client, MEMBER, temp).json()["token"])

    r = client.post("/api/me/password", headers=token, json={"current_password": "wrong-one", "new_password": "x" * 9})
    assert (r.status_code, r.json()["detail"]) == (400, "current password is incorrect")
    # the temporary password again is no change: the admin knows it
    r = client.post("/api/me/password", headers=token, json={"current_password": temp, "new_password": temp})
    assert r.status_code == 400 and "temporary" in r.json()["detail"]
    assert client.get("/api/me", headers=token).json()["must_change_password"] is True

    r = client.post("/api/me/password", headers=token, json={"current_password": temp, "new_password": "my-own-password"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["must_change_password"] is False
    fresh = auth(r.json()["token"])
    assert client.get("/api/me", headers=fresh).json()["must_change_password"] is False
    assert db.scalar(select(User.must_change_password).where(User.callsign == MEMBER)) == 0
    r = login(client, MEMBER, "my-own-password")
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is False
    assert login(client, MEMBER, temp).status_code == 401


def test_without_a_reset_the_same_password_may_be_set_again(client, db):
    """Unchanged behaviour for everyone else: "changing" to the same password
    is allowed (and ends the other sessions)."""
    plain = member(db, MEMBER)
    r = client.post("/api/me/password", headers=hdr(plain), json={"current_password": PW, "new_password": PW})
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is False


# ---------------------------------------------------------------- delete


def test_delete_a_members_account(client, db, logs):
    """The same cascade as DELETE /api/me: their posts with every comment on
    them, their comments elsewhere, the account. Everyone else's stays."""
    admin, gone, stays = member(db, ADMIN), member(db, MEMBER), member(db, OTHER)
    token = hdr(gone)
    theirs = Post(author_id=gone.id, title="Their post", body="", category="general")
    kept = Post(author_id=stays.id, title="Another post", body="", category="general")
    db.add_all([theirs, kept])
    db.commit()
    db.add_all([Comment(post_id=theirs.id, author_id=stays.id, body="a reply on their post"),
                Comment(post_id=theirs.id, author_id=gone.id, body="their reply on their post"),
                Comment(post_id=kept.id, author_id=gone.id, body="their reply elsewhere"),
                Comment(post_id=kept.id, author_id=stays.id, body="a reply that stays")])
    db.commit()
    theirs_id, kept_id, gone_id = theirs.id, kept.id, gone.id

    r = client.delete(f"/api/admin/users/{MEMBER.lower()}", headers=hdr(admin))
    assert r.status_code == 204 and r.content == b""
    db.expire_all()
    assert db.scalar(select(User.id).where(User.callsign == MEMBER)) is None
    assert db.scalars(select(Post.id).where(Post.id.in_([theirs_id, kept_id]))).all() == [kept_id]
    assert db.scalars(select(Comment.body).where(Comment.post_id == theirs_id)).all() == []
    assert db.scalars(select(Comment.body).where(Comment.post_id == kept_id)).all() == ["a reply that stays"]
    assert db.scalars(select(Comment.id).where(Comment.author_id == gone_id)).all() == []
    assert client.get("/api/me", headers=token).status_code == 401
    assert [u["callsign"] for u in listing(client, admin)["users"]] == [ADMIN, OTHER]
    assert admin_lines(logs) == [f"[admin] {ADMIN} deleted the account {MEMBER}"]
    assert client.delete(f"/api/admin/users/{MEMBER}", headers=hdr(admin)).status_code == 404


def test_an_admin_cannot_delete_their_own_account_here(client, db, logs):
    admin = member(db, ADMIN)
    for call in (ADMIN, ADMIN.lower()):
        r = client.delete(f"/api/admin/users/{call}", headers=hdr(admin))
        assert (r.status_code, r.json()) == (400, {"detail": "Use You → Delete account to delete your own account."})
    assert client.get("/api/me", headers=hdr(admin)).status_code == 200
    assert admin_lines(logs) == []


def test_an_admin_can_manage_another_admin(client, db):
    """Unlock and reset work on another admin as on anyone. Deleting their
    account doesn't (test_another_admins_account_is_not_deleted)."""
    admin, other_admin = member(db, ADMIN), member(db, ADMIN2)
    assert client.post(f"/api/admin/users/{ADMIN2}/unlock", headers=hdr(admin)).status_code == 204
    r = client.post(f"/api/admin/users/{ADMIN2}/reset-password", headers=hdr(admin))
    assert r.status_code == 200
    assert client.get("/api/me", headers=hdr(other_admin)).status_code == 401  # the reset ended their session
    assert login(client, ADMIN2, r.json()["temporary_password"]).json()["user"]["is_admin"] is True


# an admin's account is never deleted while its callsign is in ADMIN_CALLSIGNS (review R1)

ADMIN_KEEP = "An admin's account can't be deleted. Take {} out of ADMIN_CALLSIGNS and restart hamdoor first."


def _in_fcc_records(db, call) -> None:
    """The callsign in the local FCC directory, so sign up accepts it (the fake provider has no NP2 callsigns)."""
    db.add(Ham(callsign=call, name="SOME ONE", street="1 TEST ST", city="CHRISTIANSTED", state="VI",
               zip="00820", license_class="G", expires="01/01/2030"))
    db.commit()


def _stranger_signs_up(client, call):
    return client.post("/api/auth/signup", json={"callsign": call, "password": "stranger-pass-1",
                                                 "email": "stranger@example.com"})


def test_another_admins_account_is_not_deleted(client, db, monkeypatch, logs):
    """Admin rights go with a callsign in ADMIN_CALLSIGNS, and sign up checks
    only FCC records. Had NP2ADM deleted NP2ADB's account, the next person to
    sign up as NP2ADB would have been an admin at once, able to reset
    NP2ADM's password and log in as them. The delete is refused: the account,
    its session and the callsign stay."""
    monkeypatch.setattr(geocode, "resolve_location", lambda *a: None)
    _in_fcc_records(db, ADMIN2)
    admin, other_admin = member(db, ADMIN), member(db, ADMIN2)
    for call in (ADMIN2, ADMIN2.lower()):
        r = client.delete(f"/api/admin/users/{call}", headers=hdr(admin))
        assert (r.status_code, r.json()) == (400, {"detail": ADMIN_KEEP.format(ADMIN2)})
    assert client.get("/api/me", headers=hdr(other_admin)).status_code == 200
    assert admin_lines(logs) == []
    r = _stranger_signs_up(client, ADMIN2)
    assert (r.status_code, r.json()["detail"]) == (409, "that callsign is already registered")

    # out of ADMIN_CALLSIGNS (and the server restarted) it's an ordinary account: it can go, and whoever
    # signs up with the callsign afterwards is no admin
    monkeypatch.setattr(get_settings(), "admin_callsigns", ADMIN)
    assert client.delete(f"/api/admin/users/{ADMIN2}", headers=hdr(admin)).status_code == 204
    assert admin_lines(logs) == [f"[admin] {ADMIN} deleted the account {ADMIN2}"]
    r = _stranger_signs_up(client, ADMIN2)
    assert r.status_code == 201 and r.json()["user"]["is_admin"] is False
    assert client.get("/api/admin/users", headers=auth(r.json()["token"])).status_code == 403


def test_an_admin_cannot_delete_their_own_account_while_listed(client, db, monkeypatch):
    """The other way in: You -> Delete account (DELETE /api/me) by a listed
    admin would leave the callsign open the same way. It's refused before the
    password is checked, so the answer can't read as a wrong password. Taken
    off the list, the account can go."""
    monkeypatch.setattr(geocode, "resolve_location", lambda *a: None)
    _in_fcc_records(db, ADMIN2)
    member(db, ADMIN)
    me = member(db, ADMIN2)
    for password in (PW, "wrong-password"):
        r = client.request("DELETE", "/api/me", headers=hdr(me), json={"password": password})
        assert (r.status_code, r.json()) == (400, {"detail": ADMIN_KEEP.format(ADMIN2)})
    assert client.get("/api/me", headers=hdr(me)).status_code == 200
    assert _stranger_signs_up(client, ADMIN2).status_code == 409

    monkeypatch.setattr(get_settings(), "admin_callsigns", ADMIN)
    r = client.request("DELETE", "/api/me", headers=hdr(me), json={"password": "wrong-password"})
    assert (r.status_code, r.json()) == (400, {"detail": "password is incorrect"})
    assert client.request("DELETE", "/api/me", headers=hdr(me), json={"password": PW}).status_code == 204
    r = _stranger_signs_up(client, ADMIN2)
    assert r.status_code == 201 and r.json()["user"]["is_admin"] is False


def test_every_account_deletion_refuses_an_admin(db):
    """A tripwire: app.routes.users.delete_account is the one place in app/
    that deletes accounts, and it refuses an admin's account itself, so a
    way to delete accounts added later can't reopen a listed callsign
    either."""
    import inspect
    from pathlib import Path

    from fastapi import HTTPException

    from app.routes.users import delete_account

    app_dir = Path(__file__).resolve().parent.parent / "app"
    deletes = re.compile(r"delete\(\s*User\b|DELETE\s+FROM\s+users\b|\.delete\(\s*(user|member|account)\b", re.I)
    hits = [f"{p.relative_to(app_dir).as_posix()}: {line.strip()}" for p in sorted(app_dir.rglob("*.py"))
            for line in p.read_text(encoding="utf-8").splitlines() if deletes.search(line)]
    assert len(hits) == 1 and hits[0].startswith("routes/users.py: "), hits
    assert hits[0].split(": ", 1)[1] in inspect.getsource(delete_account)

    admin = member(db, ADMIN)
    with pytest.raises(HTTPException) as refused:
        delete_account(db, admin)
    assert (refused.value.status_code, refused.value.detail) == (400, ADMIN_KEEP.format(ADMIN))
    db.expire_all()
    assert db.scalar(select(User.id).where(User.callsign == ADMIN)) == admin.id


@pytest.mark.parametrize("method, action", [("POST", "/reset-password"), ("POST", "/unlock"), ("DELETE", "")])
@pytest.mark.parametrize("call", ["NP2ZZZ", "np2zzz", "AB", "N%21%21", "TOOLONGCALL9"])
def test_unknown_callsigns_are_404(client, db, logs, method, action, call):
    admin = member(db, ADMIN)
    r = client.request(method, f"/api/admin/users/{call}{action}", headers=hdr(admin))
    assert r.status_code == 404, r.text
    assert "no hamdoor account" in r.json()["detail"]
    assert admin_lines(logs) == []


def test_the_compose_file_offers_admin_callsigns():
    """docker-compose.yml is the whole configuration (README): it lists
    ADMIN_CALLSIGNS, blank (no admins) by default, and every key it sets is a
    real setting, so a mistyped one can't silently do nothing."""
    from pathlib import Path

    from app.config import Settings

    text = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
    assert re.search(r'^\s+ADMIN_CALLSIGNS: ""', text, re.M)
    keys = set(re.findall(r"^\s{6}([A-Z][A-Z0-9_]+):", text, re.M))
    assert "ADMIN_CALLSIGNS" in keys and keys <= {k.upper() for k in Settings.model_fields}
    assert Settings.model_fields["admin_callsigns"].default == ""


def test_the_you_view_carries_the_admin_copy():
    """The web app's side (static/js/views/you.js; driven end to end in a
    browser for the screenshots): the contract's sentences, the endpoints it
    calls, and the temporary password kept out of toasts and storage."""
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "views" / "you.js").read_text()
    for text in ("An admin reset your password. Choose a new one to keep using hamdoor.",
                 "Reset ${call}'s password?",
                 "They'll be logged out everywhere and must choose a new password at their next log in.",
                 "Give this to ${esc(call)}. They'll be asked to choose a new password when they log in.",
                 "${call} can log in again.", "Deleted ${call}'s account.", "Only admins can do this.",
                 "Must change password", "Locked out", "/api/admin/users", "/reset-password", "/unlock"):
        assert text in js, text
    assert "me().is_admin" in js and "must_change_password: false" in js
    # the temporary password is shown by its dialog only: no admin toast mentions a password, nothing is stored
    admin_js = js.split("// ---------------------------------------------------------------- admin (")[1] \
        .split("// ---------------------------------------------------------------- focus targets")[0]
    toasts = re.findall(r"HD\.toast\(([^;]*)\);", admin_js)
    assert len(toasts) >= 4 and not any(re.search("password", t, re.I) for t in toasts), toasts
    assert "temporary_password" in admin_js and not re.search(r"HD\.store|localStorage|sessionStorage", admin_js)


def test_the_you_view_keeps_admin_accounts_and_fits_the_temporary_password():
    """The web app's side of two review fixes (both driven in a browser too).
    R1: an admin's account has no Delete account (another admin's line, and
    your own Account section) and says how to make it deletable instead. R2:
    the temporary password stays on one line, so one too wide for its box
    (twelve W, M or m) is shrunk to fit it, also after the window changes
    size, instead of running past the box."""
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "static"
    js = (static / "js" / "views" / "you.js").read_text(encoding="utf-8")
    css = (static / "css" / "app.css").read_text(encoding="utf-8")
    assert "An admin's account can't be deleted. To delete ${yours ? \"yours\" : \"this one\"}, take ${call} out of " \
           "ADMIN_CALLSIGNS and restart hamdoor first." in js
    assert 'you || m.is_admin ? "" : btn("delete"' in js           # not on another admin's line
    assert "u.is_admin ? `<p class=\"hd-field__hint hd-account__note\" data-admin-keep>" in js  # nor in your Account
    assert "if (adminKept(e)) return adminKeepText(call, false);" in js  # a stale page explains the 400 too
    assert "To change your own password or delete your account" not in js

    dialog = js.split("function temporaryPasswordDialog(")[1].split("\n  }\n")[0]
    assert "fit();" in dialog and 'window.addEventListener("resize", fit);' in dialog
    assert 'window.removeEventListener("resize", fit);' in dialog
    assert "function fitPassword(v)" in js
    rule = re.search(r"\.hd-temppw__v \{([^}]*)\}", css).group(1)
    assert "white-space: nowrap" in rule  # one line: never wrapped in the middle of a password


def test_the_admin_endpoints_are_documented():
    from app.main import app

    paths = app.openapi()["paths"]
    for path, method in (("/api/admin/users", "get"), ("/api/admin/users/{callsign}/reset-password", "post"),
                         ("/api/admin/users/{callsign}/unlock", "post"), ("/api/admin/users/{callsign}", "delete")):
        assert paths[path][method]["tags"] == ["admin"], path


# ---------------------------------------------------------------- migration


def test_migrate_users_table_adds_must_change_password(tmp_path):
    """A users table from before must_change_password gains it as INTEGER NOT
    NULL DEFAULT 0: nobody already on the server is asked for a new password.
    Idempotent."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, callsign TEXT, map_precision TEXT NOT NULL"
                " DEFAULT 'address', location_source TEXT)")
    con.execute("INSERT INTO users (callsign) VALUES ('NP2OLD')")
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}")
    uls.migrate_users_table(eng)
    uls.migrate_users_table(eng)  # a second start changes nothing
    con = sqlite3.connect(path)
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(users)")}
    assert (cols["must_change_password"][2], cols["must_change_password"][3],
            cols["must_change_password"][4]) == ("INTEGER", 1, "0")
    assert con.execute("SELECT must_change_password FROM users").fetchall() == [(0,)]
    con.execute("INSERT INTO users (callsign) VALUES ('NP2NEW')")
    assert con.execute("SELECT must_change_password FROM users WHERE callsign = 'NP2NEW'").fetchone() == (0,)
    con.close()


def test_migrate_users_table_from_the_first_schema_adds_every_column(tmp_path):
    path = tmp_path / "older.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, callsign TEXT, grid TEXT, lat FLOAT, lon FLOAT)")
    con.execute("INSERT INTO users (callsign, grid) VALUES ('NP2OLD', '')")
    con.commit()
    con.close()
    uls.migrate_users_table(create_engine(f"sqlite:///{path}"))
    con = sqlite3.connect(path)
    assert con.execute("SELECT map_precision, location_source, must_change_password FROM users").fetchall() == \
        [("address", None, 0)]
    con.close()
