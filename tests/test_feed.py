"""Feed distance filtering, posts/comments, and nearby-operator privacy."""

from tests.conftest import auth, set_location, signup

# exact locations: A and B are 10 miles apart, C is ~158 miles away
LOC_A = (40.000, -75.000)
LOC_B = (40.145, -75.000)   # ~10 miles north of A
LOC_C = (40.000, -78.000)   # ~158 miles west of A — outside 25 mi, inside 500 mi


def _token_for(client, db, callsign, latlon):
    r = client.post("/api/auth/login", json={"callsign": callsign, "password": "password123"})
    if r.status_code != 200:
        data = signup(client, callsign)
        token = data["token"]
    else:
        token = r.json()["token"]
    set_location(db, callsign, *latlon)
    return token


def _setup(client, db):
    tok_a = _token_for(client, db, "W1AAA", LOC_A)
    tok_b = _token_for(client, db, "W1BBB", LOC_B)
    tok_c = _token_for(client, db, "W9CCC", LOC_C)
    return tok_a, tok_b, tok_c


def test_feed_shows_nearby_post_not_distant_one(client, db):
    tok_a, tok_b, tok_c = _setup(client, db)

    r = client.post("/api/posts", headers=auth(tok_b),
                    json={"title": "antenna party", "body": "help me raise a dipole", "category": "help-wanted"})
    assert r.status_code == 201, r.text
    near_post_id = r.json()["id"]

    r = client.post("/api/posts", headers=auth(tok_c),
                    json={"title": "far away for sale", "body": "beam antenna", "category": "for-sale"})
    assert r.status_code == 201

    # A's default range is 25 miles: sees B (10 mi) but not C
    feed = client.get("/api/posts/feed", headers=auth(tok_a)).json()
    titles = [p["title"] for p in feed]
    assert "antenna party" in titles
    assert "far away for sale" not in titles

    post = next(p for p in feed if p["id"] == near_post_id)
    assert 9 < post["distance_miles"] < 11
    assert post["author_callsign"] == "W1BBB"

    # shrink A's range to 5 miles: B's post (10 mi away) drops out
    r = client.patch("/api/me", headers=auth(tok_a), json={"range_miles": 5})
    assert r.status_code == 200
    feed = client.get("/api/posts/feed", headers=auth(tok_a)).json()
    assert "antenna party" not in [p["title"] for p in feed]

    # widen to 500 miles: everything appears
    client.patch("/api/me", headers=auth(tok_a), json={"range_miles": 500})
    feed = client.get("/api/posts/feed", headers=auth(tok_a)).json()
    titles = [p["title"] for p in feed]
    assert "antenna party" in titles and "far away for sale" in titles
    client.patch("/api/me", headers=auth(tok_a), json={"range_miles": 25})


def test_feed_category_filter(client, db):
    tok_a, tok_b, _ = _setup(client, db)
    feed = client.get("/api/posts/feed?category=for-sale", headers=auth(tok_a)).json()
    assert all(p["category"] == "for-sale" for p in feed)


def test_post_edit_delete_permissions(client, db):
    tok_a, tok_b, _ = _setup(client, db)
    r = client.post("/api/posts", headers=auth(tok_b),
                    json={"title": "net tonight", "body": "2m net at 8pm", "category": "nets-events"})
    pid = r.json()["id"]

    # someone else cannot edit or delete it
    assert client.patch(f"/api/posts/{pid}", headers=auth(tok_a), json={"title": "hacked"}).status_code == 403
    assert client.delete(f"/api/posts/{pid}", headers=auth(tok_a)).status_code == 403

    # the author can
    r = client.patch(f"/api/posts/{pid}", headers=auth(tok_b), json={"title": "net moved to 9pm"})
    assert r.status_code == 200 and r.json()["title"] == "net moved to 9pm"
    assert client.delete(f"/api/posts/{pid}", headers=auth(tok_b)).status_code == 204
    assert client.get(f"/api/posts/{pid}", headers=auth(tok_b)).status_code == 404


def test_comments(client, db):
    tok_a, tok_b, _ = _setup(client, db)
    r = client.post("/api/posts", headers=auth(tok_b),
                    json={"title": "elmer needed", "body": "ft8 setup", "category": "elmer"})
    pid = r.json()["id"]

    r = client.post(f"/api/posts/{pid}/comments", headers=auth(tok_a), json={"body": "happy to help!"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    post = client.get(f"/api/posts/{pid}", headers=auth(tok_a)).json()
    assert post["comment_count"] == 1
    assert post["comments"][0]["author_callsign"] == "W1AAA"

    # post author may delete a comment on their post
    assert client.delete(f"/api/comments/{cid}", headers=auth(tok_b)).status_code == 204
    post = client.get(f"/api/posts/{pid}", headers=auth(tok_a)).json()
    assert post["comment_count"] == 0


def test_operators_nearby_and_privacy(client, db):
    tok_a, tok_b, _ = _setup(client, db)
    r = client.get("/api/operators/nearby", headers=auth(tok_a))
    assert r.status_code == 200
    ops = r.json()
    callsigns = [o["callsign"] for o in ops]
    assert "W1BBB" in callsigns        # 10 miles away, within default 25 mi
    assert "W9CCC" not in callsigns    # far away
    assert "W1AAA" not in callsigns    # excludes self

    op_b = next(o for o in ops if o["callsign"] == "W1BBB")
    assert 9 < op_b["distance_miles"] < 11
    # privacy: no street address or email leaked
    assert "email" not in op_b
    assert "address_line" not in op_b
    assert op_b["grid"] == "FN31pr"


def test_operators_explicit_range(client, db):
    tok_a, _, tok_c = _setup(client, db)
    r = client.get("/api/operators/nearby?range=500", headers=auth(tok_a))
    callsigns = [o["callsign"] for o in r.json()]
    assert "W9CCC" in callsigns


def test_range_validation(client, db):
    tok_a, _, _ = _setup(client, db)
    assert client.patch("/api/me", headers=auth(tok_a), json={"range_miles": 1}).status_code == 422
    assert client.patch("/api/me", headers=auth(tok_a), json={"range_miles": 10000}).status_code == 422
