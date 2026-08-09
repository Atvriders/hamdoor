import os
import tempfile

_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ULS_IMPORT_ENABLED"] = "false"

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.integrations.provider import CallsignProvider, CallsignRecord, get_provider
from app.main import app
from app.models import User

# callsigns the fake provider "knows"
FAKE_RECORDS = {
    "W1AAA": CallsignRecord(
        callsign="W1AAA", name="Alice Anderson", address_line="1 Main St",
        city="Hartford", state="CT", zip="06103", grid="FN31pr",
        license_class="E", expires="01/01/2030", source="fake",
    ),
    "W1BBB": CallsignRecord(
        callsign="W1BBB", name="Bob Baker", address_line="2 Elm St",
        city="Hartford", state="CT", zip="06103", grid="FN31pr",
        license_class="G", expires="01/01/2030", source="fake",
    ),
    "W9CCC": CallsignRecord(
        callsign="W9CCC", name="Carol Clark", address_line="3 Oak St",
        city="Dodge City", state="KS", zip="67801", grid="EM18",
        license_class="T", expires="01/01/2030", source="fake",
    ),
    "W1DDD": CallsignRecord(
        callsign="W1DDD", name="Dave Douglas", address_line="4 Pine St",
        city="Newington", state="CT", zip="06111", grid="FN31pq",
        license_class="A", expires="01/01/2030", source="fake",
    ),
}


class FakeProvider(CallsignProvider):
    name = "fake"

    def lookup(self, callsign: str):
        return FAKE_RECORDS.get(callsign.upper())


app.dependency_overrides[get_provider] = lambda: FakeProvider()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db():
    from app.db import init_db
    init_db()  # idempotent create_all — covers tests that never hit the web app
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def signup(client, callsign, email=None, password="password123"):
    resp = client.post("/api/auth/signup", json={
        "callsign": callsign,
        "password": password,
        "email": email or f"{callsign.lower()}@example.com",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def set_location(db, callsign, lat, lon):
    user = db.query(User).filter_by(callsign=callsign).one()
    user.lat = lat
    user.lon = lon
    db.commit()
