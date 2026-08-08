from tests.conftest import auth, signup


def test_lookup_found(client):
    r = client.get("/api/lookup/W1AAA")
    assert r.status_code == 200
    data = r.json()
    assert data["found"] is True
    assert data["name"] == "Alice Anderson"
    assert data["city"] == "Hartford"
    assert data["grid"] == "FN31pr"


def test_lookup_not_found(client):
    r = client.get("/api/lookup/ZZ9ZZZ")
    assert r.status_code == 200
    assert r.json()["found"] is False


def test_lookup_normalizes_callsign(client):
    r = client.get("/api/lookup/w1aaa")
    assert r.json()["callsign"] == "W1AAA"


def test_signup_autofills_profile(client):
    data = signup(client, "W1AAA")
    assert data["token"]
    user = data["user"]
    assert user["name"] == "Alice Anderson"
    assert user["address_line"] == "1 Main St"
    assert user["grid"] == "FN31pr"
    # grid FN31pr resolves to a location without any network call
    assert user["lat"] is not None and user["lon"] is not None
    assert user["range_miles"] == 25


def test_signup_duplicate_callsign_rejected(client):
    r = client.post("/api/auth/signup", json={
        "callsign": "W1AAA", "password": "password123", "email": "other@example.com",
    })
    assert r.status_code == 409


def test_signup_unknown_callsign_rejected(client):
    r = client.post("/api/auth/signup", json={
        "callsign": "ZZ9ZZZ", "password": "password123", "email": "zz@example.com",
    })
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


def test_signup_short_password_rejected(client):
    r = client.post("/api/auth/signup", json={
        "callsign": "W1BBB", "password": "short", "email": "b@example.com",
    })
    assert r.status_code == 422


def test_login_success_and_me(client):
    signup(client, "W1BBB")
    r = client.post("/api/auth/login", json={"callsign": "W1BBB", "password": "password123"})
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/api/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["callsign"] == "W1BBB"
    # self view includes private fields
    assert me.json()["email"] == "w1bbb@example.com"


def test_login_wrong_password(client):
    r = client.post("/api/auth/login", json={"callsign": "W1BBB", "password": "wrong-password"})
    assert r.status_code == 401
    assert r.json()["detail"] == "incorrect password"


def test_login_unknown_callsign_tells_you_to_sign_up(client):
    r = client.post("/api/auth/login", json={"callsign": "W9ZZZ", "password": "whatever123"})
    assert r.status_code == 404
    assert "no account registered" in r.json()["detail"]


def test_health_reports_user_count(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["users"] >= 2  # signups from earlier tests share this DB
    assert body["database"].endswith(".db")


def test_protected_route_requires_token(client):
    assert client.get("/api/me").status_code == 401
