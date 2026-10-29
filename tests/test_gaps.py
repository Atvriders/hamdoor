"""Backend gaps G1b–G10: posts under map precision, member and author
badges, where a member's pin came from, events, account deletion, licences
within a radius, the feed's author filter and the lookup's grant date.

Members live around Reno, NV and use KG7* callsigns, so nothing another
module writes is in their range. FCC rows and ZIP anchors a test adds are
removed afterwards (the post-import sync tests elsewhere count every user
they move). No network."""

import math
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import jwt
import pytest
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.db import engine
from app.geo import grid_to_latlon, haversine_miles
from app.integrations import geocode, uls
from app.integrations.geocode import latlon_to_grid
from app.integrations.provider import CallsignProvider, CallsignRecord, get_provider
from app.main import app
from app.models import Comment, Ham, Post, User
from app.routes import hams as hams_route
from app.routes import users as users_route
from app.security import create_token, hash_password
from tests.conftest import auth

TODAY = date(2026, 10, 20)
_PW_HASH = hash_password("password123")  # bcrypt is slow: hash once


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """No network (Nominatim answers nothing unless a test says so), data
    files in tmp_path, a fixed 'today' for the new-ham window."""
    monkeypatch.setattr(uls, "_data_dir", lambda: str(tmp_path))

    def no_network(self, request):
        raise AssertionError(f"network access in a test: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    monkeypatch.setattr(hams_route, "_today", lambda: TODAY)


def mdy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


class _Fcc:
    """FCC directory rows and ZIP anchors for one test (removed afterwards)."""

    def __init__(self, db):
        self.db, self.calls, self.zips = db, [], []

    def ham(self, callsign, lat=None, lon=None, loc="address", cls="E", granted=None,
            street="1 A ST", city="Reno", state="NV", zip_="89501"):
        self.db.query(Ham).filter_by(callsign=callsign).delete()
        self.db.add(Ham(callsign=callsign, name=f"Name {callsign}", street=street, city=city, state=state,
                        zip=zip_, license_class=cls, expires="01/01/2035",
                        granted=granted if granted is not None else mdy(TODAY - timedelta(days=4000)),
                        lat=lat, lon=lon, loc_source=loc if lat is not None else ""))
        self.db.commit()
        self.calls.append(callsign)

    def anchor(self, zip_, lat, lon):
        uls.migrate_hams_table()
        with engine.begin() as conn:
            conn.execute(text("INSERT OR REPLACE INTO zip_anchors VALUES (:z, :lat, :lon, 'median', 5)"),
                         {"z": zip_, "lat": lat, "lon": lon})
        self.zips.append(zip_)

    def cleanup(self):
        self.db.rollback()
        self.db.query(Ham).filter(Ham.callsign.in_(self.calls)).delete(synchronize_session=False)
        self.db.commit()
        with engine.begin() as conn:
            for z in self.zips:
                conn.execute(text("DELETE FROM zip_anchors WHERE zip = :z"), {"z": z})


@pytest.fixture()
def fcc(db):
    f = _Fcc(db)
    yield f
    f.cleanup()


def member(db, callsign, lat, lon, grid="", precision="address", **profile) -> User:
    """A member placed directly (no signup), password 'password123'."""
    u = db.query(User).filter_by(callsign=callsign).one_or_none()
    if u is None:
        u = User(callsign=callsign, password_hash=_PW_HASH, email=f"{callsign.lower()}@example.com",
                 name=f"Name {callsign}")
        db.add(u)
    u.lat, u.lon, u.grid, u.map_precision = lat, lon, grid, precision
    for key, value in profile.items():
        setattr(u, key, value)
    db.commit()
    return u


def hdr(user: User) -> dict:
    return auth(create_token(user))


def new_post(client, user, **fields) -> dict:
    r = client.post("/api/posts", headers=hdr(user),
                    json={"title": fields.pop("title", "post"), "category": fields.pop("category", "general"),
                          **fields})
    assert r.status_code == 201, r.text
    return r.json()


def feed(client, user, **params) -> list[dict]:
    r = client.get("/api/posts/feed", params=params, headers=hdr(user))
    assert r.status_code == 200, r.text
    return r.json()


def subsquare_centre(lat, lon):
    return grid_to_latlon(latlon_to_grid(lat, lon, 6))


def stored_points(db, ids) -> set:
    db.expire_all()
    return {(p.lat, p.lon) for p in db.query(Post).filter(Post.id.in_(ids))}


# ---------------------------------------------------------------- G1c posts under map precision


def test_a_grid_members_post_never_stores_or_returns_their_exact_point(client, db):
    """G1c regression: posts.py copied user.lat/lon into every post, so a
    'grid' member's exact point was stored and sent to everyone in range,
    with a one-decimal distance."""
    exact = (39.5296, -119.8138)
    g = member(db, "KG7GRA", *exact, precision="grid")
    viewer = member(db, "KG7VWA", 39.60, -119.75)
    centre = subsquare_centre(*exact)
    created = new_post(client, g, title="QRP meetup")
    assert (created["lat"], created["lon"]) == centre                    # old: the exact point
    assert created["author_grid"] == latlon_to_grid(*exact, 6)
    assert stored_points(db, [created["id"]]) == {centre}                # old: stored exact
    r_feed = client.get("/api/posts/feed", headers=hdr(viewer))
    (mine,) = [p for p in r_feed.json() if p["id"] == created["id"]]
    assert (mine["lat"], mine["lon"]) == centre
    assert mine["distance_miles"] == float(round(haversine_miles(39.60, -119.75, *centre)))  # whole miles
    r_one = client.get(f"/api/posts/{created['id']}", headers=hdr(viewer))
    r_edit = client.patch(f"/api/posts/{created['id']}", headers=hdr(g), json={"title": "QRP meetup moved"})
    for r in (r_one, r_edit):
        assert (r.json()["lat"], r.json()["lon"]) == centre
        assert r.json()["distance_miles"].is_integer()
    for body in (r_feed.text, r_one.text, r_edit.text):
        assert "39.5296" not in body and "-119.8138" not in body
    # comments carry no location at all
    c = client.post(f"/api/posts/{created['id']}/comments", headers=hdr(g), json={"body": "see you"}).json()
    assert set(c) == {"id", "post_id", "author_callsign", "author_name", "body", "created_at"}


def test_signup_with_grid_precision_posts_at_the_subsquare(client, db, fcc):
    """The signup path: a member who signs up with 'grid' never stores their
    exact point in a post either."""
    fcc.ham("KG7SGP", 39.48, -119.79, loc="address")
    db.query(User).filter_by(callsign="KG7SGP").delete()
    db.commit()
    r = client.post("/api/auth/signup", json={"callsign": "KG7SGP", "password": "password123",
                                              "email": "kg7sgp@example.com", "map_precision": "grid"})
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    post = client.post("/api/posts", headers=auth(tok), json={"title": "hi", "category": "general"}).json()
    assert (post["lat"], post["lon"]) == subsquare_centre(39.48, -119.79)
    assert stored_points(db, [post["id"]]) == {subsquare_centre(39.48, -119.79)}


def test_switching_precision_resnaps_existing_posts_both_ways(client, db):
    """G1c, multi-call: to 'grid' every earlier post moves to the subsquare
    centre (stored and shown, whole-mile distance); back to 'address' they
    get the precise point again — twice over, without touching updated_at.
    A 'grid' member's posts then follow their shown point when it moves."""
    exact = (39.4000, -119.7000)
    a = member(db, "KG7SWA", *exact, grid="")
    viewer = member(db, "KG7VWB", 39.45, -119.75)
    ids = [new_post(client, a, title=f"p{i}")["id"] for i in range(2)]
    stamps = {p.id: p.updated_at for p in db.query(Post).filter(Post.id.in_(ids))}
    centre = subsquare_centre(*exact)

    def shown():
        return {(p["lat"], p["lon"], p["distance_miles"], p["author_grid"])
                for p in feed(client, viewer) if p["id"] in ids}

    assert stored_points(db, ids) == {exact}
    for _ in range(2):
        r = client.patch("/api/me", headers=hdr(a), json={"map_precision": "grid"})
        assert r.status_code == 200, r.text
        assert stored_points(db, ids) == {centre}                         # old: still exact
        assert shown() == {(*centre, float(round(haversine_miles(39.45, -119.75, *centre))),
                            latlon_to_grid(*exact, 6))}
        r = client.patch("/api/me", headers=hdr(a), json={"map_precision": "address"})
        assert r.status_code == 200, r.text
        assert stored_points(db, ids) == {exact}
        assert shown() == {(*exact, round(haversine_miles(39.45, -119.75, *exact), 1), "")}
    db.expire_all()
    assert {p.id: p.updated_at for p in db.query(Post).filter(Post.id.in_(ids))} == stamps  # not edits
    # in 'grid', a new grid square moves the posts with the member's shown point
    client.patch("/api/me", headers=hdr(a), json={"map_precision": "grid"})
    r = client.patch("/api/me", headers=hdr(a), json={"grid": "DM09ab"})
    assert r.status_code == 200
    assert stored_points(db, ids) == {grid_to_latlon("DM09ab")}
    client.patch("/api/me", headers=hdr(a), json={"map_precision": "address"})  # leave a default member


def test_startup_sync_resnaps_grid_members_posts_from_before_g1c(client, db):
    """Posts a 'grid' member wrote before posts honoured map precision hold
    their exact point: the API never returns it, and the user sync that
    runs at startup (and after every import) stores the centre instead."""
    exact = (39.3000, -119.6000)
    g = member(db, "KG7LEG", *exact, precision="grid")
    viewer = member(db, "KG7VWC", 39.31, -119.61)
    legacy = Post(author_id=g.id, title="legacy", body="", category="general", lat=exact[0], lon=exact[1])
    db.add(legacy)
    db.commit()
    centre = subsquare_centre(*exact)
    one = client.get(f"/api/posts/{legacy.id}", headers=hdr(viewer)).json()
    assert (one["lat"], one["lon"]) == centre                              # old: the exact point
    assert [p for p in feed(client, viewer) if p["id"] == legacy.id][0]["lat"] == centre[0]
    users_route.sync_user_locations(db)
    assert stored_points(db, [legacy.id]) == {centre}                      # old: still exact
    assert users_route.sync_user_locations(db) == 0 and stored_points(db, [legacy.id]) == {centre}


def test_post_import_sync_moves_a_grid_members_posts_with_them(db, fcc):
    """A 'grid' member without a grid of their own is shown at the
    subsquare of their stored point; when the post-import sync moves that
    point (a street geocode arrived), their posts follow."""
    g = member(db, "KG7SYG", 39.0, -119.0, precision="grid", address_line="5 B ST", city="Reno",
               state="NV", zip="89501")
    db.add(Post(author_id=g.id, title="t", body="", category="general", lat=39.0, lon=-119.0))
    db.commit()
    pid = db.query(Post).filter_by(author_id=g.id).one().id
    fcc.ham("KG7SYG", 39.52, -119.81, loc="address", street="5 B ST")
    geocode.sync_user_locations(db)
    db.expire_all()
    u = db.query(User).filter_by(callsign="KG7SYG").one()
    assert (u.lat, u.lon, u.location_source) == (39.52, -119.81, "licence")
    assert stored_points(db, [pid]) == {subsquare_centre(39.52, -119.81)}


def test_a_grid_members_long_locator_never_reaches_their_posts(client, db):
    """An 8-character grid (~0.5 x 0.3 mi) is more precise than 'grid'
    allows: posts carry the 6-character subsquare, never the long locator
    (author_grid used to be the profile grid as entered)."""
    g = member(db, "KG7GR8", 39.6512, -119.5523, grid="DM09qp45", precision="grid")
    post = new_post(client, g)
    assert post["author_grid"] == "DM09qp" and (post["lat"], post["lon"]) == grid_to_latlon("DM09qp")
    assert "DM09qp45" not in client.get(f"/api/posts/{post['id']}", headers=hdr(g)).text


def test_me_survives_an_unknown_location_source(client, db):
    u = member(db, "KG7JNK", 39.7, -119.7, location_source="hand-edited")
    assert client.get("/api/me", headers=hdr(u)).json()["location_source"] is None
    u = member(db, "KG7JNK", None, None, location_source="grid")  # no location: always null
    assert client.get("/api/me", headers=hdr(u)).json()["location_source"] is None


def test_a_grid_member_without_any_location_posts_nowhere(client, db):
    g = member(db, "KG7NOL", None, None, precision="grid")
    post = new_post(client, g)
    assert (post["lat"], post["lon"], post["distance_miles"]) == (None, None, None)
    assert stored_points(db, [post["id"]]) == {(None, None)}


def test_a_precision_toggle_without_a_location_never_loses_posts_for_good(client, db, monkeypatch):
    """B4-R1 regression, multi-call: an 'address' member whose address stops
    resolving keeps their posts' snapshot; toggling to 'grid' empties the
    posts (no point to show them at, and the snapshot is their exact point)
    and back to 'address' has nothing to restore yet. Once the address
    resolves again the posts get the member's point back. They used to stay
    NULL for good, out of every feed: an 'address' member's posts were only
    re-placed on a precision switch."""
    home = (39.25, -119.95)
    a = member(db, "KG7NUA", *home, grid="", address_line="1 Lake St", city="Reno", state="NV", zip="89599")
    viewer = member(db, "KG7NVA", 39.26, -119.96, range_miles=25)
    pid = new_post(client, a, title="antenna for sale")["id"]
    stamp = db.get(Post, pid).updated_at
    assert pid in {p["id"] for p in feed(client, viewer)}
    tok = hdr(a)

    def patch(body):
        r = client.patch("/api/me", headers=tok, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    assert patch({"address_line": "1 Lake Stt"})["lat"] is None          # a typo, or Nominatim down
    assert stored_points(db, [pid]) == {home}                             # an 'address' snapshot stays
    patch({"map_precision": "grid"})
    assert stored_points(db, [pid]) == {(None, None)}                     # nothing to show; exact point gone
    patch({"map_precision": "address"})
    assert stored_points(db, [pid]) == {(None, None)}                     # nothing to restore it with yet
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: home)
    me = patch({"address_line": "1 Lake St"})
    assert (me["lat"], me["lon"]) == home
    assert stored_points(db, [pid]) == {home}                             # old: (None, None) for good
    assert pid in {p["id"] for p in feed(client, viewer)}                 # old: in no feed
    assert db.get(Post, pid).updated_at == stamp                          # a re-placement, not an edit


def test_refresh_callsign_gives_an_address_members_unplaced_posts_their_point(client, db):
    """B4-R1: refresh-callsign locates a member again too. Their posts with
    no location (as the toggle above leaves them) get the new point; a post
    that has one keeps its snapshot, as an 'address' member's posts do."""
    u = member(db, "KG7NUB", None, None, grid="")
    db.add_all([Post(author_id=u.id, title="emptied", body="", category="general", lat=None, lon=None),
                Post(author_id=u.id, title="kept", body="", category="general", lat=39.11, lon=-119.11)])
    db.commit()
    ids = {p.title: p.id for p in db.query(Post).filter_by(author_id=u.id)}
    previous = app.dependency_overrides[get_provider]
    app.dependency_overrides[get_provider] = lambda: _Coords("KG7NUB")
    try:
        r = client.post("/api/me/refresh-callsign", headers=hdr(u))
    finally:
        app.dependency_overrides[get_provider] = previous
    assert (r.json()["lat"], r.json()["lon"], r.json()["location_source"]) == (39.4321, -119.6543, "provider")
    assert stored_points(db, [ids["emptied"]]) == {(39.4321, -119.6543)}  # old: (None, None)
    assert stored_points(db, [ids["kept"]]) == {(39.11, -119.11)}


def test_post_import_sync_gives_an_address_members_unplaced_posts_their_point(db, fcc):
    """B4-R1: so does the post-import sync, for the members it moves."""
    u = member(db, "KG7NUC", None, None, grid="", address_line="5 E ST", city="Reno", state="NV", zip="89501")
    db.add(Post(author_id=u.id, title="emptied", body="", category="general", lat=None, lon=None))
    db.commit()
    pid = db.query(Post).filter_by(author_id=u.id).one().id
    fcc.ham("KG7NUC", 39.52, -119.81, loc="address", street="5 E ST")     # a street geocode arrives
    geocode.sync_user_locations(db)
    db.expire_all()
    moved = db.query(User).filter_by(callsign="KG7NUC").one()
    assert (moved.lat, moved.lon, moved.location_source) == (39.52, -119.81, "licence")
    assert stored_points(db, [pid]) == {(39.52, -119.81)}                 # old: (None, None)


def test_switching_back_to_address_without_a_location_keeps_posts_where_they_show(client, db):
    """B4-R1: a 'grid' member can be shown at a subsquare with no stored
    point (an over-long locator such as "DM09ab4" does not resolve, and here
    neither does the address), and posts at its centre. Switching to
    'address' then has no precise point to restore: the posts stay at that
    centre, which was never a secret, instead of being emptied out of every
    feed."""
    g = member(db, "KG7NUD", 39.07, -119.93, precision="grid", address_line="1 Nowhere Rd", city="Reno",
               state="NV", zip="89599")
    tok = hdr(g)
    r = client.patch("/api/me", headers=tok, json={"grid": "DM09ab4"})
    assert r.status_code == 200 and (r.json()["lat"], r.json()["lon"]) == (None, None)
    centre = grid_to_latlon("DM09ab")
    pid = new_post(client, g, title="net tonight")["id"]
    assert stored_points(db, [pid]) == {centre}
    viewer = member(db, "KG7NVD", 39.07, -119.94, range_miles=25)
    assert pid in {p["id"] for p in feed(client, viewer)}
    r = client.patch("/api/me", headers=tok, json={"map_precision": "address"})
    assert r.status_code == 200 and r.json()["lat"] is None
    assert stored_points(db, [pid]) == {centre}                           # old: (None, None)
    assert pid in {p["id"] for p in feed(client, viewer)}


# ---------------------------------------------------------------- G1b + G2 nearby


def test_nearby_carries_map_precision_licence_class_and_new(client, db, fcc):
    """G1b/G2 regression: UserPublic had no map_precision, license_class or
    new, so the map had to infer precision and fetch every neighbour's FCC
    row."""
    viewer = member(db, "KG7VWD", 39.20, -119.50)
    member(db, "KG7NBA", 39.21, -119.51)
    member(db, "KG7NBB", 39.22, -119.52, precision="grid")
    member(db, "KG7NBC", 39.23, -119.53)                                   # no FCC row
    member(db, "KG7NBD", 39.24, -119.54)                                   # a club licence
    fcc.ham("KG7NBA", cls="E", granted=mdy(TODAY - timedelta(days=4000)))
    fcc.ham("KG7NBB", cls="T", granted=mdy(TODAY - timedelta(days=10)))
    fcc.ham("KG7NBD", cls="", granted=mdy(TODAY - timedelta(days=4000)))
    r = client.get("/api/operators/nearby", params={"range": 25}, headers=hdr(viewer))
    got = {o["callsign"]: o for o in r.json()}
    assert {k: got["KG7NBA"][k] for k in ("map_precision", "license_class", "new")} == \
        {"map_precision": "address", "license_class": "E", "new": False}
    assert {k: got["KG7NBB"][k] for k in ("map_precision", "license_class", "new")} == \
        {"map_precision": "grid", "license_class": "T", "new": True}
    # QA F5: a club ("") and a member the directory has no row for (null) are told apart
    assert {k: got["KG7NBC"][k] for k in ("map_precision", "license_class", "new")} == \
        {"map_precision": "address", "license_class": None, "new": False}
    assert {k: got["KG7NBD"][k] for k in ("map_precision", "license_class", "new")} == \
        {"map_precision": "address", "license_class": "", "new": False}
    assert (got["KG7NBB"]["lat"], got["KG7NBB"]["lon"]) == subsquare_centre(39.22, -119.52)


# ---------------------------------------------------------------- G3 post author badges


def test_posts_carry_the_authors_class_and_new_flag_everywhere(client, db, fcc):
    """G3 regression: PostOut had no author_class / author_new."""
    a = member(db, "KG7AUA", 39.10, -119.40)
    b = member(db, "KG7AUB", 39.11, -119.41)                               # no FCC row
    fcc.ham("KG7AUA", cls="G", granted=mdy(TODAY - timedelta(days=5)))
    created = new_post(client, a, title="new ham here")
    assert (created["author_class"], created["author_new"]) == ("G", True)
    other = new_post(client, b, title="no licence row")
    assert (other["author_class"], other["author_new"]) == (None, False)       # QA F5: not listed, not a club
    listed = {p["id"]: p for p in feed(client, b)}
    assert (listed[created["id"]]["author_class"], listed[created["id"]]["author_new"]) == ("G", True)
    assert (listed[other["id"]]["author_class"], listed[other["id"]]["author_new"]) == (None, False)
    club = member(db, "KG7AUC", 39.12, -119.42)
    fcc.ham("KG7AUC", cls="")
    assert new_post(client, club, title="club station")["author_class"] == ""  # a club licence
    one = client.get(f"/api/posts/{created['id']}", headers=hdr(b)).json()
    edited = client.patch(f"/api/posts/{created['id']}", headers=hdr(a), json={"body": "x"}).json()
    for p in (one, edited):
        assert (p["author_class"], p["author_new"]) == ("G", True)


# ---------------------------------------------------------------- G4 events


@pytest.mark.parametrize("bad", [
    "2026-10-28", "2026-10-28T20:00Z", "2026-10-28T20:00+02:00", "2026-10-28 20:00",
    "2026-02-30T20:00", "2026-10-28T24:00", "2026-10-28T20:60", "28/10/2026 20:00",
    "2026-10-28T20:00:00.5", "2026-10-28T8:00", "２０２６-10-28T20:00", "", "soon", 20261028, True,
])
def test_event_at_accepts_only_a_local_date_time(client, db, bad):
    a = member(db, "KG7EVB", 39.0, -119.9)
    r = client.post("/api/posts", headers=hdr(a), json={"title": "net", "event_at": bad})
    assert r.status_code == 422, (bad, r.text)


def test_events_on_create_update_and_feed(client, db):
    """G4, multi-call: event_at/repeats_weekly are stored, returned, changed,
    cleared with null (which also ends the repeat), and left alone when
    omitted; a weekly repeat needs an event time. Any category may carry
    one."""
    a = member(db, "KG7EVA", 38.90, -119.90)
    p = new_post(client, a, title="2 m net", category="nets-events", event_at="2026-10-28T20:00",
                 repeats_weekly=True)
    assert (p["event_at"], p["repeats_weekly"]) == ("2026-10-28T20:00", True)
    assert (new_post(client, a, title="swap meet", event_at=" 2026-11-07T09:30:45 ")["event_at"]) == \
        "2026-11-07T09:30"                                                  # seconds dropped
    plain = new_post(client, a, title="no event")
    assert (plain["event_at"], plain["repeats_weekly"]) == (None, False)
    assert client.post("/api/posts", headers=hdr(a),
                       json={"title": "x", "repeats_weekly": True}).status_code == 422
    row = db.get(Post, p["id"])
    db.refresh(row)
    assert (row.event_at, row.repeats_weekly) == ("2026-10-28T20:00", 1)

    def patch(body, expect=200):
        r = client.patch(f"/api/posts/{p['id']}", headers=hdr(a), json=body)
        assert r.status_code == expect, (body, r.text)
        return r.json()

    assert (patch({"title": "2 m net (ARES)"})["event_at"]) == "2026-10-28T20:00"   # omitted: kept
    assert patch({"event_at": "2026-11-04T19:30"})["repeats_weekly"] is True
    assert patch({"repeats_weekly": False})["repeats_weekly"] is False
    assert patch({"repeats_weekly": True})["event_at"] == "2026-11-04T19:30"
    cleared = patch({"event_at": None})
    assert (cleared["event_at"], cleared["repeats_weekly"]) == (None, False)   # the repeat ends too
    patch({"repeats_weekly": True}, 422)                                   # no time to repeat
    patch({"event_at": None, "repeats_weekly": True}, 422)
    patch({"repeats_weekly": None}, 422)
    patch({"event_at": "tomorrow"}, 422)
    both = patch({"event_at": "2026-12-02T20:00", "repeats_weekly": True, "category": "general"})
    assert (both["event_at"], both["repeats_weekly"], both["category"]) == ("2026-12-02T20:00", True, "general")
    listed = [x for x in feed(client, a) if x["id"] == p["id"]][0]
    assert (listed["event_at"], listed["repeats_weekly"]) == ("2026-12-02T20:00", True)


def test_migrate_posts_table_adds_the_event_columns(tmp_path):
    """A posts table from before events gains event_at (TEXT NULL) and
    repeats_weekly (INTEGER NOT NULL DEFAULT 0); existing posts have no
    event. Idempotent, and wired into the startup migration."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL, title VARCHAR(200),"
                " body TEXT, category VARCHAR(32), lat FLOAT, lon FLOAT, created_at DATETIME, updated_at DATETIME)")
    con.execute("INSERT INTO posts (author_id, title, body, category, lat, lon) VALUES (1, 't', '', 'general', 1, 2)")
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}")
    uls.migrate_posts_table(eng)
    uls.migrate_posts_table(eng)  # a second start changes nothing
    con = sqlite3.connect(path)
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(posts)")}
    assert (cols["event_at"][2], cols["event_at"][3]) == ("TEXT", 0)
    assert (cols["repeats_weekly"][2], cols["repeats_weekly"][3], cols["repeats_weekly"][4]) == ("INTEGER", 1, "0")
    assert con.execute("SELECT event_at, repeats_weekly FROM posts").fetchall() == [(None, 0)]
    con.execute("INSERT INTO posts (author_id, title, body, category) VALUES (1, 'u', '', 'general')")
    assert con.execute("SELECT repeats_weekly FROM posts WHERE title = 'u'").fetchone() == (0,)
    con.close()


def test_startup_migration_runs_the_users_and_posts_migrations(monkeypatch):
    ran = []
    monkeypatch.setattr(uls, "migrate_users_table", lambda bind=None: ran.append("users"))
    monkeypatch.setattr(uls, "migrate_posts_table", lambda bind=None: ran.append("posts"))
    uls.migrate_hams_table()
    assert ran == ["users", "posts"]


# ---------------------------------------------------------------- G1d location_source


class _Coords(CallsignProvider):
    """A callook-like answer: the licence address with Bing coordinates."""

    def __init__(self, callsign, lat=39.4321, lon=-119.6543):
        self.callsign, self.lat, self.lon = callsign, lat, lon

    def lookup(self, callsign):
        return CallsignRecord(callsign=self.callsign, name="Coords", address_line="9 C ST", city="Reno",
                              state="NV", zip="89502", grid="", lat=self.lat, lon=self.lon, source="fake")


def signup_as(client, db, callsign, provider=None, **fields) -> dict:
    db.query(User).filter_by(callsign=callsign).delete()
    db.commit()
    previous = app.dependency_overrides[get_provider]
    if provider is not None:
        app.dependency_overrides[get_provider] = lambda: provider
    try:
        r = client.post("/api/auth/signup", json={"callsign": callsign, "password": "password123",
                                                  "email": f"{callsign.lower()}@example.com", **fields})
    finally:
        app.dependency_overrides[get_provider] = previous
    assert r.status_code == 201, r.text
    return r.json()


def test_signup_says_where_the_pin_came_from(client, db, fcc, monkeypatch):
    """G1d: every step of the resolve order names itself."""
    fcc.ham("KG7LSA", 39.52, -119.81, loc="address")                       # street geocode
    assert signup_as(client, db, "KG7LSA")["user"]["location_source"] == "licence"
    assert signup_as(client, db, "KG7LSB", provider=_Coords("KG7LSB"))["user"]["location_source"] == "provider"
    fcc.ham("KG7LSC", 39.5, -119.8, loc="zip")
    u = signup_as(client, db, "KG7LSC", grid="DM09")["user"]
    assert (u["lat"], u["lon"], u["location_source"]) == (*grid_to_latlon("DM09"), "grid")
    fcc.ham("KG7LSD", None, None, zip_="89599")
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: (39.61, -119.71))
    assert signup_as(client, db, "KG7LSD")["user"]["location_source"] == "geocoded"
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    fcc.ham("KG7LSE", None, None, zip_="89598")
    fcc.anchor("89598", 39.55, -119.75)
    data = signup_as(client, db, "KG7LSE")
    assert (data["user"]["lat"], data["user"]["location_source"]) == (39.55, "zip")
    fcc.ham("KG7LSF", None, None, zip_="89597")                              # nothing to place them by
    nowhere = signup_as(client, db, "KG7LSF")["user"]
    assert (nowhere["lat"], nowhere["location_source"]) == (None, None)
    me = client.get("/api/me", headers=auth(data["token"])).json()
    assert me["location_source"] == "zip"
    login = client.post("/api/auth/login", json={"callsign": "KG7LSE", "password": "password123"})
    assert login.json()["user"]["location_source"] == "zip"


def test_profile_edits_refresh_and_sync_keep_location_source_current(client, db, fcc, monkeypatch):
    """G1d, multi-call: PATCH /api/me (address or grid), refresh-callsign and
    the post-import sync update the source with the point; other edits
    leave it alone."""
    fcc.ham("KG7PSA", 39.52, -119.81, loc="address")
    data = signup_as(client, db, "KG7PSA")
    tok = auth(data["token"])
    assert data["user"]["location_source"] == "licence"

    def patch(body):
        r = client.patch("/api/me", headers=tok, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    assert patch({"range_miles": 30})["location_source"] == "licence"
    assert patch({"map_precision": "grid"})["location_source"] == "licence"
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: (39.66, -119.66))
    moved = patch({"address_line": "77 Other St", "map_precision": "address"})
    assert (moved["lat"], moved["location_source"]) == (39.66, "geocoded")
    assert patch({"grid": "DM09ab"})["location_source"] == "grid"
    back = patch({"grid": "", "address_line": "1 A ST"})
    assert (back["lat"], back["lon"], back["location_source"]) == (39.52, -119.81, "licence")
    monkeypatch.setattr(geocode, "geocode_address", lambda *a: None)
    lost = patch({"address_line": "nowhere", "zip": "00000"})
    assert (lost["lat"], lost["location_source"]) == (None, None)
    # refresh-callsign: the provider's address and coordinates (no street geocode for it here)
    fcc.ham("KG7PSA", 39.5, -119.8, loc="zip", street="9 C ST", zip_="89502")
    previous = app.dependency_overrides[get_provider]
    app.dependency_overrides[get_provider] = lambda: _Coords("KG7PSA")
    try:
        r = client.post("/api/me/refresh-callsign", headers=tok)
    finally:
        app.dependency_overrides[get_provider] = previous
    assert (r.json()["lat"], r.json()["location_source"]) == (39.4321, "provider")
    # the post-import sync: a street geocode for the same address arrives
    fcc.ham("KG7PSA", 39.50001, -119.80001, loc="address", street="9 C ST", zip_="89502")
    users_route.sync_user_locations(db)
    me = client.get("/api/me", headers=tok).json()
    assert (me["lat"], me["location_source"]) == (39.50001, "licence")


def test_post_import_sync_names_grid_and_zip_placements(db, fcc):
    fcc.ham("KG7SZA", 39.40, -119.40, loc="zip", street="2 D ST", zip_="89596")
    fcc.ham("KG7SZB", 39.40, -119.40, loc="zip", street="3 D ST", zip_="89596")
    fcc.anchor("89596", 39.40, -119.40)
    member(db, "KG7SZA", None, None, grid="DM09ab", address_line="2 D ST", city="Reno", state="NV", zip="89596")
    member(db, "KG7SZB", None, None, grid="", address_line="3 D ST", city="Reno", state="NV", zip="89596")
    geocode.sync_user_locations(db)
    db.expire_all()
    a = db.query(User).filter_by(callsign="KG7SZA").one()
    b = db.query(User).filter_by(callsign="KG7SZB").one()
    assert ((a.lat, a.lon), a.location_source) == (grid_to_latlon("DM09ab"), "grid")
    assert ((b.lat, b.lon), b.location_source) == ((39.40, -119.40), "zip")


def test_migrate_users_table_guesses_location_source_once(tmp_path):
    """G1d migration on a pre-existing database: the column is added (NULL
    allowed) and filled once — the hams row's street point -> 'licence',
    the grid-square centre -> 'grid', anything else NULL; idempotent, and a
    later run never re-guesses."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, callsign TEXT, grid TEXT, lat FLOAT, lon FLOAT)")
    con.execute("CREATE TABLE hams (callsign TEXT PRIMARY KEY, lat REAL, lon REAL, loc_source TEXT)")
    centre = grid_to_latlon("DM09ab")
    con.executemany("INSERT INTO hams VALUES (?, ?, ?, ?)", [
        ("KG7MLA", 39.52, -119.81, "address"), ("KG7MLB", 39.5, -119.8, "zip"),
        ("KG7MLC", 39.5, -119.8, "zip"), ("KG7MLF", centre[0], centre[1], "address")])
    con.executemany("INSERT INTO users (callsign, grid, lat, lon) VALUES (?, ?, ?, ?)", [
        ("KG7MLA", "", 39.52, -119.81),             # on the street geocode
        ("KG7MLB", "DM09ab", centre[0], centre[1]),  # on the grid centre
        ("KG7MLC", "", 39.5, -119.8),               # on a ZIP-level point: unknown
        ("KG7MLD", "DM09ab", 39.4321, -119.6543),   # provider / Nominatim: unknown
        ("KG7MLE", "DM09ab", None, None),           # no location
        ("KG7MLF", "DM09ab", centre[0], centre[1]),  # both: the licence wins
    ])
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}")
    uls.migrate_users_table(eng)
    con = sqlite3.connect(path)
    got = dict(con.execute("SELECT callsign, location_source FROM users"))
    assert got == {"KG7MLA": "licence", "KG7MLB": "grid", "KG7MLC": None, "KG7MLD": None,
                   "KG7MLE": None, "KG7MLF": "licence"}
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(users)")}
    assert (cols["location_source"][2], cols["location_source"][3]) == ("TEXT", 0)
    assert cols["map_precision"][4] == "'address'"
    con.execute("UPDATE users SET location_source = 'provider' WHERE callsign = 'KG7MLD'")
    con.execute("UPDATE users SET location_source = NULL WHERE callsign = 'KG7MLA'")
    con.commit()
    con.close()
    uls.migrate_users_table(eng)  # a second start: no new guesses, nothing overwritten
    con = sqlite3.connect(path)
    got2 = dict(con.execute("SELECT callsign, location_source FROM users"))
    assert got2 == dict(got, KG7MLD="provider", KG7MLA=None)
    con.close()


def test_migrate_users_table_without_hams_or_location_columns(tmp_path):
    """The oldest users tables (no lat/lon/grid) and databases without a
    hams table migrate without guessing."""
    path = tmp_path / "older.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, callsign TEXT)")
    con.execute("INSERT INTO users (callsign) VALUES ('KG7OLD')")
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{path}")
    uls.migrate_users_table(eng)
    uls.migrate_users_table(eng)
    con = sqlite3.connect(path)
    assert con.execute("SELECT map_precision, location_source FROM users").fetchone() == ("address", None)
    con.close()


# ---------------------------------------------------------------- G7 account deletion


def test_delete_account_removes_comments_posts_and_the_member(client, db, fcc):
    """G7: wrong password 400, no password 422; then the member's comments,
    their posts with every comment on them, and the account go (204). Other
    members' posts stay; the FCC directory entry stays."""
    fcc.ham("KG7DLA", 39.05, -119.05)
    victim = member(db, "KG7DLA", 39.05, -119.05)
    other = member(db, "KG7DLB", 39.06, -119.06)
    p_victim = new_post(client, victim, title="mine")["id"]
    p_other = new_post(client, other, title="theirs")["id"]

    def comment(user, pid, body):
        r = client.post(f"/api/posts/{pid}/comments", headers=hdr(user), json={"body": body})
        assert r.status_code == 201
        return r.json()["id"]

    c_on_victim = comment(other, p_victim, "nice")
    c_by_victim = comment(victim, p_other, "thanks")
    c_self = comment(victim, p_victim, "bump")
    c_kept = comment(other, p_other, "stays")
    tok = hdr(victim)
    assert client.request("DELETE", "/api/me", headers=tok, json={"password": "wrong-password"}).status_code == 400
    assert client.request("DELETE", "/api/me", headers=tok).status_code == 422
    assert client.request("DELETE", "/api/me", headers=tok, json={}).status_code == 422
    assert client.get("/api/me", headers=tok).status_code == 200            # nothing deleted yet
    r = client.request("DELETE", "/api/me", headers=tok, json={"password": "password123"})
    assert r.status_code == 204 and r.content == b""
    db.expire_all()
    assert db.query(User).filter_by(callsign="KG7DLA").count() == 0
    assert db.get(Post, p_victim) is None and db.get(Post, p_other) is not None
    assert {c.id for c in db.query(Comment).filter(
        Comment.id.in_([c_on_victim, c_by_victim, c_self, c_kept]))} == {c_kept}
    assert db.get(Ham, "KG7DLA") is not None                                # public record stays
    assert client.get("/api/me", headers=tok).status_code == 401
    assert client.post("/api/auth/login", json={"callsign": "KG7DLA", "password": "password123"}).status_code == 404
    assert client.get(f"/api/posts/{p_victim}", headers=hdr(other)).status_code == 404
    assert client.get(f"/api/posts/{p_other}", headers=hdr(other)).json()["comment_count"] == 1


def test_a_deleted_accounts_token_never_opens_another_account(client, db):
    """Regression for G7: SQLite gives the highest deleted row id to the next
    signup, and tokens were checked by id alone — an old token opened the
    new member's account. A token is good only for the callsign it was
    issued to, and not from before that account existed."""
    x = member(db, "KG7TKA", 39.07, -119.07)
    old_tok, old_id = hdr(x), x.id
    assert client.request("DELETE", "/api/me", headers=old_tok, json={"password": "password123"}).status_code == 204
    db.expunge(x)  # deleted by the API's own session
    y = User(callsign="KG7TKB", password_hash=_PW_HASH, email="kg7tkb@example.com", name="Y")
    db.add(y)
    db.commit()
    assert y.id == old_id                                                   # the id was handed on
    assert client.get("/api/me", headers=old_tok).status_code == 401       # old code: 200, as KG7TKB
    assert client.get("/api/me", headers=hdr(y)).json()["callsign"] == "KG7TKB"
    s = get_settings()
    forged = jwt.encode({"sub": "KG7TKA", "uid": y.id, "iat": datetime.now(timezone.utc),
                         "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, s.secret_key,
                        algorithm=s.jwt_algorithm)
    assert client.get("/api/me", headers=auth(forged)).status_code == 401
    created = y.created_at.replace(tzinfo=timezone.utc)
    before = jwt.encode({"sub": "KG7TKB", "uid": y.id, "iat": created - timedelta(seconds=5),
                         "exp": created + timedelta(hours=1)}, s.secret_key, algorithm=s.jwt_algorithm)
    assert client.get("/api/me", headers=auth(before)).status_code == 401   # issued before the account
    db.delete(y)
    db.commit()


# ---------------------------------------------------------------- G8 licences within a radius


_HAM_COLS = ("callsign", "name", "street", "city", "state", "zip", "email", "license_class",
             "expires", "granted", "lat", "lon", "loc_source", "expires_iso", "granted_iso")


def seed_points(points):
    """Replace the hams table with these (lat, lon) points (None = unplaced)."""
    uls.migrate_hams_table()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM hams"))
        conn.exec_driver_sql(
            f"INSERT INTO hams ({', '.join(_HAM_COLS)}) VALUES ({', '.join('?' for _ in _HAM_COLS)})",
            [(f"KG7P{i}", "", "", "", "", "", "", "E", "", "", lat, lon,
              "address" if lat is not None else "", "", "") for i, (lat, lon) in enumerate(points)])


def destination(lat, lon, bearing_deg, miles):
    """The point `miles` from (lat, lon) along a great circle (spherical,
    the radius haversine_miles uses), longitude wrapped into [-180, 180)."""
    from app.geo import EARTH_RADIUS_MILES

    d = miles / EARTH_RADIUS_MILES
    p1, l1, b = math.radians(lat), math.radians(lon), math.radians(bearing_deg)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


CENTRES = [(39.53, -119.81), (40.71, -74.0), (25.76, -80.19), (61.2, -149.9), (71.29, -156.79),
           (51.88, -176.65),  # Adak: the 500-mile cap crosses the antimeridian
           (13.44, 144.79), (-14.28, -170.7),
           (80.0, 20.0), (86.0, -40.0)]  # caps too wide for the series: counted row by row


def test_count_hams_within_is_the_exact_haversine_count(db):
    """G8: the SQL count equals haversine_miles(...) <= r for every point,
    including points a hair inside and outside the circle, across the
    antimeridian and near the poles."""
    rng = random.Random(8)
    radii = (1, 5, 25, 100, 250, 500)
    points = []
    for clat, clon in CENTRES:
        for r in radii:
            for _ in range(30):  # a few metres either side of the circle
                points.append(destination(clat, clon, rng.uniform(0, 360), r * rng.choice((0.99999, 1.00001))))
        for _ in range(150):  # anywhere around
            points.append(destination(clat, clon, rng.uniform(0, 360), rng.uniform(0, 700)))
    points += [(None, None)] * 5
    seed_points(points)
    for clat, clon in CENTRES:
        for r in radii:
            want = sum(1 for la, lo in points if la is not None and haversine_miles(clat, clon, la, lo) <= r)
            assert hams_route.count_hams_within(db, clat, clon, r) == want, (clat, clon, r)


def test_near_count_endpoint(client, db):
    """G8: default radius = the member's range; 1–500 miles; 400 without a
    location. Regression: the route did not exist (404)."""
    centre = (39.53, -119.81)
    pts = [destination(*centre, b, m) for b, m in ((0, 3), (90, 9.9), (180, 10.1), (270, 24.9), (45, 25.1),
                                                    (10, 99), (200, 499), (300, 501))] + [(None, None)]
    seed_points(pts)
    u = member(db, "KG7NCA", *centre, range_miles=25)
    r = client.get("/api/hams/near-count", headers=hdr(u))
    assert r.status_code == 200, r.text
    assert r.json() == {"count": 4, "radius_miles": 25}
    assert client.get("/api/hams/near-count?radius_miles=10", headers=hdr(u)).json() == {"count": 2, "radius_miles": 10}
    assert client.get("/api/hams/near-count?radius_miles=12.5", headers=hdr(u)).json() == \
        {"count": 3, "radius_miles": 12.5}
    assert client.get("/api/hams/near-count?radius_miles=500", headers=hdr(u)).json()["count"] == 7
    for bad in ("0", "0.5", "501", "abc", "nan", "inf", "-5"):
        assert client.get(f"/api/hams/near-count?radius_miles={bad}", headers=hdr(u)).status_code == 422, bad
    nowhere = member(db, "KG7NCB", None, None)
    assert client.get("/api/hams/near-count", headers=hdr(nowhere)).status_code == 400
    assert client.get("/api/hams/near-count").status_code == 401


# ---------------------------------------------------------------- G9 feed by author


def test_feed_author_filter_keeps_range_rules(client, db):
    """G9 regression: the feed ignored `author`. One member's posts, still
    only within the viewer's range; unknown or malformed callsigns give an
    empty list."""
    viewer = member(db, "KG7FAV", 38.80, -119.30, range_miles=25)
    a = member(db, "KG7FAA", 38.81, -119.31)
    b = member(db, "KG7FAB", 38.82, -119.32)
    far = member(db, "KG7FAC", 38.80, -117.00)  # ~124 mi east
    a1 = new_post(client, a, title="a general")["id"]
    a2 = new_post(client, a, title="a for sale", category="for-sale")["id"]
    b1 = new_post(client, b, title="b general")["id"]
    f1 = new_post(client, far, title="far away")["id"]
    assert [p["id"] for p in feed(client, viewer, author="KG7FAA")] == [a2, a1]
    assert [p["id"] for p in feed(client, viewer, author=" kg7faa ")] == [a2, a1]
    assert [p["id"] for p in feed(client, viewer, author="KG7FAA", category="for-sale")] == [a2]
    assert feed(client, viewer, author="KG7FAC") == []                       # out of range
    assert [p["id"] for p in feed(client, far, author="KG7FAC")] == [f1]
    assert feed(client, viewer, author="KG7ZZZ") == []
    assert feed(client, viewer, author="not a call!") == []
    everyone = {p["id"] for p in feed(client, viewer, author="")}
    assert {a1, a2, b1} <= everyone and f1 not in everyone


# ---------------------------------------------------------------- G10 lookup grant date


class _Known(CallsignProvider):
    def __init__(self, callsign):
        self.callsign = callsign

    def lookup(self, callsign):
        if callsign.upper() != self.callsign:
            return None
        return CallsignRecord(callsign=self.callsign, name="Known", address_line="1 A ST", city="Reno",
                              state="NV", zip="89501", grid="DM09ab", license_class="T",
                              expires="01/01/2036", source="fake")


def test_lookup_adds_the_grant_date_and_new_flag(client, db, fcc):
    """G10 regression: /api/lookup had no grant date or new-ham flag."""
    fresh = mdy(TODAY - timedelta(days=12))
    fcc.ham("KG7LKA", cls="T", granted=fresh)                                # local row only
    body = client.get("/api/lookup/kg7lka").json()
    assert (body["found"], body["granted"], body["new"]) == (True, fresh, True)
    fcc.ham("KG7LKB", cls="E", granted="03/15/1990")
    assert client.get("/api/lookup/KG7LKB").json()["new"] is False
    previous = app.dependency_overrides[get_provider]
    try:
        app.dependency_overrides[get_provider] = lambda: _Known("KG7LKA")    # provider + local row
        body = client.get("/api/lookup/KG7LKA").json()
        assert (body["source"], body["granted"], body["new"]) == ("fake", fresh, True)
        app.dependency_overrides[get_provider] = lambda: _Known("KG7LKC")    # provider only
        body = client.get("/api/lookup/KG7LKC").json()
        assert (body["found"], body["granted"], body["new"]) == (True, "", False)
    finally:
        app.dependency_overrides[get_provider] = previous
    fcc.ham("KG7LKD", granted="")                                            # row without a grant date
    assert {k: client.get("/api/lookup/KG7LKD").json()[k] for k in ("granted", "new")} == \
        {"granted": "", "new": False}
    missing = client.get("/api/lookup/KG7LKZ").json()
    assert (missing["found"], missing["granted"], missing["new"]) == (False, "", False)
