"""/api/aprs/* routes over a store filled with real packets at a frozen clock.
The aprs.fi lookup uses a fake httpx client: no network."""

import threading
import time
import types

import httpx
import pytest

from app.config import get_settings
from app.integrations import aprs_feed, aprs_parser, aprsis
from app.integrations.aprs_feed import StationStore
from tests.conftest import auth, signup

T0 = 1_800_000_000.0
A_KEYS = {"key", "call", "from", "kind", "cat", "sym", "lat", "lon", "amb", "last", "first",
          "speed_kmh", "course", "alt_m", "comment", "status", "path", "igate", "messaging",
          "mice_msg", "emergency", "device", "wx", "phg", "freq", "track", "aprsfi_url"}
KC = {"min_lat": 38.5, "max_lat": 39.5, "min_lon": -95.0, "max_lon": -94.0}


@pytest.fixture()
def token(client, db):
    r = client.post("/api/auth/login", json={"callsign": "W1DDD", "password": "password123"})
    return r.json()["token"] if r.status_code == 200 else signup(client, "W1DDD")["token"]


@pytest.fixture()
def clock(monkeypatch):
    t = {"now": T0}
    monkeypatch.setattr(aprs_feed, "_clock", lambda: t["now"])
    return t


@pytest.fixture()
def store(monkeypatch, clock):
    st = StationStore(retention_s=6 * 3600, track_max=120)
    monkeypatch.setattr(aprs_feed, "_store", st)
    return st


def put(store, line, t):
    p = aprs_parser.parse_line(line)
    assert p is not None, line
    return store.ingest(p, t)


@pytest.fixture()
def kc(store):
    """A Kansas City scene: a moving car (Mic-E), a home station, a weather
    station, a repeater object; plus one station far away."""
    # Mic-E: dest SY1P8X = 39 10.88 N, no +100, W; lon bytes z?\\ = 94 35.64 W
    car = 'K0CAR-9>SY1P8X,WIDE1-1,qAR,K0IG-10:`z?\\l "k/`"4^}_4'
    put(store, car, T0 - 600)
    put(store, car.replace("`z?\\", "`z?a"), T0 - 300)  # ~70 m west
    put(store, car.replace("`z?\\", "`z?l"), T0 - 60)  # ~160 m further
    put(store, "K0HOME>APRS,TCPIP*,qAC,T2KC:!3905.00N/09435.00W-home", T0 - 1200)
    put(store, "K0WX>APRS,TCPIP*,qAC,T2KC:!3906.00N/09436.00W_090/005g010t072h50b10150", T0 - 900)
    put(store, "Q0AAF>APN391,qAR,K0IG-10:;146.940KC*111111z3907.00N/09437.00WrT151 R75m", T0 - 30)
    put(store, "W1FAR>APRS,TCPIP*,qAC,T2CT:!4143.75N/07242.50W-far away", T0 - 30)
    return store


def get(client, token, path, **params):
    return client.get(path, params=params, headers=auth(token))


def test_aprs_routes_require_auth(client):
    for path in ("/api/aprs/map?min_lat=0&max_lat=1&min_lon=0&max_lon=1&zoom=10",
                 "/api/aprs/station?key=X", "/api/aprs/search?q=X", "/api/aprs/lookup/W1AW",
                 "/api/aprs/status"):
        assert client.get(path).status_code == 401, path


def test_map_hidden_below_zoom_9(client, token, kc):
    r = get(client, token, "/api/aprs/map", **KC, zoom=8)
    assert r.status_code == 200
    assert r.json() == {"type": "hidden", "zoom": 8, "total": 4}
    r = get(client, token, "/api/aprs/map", min_lat=24, max_lat=50, min_lon=-125, max_lon=-66, zoom=4)
    assert r.json()["total"] == 5


def test_map_stations_shape_and_tracks(client, token, kc):
    r = get(client, token, "/api/aprs/map", **KC, zoom=9)
    body = r.json()
    assert set(body) == {"type", "zoom", "total", "truncated", "feed", "stations"}
    assert body["type"] == "stations" and body["total"] == 4 and body["truncated"] is False
    assert set(body["feed"]) == {"connected", "server", "since", "packets", "stations"}
    assert body["feed"]["stations"] == 5
    assert [s["key"] for s in body["stations"]] == ["obj:146.940KC", "K0CAR-9", "K0WX", "K0HOME"]
    for s in body["stations"]:
        assert set(s) == A_KEYS and s["track"] is None  # no tracks below z10
    cats = {s["key"]: s["cat"] for s in body["stations"]}
    assert cats == {"obj:146.940KC": "repeater", "K0CAR-9": "mobile", "K0WX": "wx", "K0HOME": "fixed"}

    z10 = get(client, token, "/api/aprs/map", **KC, zoom=10).json()["stations"]
    car = next(s for s in z10 if s["key"] == "K0CAR-9")
    assert len(car["track"]) == 3 and car["track"][-1][2] == int(T0 - 60)
    assert all(s["track"] is None for s in z10 if s["key"] != "K0CAR-9")  # single fixes
    z10_off = get(client, token, "/api/aprs/map", **KC, zoom=10, tracks=0).json()["stations"]
    assert all(s["track"] is None for s in z10_off)
    short = get(client, token, "/api/aprs/map", **KC, zoom=10, window=400).json()["stations"]
    assert next(s for s in short if s["key"] == "K0CAR-9")["track"] is not None  # 2 fixes in 400 s
    assert [s["key"] for s in short] == ["obj:146.940KC", "K0CAR-9"]


def test_map_never_422_on_wide_or_swapped_bbox(client, token, kc):
    r = get(client, token, "/api/aprs/map", min_lat=60, max_lat=10, min_lon=-260.5, max_lon=110.2, zoom=9)
    assert r.status_code == 200 and r.json()["total"] == 5
    r = get(client, token, "/api/aprs/map", **KC, zoom=99)
    assert r.json()["zoom"] == 22
    r = get(client, token, "/api/aprs/map", **KC, zoom=-3)
    assert r.json() == {"type": "hidden", "zoom": 0, "total": 4}


def test_map_cap_and_truncated(client, token, kc, monkeypatch):
    monkeypatch.setattr(get_settings(), "aprs_map_max_results", 2)
    body = get(client, token, "/api/aprs/map", **KC, zoom=12).json()
    assert body["total"] == 4 and body["truncated"] is True
    assert [s["key"] for s in body["stations"]] == ["obj:146.940KC", "K0CAR-9"]  # most recent first


def test_map_window_is_clamped(client, token, kc, clock):
    # window=10 is raised to the 300 s minimum: the car (60 s) and object (30 s) only
    body = get(client, token, "/api/aprs/map", **KC, zoom=9, window=10).json()
    assert {s["key"] for s in body["stations"]} == {"obj:146.940KC", "K0CAR-9"}
    # huge windows are capped at the retention (6 h)
    clock["now"] = T0 + 5 * 3600
    body = get(client, token, "/api/aprs/map", **KC, zoom=9, window=10**9).json()
    assert body["total"] == 4


def test_map_category_filter(client, token, kc):
    def keys(cats):
        return [s["key"] for s in get(client, token, "/api/aprs/map", **KC, zoom=9, cats=cats).json()["stations"]]
    assert keys("wx") == ["K0WX"]
    assert keys("mobile,repeater") == ["obj:146.940KC", "K0CAR-9"]
    assert keys("FIXED, wx") == ["K0WX", "K0HOME"]
    assert keys("nonsense") == []
    assert len(keys("")) == 4  # blank = all
    hidden = get(client, token, "/api/aprs/map", **KC, zoom=5, cats="wx").json()
    assert hidden == {"type": "hidden", "zoom": 5, "total": 1}


def test_station_detail(client, token, kc):
    r = get(client, token, "/api/aprs/station", key="K0CAR-9")
    assert r.status_code == 200
    st = r.json()
    assert set(st) == A_KEYS | {"packets"}
    assert len(st["track"]) == 3 and len(st["packets"]) == 3
    assert st["packets"][0]["t"] == int(T0 - 60) and st["packets"][0]["raw"].startswith("K0CAR-9>")
    assert st["device"] == "Yaesu FTM-500D" and st["igate"] == "K0IG-10"
    obj = get(client, token, "/api/aprs/station", key="obj:146.940KC").json()
    assert obj["freq"] == 146.94 and obj["comment"] == "T151 R75m" and obj["from"] == "Q0AAF"
    assert get(client, token, "/api/aprs/station", key="N0PE").status_code == 404


def test_search(client, token, kc):
    res = get(client, token, "/api/aprs/search", q="k0").json()["results"]
    assert [r["key"] for r in res] == ["K0CAR-9", "K0WX", "K0HOME"]
    assert set(res[0]) == {"key", "call", "cat", "lat", "lon", "last"}
    assert get(client, token, "/api/aprs/search", q="146").json()["results"][0]["key"] == "obj:146.940KC"
    assert get(client, token, "/api/aprs/search", q="K0WX").json()["results"][0]["call"] == "K0WX"
    assert get(client, token, "/api/aprs/search", q="").json() == {"results": []}
    assert len(get(client, token, "/api/aprs/search", q="K", limit=1).json()["results"]) == 1


def test_status_route(client, token, store):
    body = get(client, token, "/api/aprs/status").json()
    assert set(body) == {"enabled", "connected", "server", "login", "filter", "since", "packets",
                         "packets_per_min", "stations", "window_max_s", "last_error"}
    assert body["enabled"] is False and body["window_max_s"] == 6 * 3600


# ---------------------------------------------------------------- aprs.fi lookup

APRSFI_OK = {"command": "get", "result": "ok", "what": "loc", "found": 1, "entries": [
    {"name": "OH7RDA", "type": "l", "time": "1267445689", "lasttime": "1270580127",
     "lat": "63.06717", "lng": "27.66050", "symbol": "/#", "srccall": "OH7RDA",
     "dstcall": "APND12", "phg": "44603", "comment": "/R,W,Wn,Tn Siilinjarvi",
     "path": "WIDE2-2,qAR,OH7AA"}]}


class FakeHttp:
    """Stands in for httpx.Client inside app.integrations.aprsis."""

    calls: list = []
    responder = None

    def __init__(self, *args, timeout=None, headers=None, **kwargs):
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        FakeHttp.calls.append({"url": url, "params": dict(params or {}), "headers": self.headers})
        return FakeHttp.responder(url, params)


@pytest.fixture()
def aprsfi(monkeypatch):
    monkeypatch.setattr(get_settings(), "aprsfi_api_key", "SECRETKEY")
    monkeypatch.setattr(aprsis, "_lookup_cache", aprsis._LookupCache())
    monkeypatch.setattr(aprsis, "_global_budget",
                        aprsis._Budget(aprsis.APRSFI_GLOBAL_BURST, aprsis.APRSFI_GLOBAL_REFILL_S))
    monkeypatch.setattr(aprsis, "_user_budgets", {})
    monkeypatch.setattr(aprsis, "_inflight", {})
    monkeypatch.setattr(aprsis, "_failures", 0)
    monkeypatch.setattr(aprsis, "_paused_until", 0.0)
    mono = {"t": 1000.0}
    monkeypatch.setattr(aprsis, "time", types.SimpleNamespace(monotonic=lambda: mono["t"]))
    monkeypatch.setattr(aprsis.httpx, "Client", FakeHttp)
    FakeHttp.calls = []
    FakeHttp.responder = staticmethod(lambda url, params: httpx.Response(200, json=APRSFI_OK))
    return mono


def test_lookup_not_configured(client, token, monkeypatch):
    monkeypatch.setattr(get_settings(), "aprsfi_api_key", "")
    r = get(client, token, "/api/aprs/lookup/OH7RDA")
    assert r.status_code == 404
    assert r.json()["detail"] == "aprs.fi lookup is not configured on this server"


def test_lookup_found_and_cached(client, token, aprsfi):
    r = get(client, token, "/api/aprs/lookup/oh7rda")
    assert r.status_code == 200
    assert r.json() == {"found": True, "call": "OH7RDA", "lat": 63.06717, "lon": 27.6605,
                        "last": 1270580127, "comment": "/R,W,Wn,Tn Siilinjarvi",
                        "aprsfi_url": "https://aprs.fi/#!call=a%2FOH7RDA", "source": "aprs.fi"}
    call = FakeHttp.calls[0]
    assert call["url"] == "https://api.aprs.fi/api/get"
    assert call["params"] == {"name": "OH7RDA", "what": "loc", "apikey": "SECRETKEY", "format": "json"}
    assert call["headers"]["User-Agent"] == get_settings().http_user_agent
    # multi-call: a second user within 60 s is served from the shared cache
    assert get(client, token, "/api/aprs/lookup/OH7RDA").status_code == 200
    assert len(FakeHttp.calls) == 1


def test_lookup_not_found(client, token, aprsfi):
    FakeHttp.responder = staticmethod(lambda url, params: httpx.Response(
        200, json={"command": "get", "result": "ok", "what": "loc", "found": 0, "entries": []}))
    body = get(client, token, "/api/aprs/lookup/N0NE").json()
    assert body["found"] is False and body["lat"] is None and body["aprsfi_url"].endswith("a%2FN0NE")


def test_lookup_rejects_wildcards_and_lists(client, token, aprsfi):
    assert get(client, token, "/api/aprs/lookup/W1AW,K1ABC").status_code == 422
    assert get(client, token, "/api/aprs/lookup/W1*").status_code == 422
    assert get(client, token, "/api/aprs/lookup/TOOLONGNAME1").status_code == 422
    assert FakeHttp.calls == []


def test_lookup_backs_off_after_repeated_failures(client, token, aprsfi):
    def down(url, params):
        raise httpx.ConnectError("boom SECRETKEY")
    FakeHttp.responder = staticmethod(down)
    assert get(client, token, "/api/aprs/lookup/W1AW").status_code == 503
    assert get(client, token, "/api/aprs/lookup/W1AW").status_code == 503
    assert len(FakeHttp.calls) == 2
    r = get(client, token, "/api/aprs/lookup/W1AW")  # paused: aprs.fi is not called
    assert r.status_code == 503 and "paused" in r.json()["detail"] and len(FakeHttp.calls) == 2
    aprsfi["t"] += 31  # the 30 s pause is over
    FakeHttp.responder = staticmethod(lambda url, params: httpx.Response(200, json=APRSFI_OK))
    assert get(client, token, "/api/aprs/lookup/OH7RDA").status_code == 200
    assert aprsis._failures == 0


def test_lookup_api_error_is_reported_without_the_key(client, token, aprsfi):
    FakeHttp.responder = staticmethod(lambda url, params: httpx.Response(
        200, json={"result": "fail", "description": "authentication failed: wrong API key SECRETKEY"}))
    r = get(client, token, "/api/aprs/lookup/OH7RDA")
    assert r.status_code == 503 and "SECRETKEY" not in r.text and "authentication failed" in r.text


# ---------------------------------------------------------------- review fixes (B3-R*)

NAN_GGA = b"N0NAN-15>GPSLV,qAR,K0IG:$GPGGA,205759.000,3906.0000,N,09436.0000,W,1,08,1.1,nan,M,-36.0,M,,0000*29"


def test_one_nan_packet_cannot_break_the_map(client, token, kc):
    """B3-R1, the reviewer's repro: before the fix this one packet (valid
    checksum) made the z10 area view, a z12 street view and the station
    detail HTTP 500 until the 6 h retention pruned it."""
    put(kc, NAN_GGA, T0 - 30)
    assert get(client, token, "/api/aprs/map", **KC, zoom=10).status_code == 200
    street = get(client, token, "/api/aprs/map", min_lat=39.09, max_lat=39.11, min_lon=-94.61, max_lon=-94.59,
                 zoom=12)
    assert street.status_code == 200
    nan = next(s for s in street.json()["stations"] if s["key"] == "N0NAN-15")
    assert nan["alt_m"] is None and (nan["lat"], nan["lon"]) == (pytest.approx(39.1), pytest.approx(-94.6))
    assert get(client, token, "/api/aprs/station", key="N0NAN-15").status_code == 200


def test_lookup_budget_per_user_answers_429(client, token, aprsfi):
    """B3-R5, multi-call: every distinct name was an upstream call on the
    deployer's key, with no limit. One user now gets a burst, then 429
    with Retry-After; cached names stay free; the budget refills."""
    burst = aprsis.APRSFI_USER_BURST
    codes = [get(client, token, f"/api/aprs/lookup/N{i}ABC").status_code for i in range(burst + 3)]
    assert codes == [200] * burst + [429] * 3
    assert len(FakeHttp.calls) == burst
    r = get(client, token, "/api/aprs/lookup/N9ABC")
    assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= aprsis.APRSFI_USER_REFILL_S
    assert get(client, token, "/api/aprs/lookup/N0ABC").status_code == 200  # cached: no budget needed
    aprsfi["t"] += aprsis.APRSFI_USER_REFILL_S  # one call's worth refills
    assert get(client, token, "/api/aprs/lookup/N9ABC").status_code == 200
    assert get(client, token, "/api/aprs/lookup/N8ABC").status_code == 429
    assert len(FakeHttp.calls) == burst + 1


def test_lookup_global_budget_spans_users(aprsfi):
    """B3-R5: the server-wide budget caps upstream calls however many users
    ask (the reviewer's 500 distinct names gave 500 aprs.fi calls)."""
    outcome = []
    for i in range(500):
        try:
            aprsis.aprsfi_lookup(f"N{i}ABC", user_key=f"user{i}")
            outcome.append("ok")
        except aprsis.AprsFiRateLimited as exc:
            assert exc.retry_after >= 1
            outcome.append("429")
    assert outcome == ["ok"] * aprsis.APRSFI_GLOBAL_BURST + ["429"] * (500 - aprsis.APRSFI_GLOBAL_BURST)
    assert len(FakeHttp.calls) == aprsis.APRSFI_GLOBAL_BURST
    aprsfi["t"] += 10 * aprsis.APRSFI_GLOBAL_REFILL_S
    aprsis.aprsfi_lookup("K0NEW", user_key="user-new")
    assert len(FakeHttp.calls) == aprsis.APRSFI_GLOBAL_BURST + 1


def test_lookup_cache_forgets_expired_names_and_is_bounded(aprsfi):
    """B3-R5: TTLCache dropped an expired entry only when the same name was
    asked again, so every distinct name stayed for the life of the process."""
    cache = aprsis._lookup_cache
    for i in range(5):
        cache.set(f"N{i}ABC", {"found": False}, aprsis.APRSFI_CACHE_SECONDS)
    assert len(cache) == 5
    aprsfi["t"] += aprsis.APRSFI_CACHE_SECONDS + 1
    cache.set("K0NEW", {"found": True}, aprsis.APRSFI_CACHE_SECONDS)
    assert len(cache) == 1 and cache.get("N0ABC") is None and cache.get("K0NEW") == {"found": True}
    small = aprsis._LookupCache(max_entries=3)
    for i in range(5):
        small.set(f"K{i}", {"i": i}, 60)
    assert small.get("K0") is None and small.get("K1") is None
    assert len(small) == 3 and small.get("K4") == {"i": 4}


def test_lookup_single_flight_per_name(aprsfi):
    """B3-R5: two users asking for the same uncached name at once cost one
    aprs.fi call; the second waits for the first answer."""
    inside, release = threading.Event(), threading.Event()

    def slow(url, params):
        inside.set()
        release.wait(5)
        return httpx.Response(200, json=APRSFI_OK)
    FakeHttp.responder = staticmethod(slow)
    answers = []
    first = threading.Thread(target=lambda: answers.append(aprsis.aprsfi_lookup("OH7RDA", user_key="a")))
    second = threading.Thread(target=lambda: answers.append(aprsis.aprsfi_lookup("OH7RDA", user_key="b")))
    first.start()
    assert inside.wait(5)
    second.start()
    deadline = time.time() + 5  # until the second asker is either waiting or (old code) upstream too
    while (time.time() < deadline and len(FakeHttp.calls) < 2
           and getattr(aprsis, "_inflight", {}).get("OH7RDA", [None, 0])[1] < 2):
        time.sleep(0.005)
    release.set()
    first.join(5)
    second.join(5)
    assert len(answers) == 2 and answers[0] == answers[1] and answers[0]["found"] is True
    assert len(FakeHttp.calls) == 1


def test_lookup_ignores_non_finite_coordinates(client, token, aprsfi):
    # B3-R1 (same root cause): float("nan") from an upstream answer would make
    # this name's lookup a 500 for every user for the cache lifetime
    entry = dict(APRSFI_OK["entries"][0], lat="nan", lng="inf")
    FakeHttp.responder = staticmethod(lambda url, params: httpx.Response(200, json={**APRSFI_OK, "entries": [entry]}))
    r = get(client, token, "/api/aprs/lookup/OH7RDA")
    assert r.status_code == 200 and r.json()["found"] is False and r.json()["lat"] is None


# ---------------------------------------------------------------- second review (B3-2R*)

def position_line(src, lat, lon, sym="/>"):
    """An uncompressed position line (to 0.01 minute)."""
    def dm(v, width, hemis):
        d = int(abs(v))
        return f"{d:0{width}d}{(abs(v) - d) * 60:05.2f}{hemis[0] if v >= 0 else hemis[1]}"
    return f"{src}>APRS,TCPIP*,qAC,T2KC:!{dm(lat, 2, 'NS')}{sym[0]}{dm(lon, 3, 'EW')}{sym[1]}"


def test_map_track_tails_share_one_budget(client, token, store, monkeypatch):
    """B3-2R2: tails were gated only by the client-chosen zoom, so a CONUS
    bbox sent with zoom=10 carried every tail in the store (84k points,
    5.9 MB, 230 ms under the store lock; 19 MB at saturation). One response
    now holds at most TRACK_POINTS_MAX points, most recent stations first."""
    monkeypatch.setattr(aprs_feed, "TRACK_POINTS_MAX", 50, raising=False)
    for i, call in enumerate(("K0A-9", "K0B-9", "K0C-9")):  # 20-point tails; K0A-9 the most recent
        for k in range(20):
            put(store, position_line(call, 39.0 + k * 0.002, -94.5 - i * 0.1), T0 - 2000 + k * 60 - i)
    body = get(client, token, "/api/aprs/map", min_lat=24, max_lat=50, min_lon=-125, max_lon=-66, zoom=10).json()
    tails = {s["key"]: len(s["track"]) if s["track"] else None for s in body["stations"]}
    assert body["total"] == 3 and body["truncated"] is False
    assert tails == {"K0A-9": 20, "K0B-9": 20, "K0C-9": None}
    assert all(len(p) == 3 for s in body["stations"] if s["track"] for p in s["track"])


@pytest.mark.parametrize("bbox,zoom,total", [
    (dict(min_lat="-inf", max_lat="-inf", min_lon=-95, max_lon=-94), 9, 0),
    (dict(min_lat=39, max_lat=40, min_lon="inf", max_lon="inf"), 9, 0),
    (dict(min_lat="-inf", max_lat="-inf", min_lon=-95, max_lon=-94), 4, 0),
    (dict(min_lat="inf", max_lat="inf", min_lon=-95, max_lon=-94), 12, 0),
    (dict(min_lat=39, max_lat=40, min_lon="-inf", max_lon="-inf"), 10, 0),
    (dict(min_lat="-inf", max_lat="inf", min_lon=-95, max_lon=-94), 9, 4),  # one infinite edge: unbounded
    (dict(min_lat="nan", max_lat="nan", min_lon="nan", max_lon="nan"), 9, 5),  # clamp_bbox: NaN = unbounded
])
def test_map_non_finite_bbox_is_clamped_not_a_500(client, token, kc, bbox, zoom, total):
    """B3-2R3: FastAPI accepts "inf" for a float parameter and clamp_bbox
    let min_lat=max_lat=-inf (or inf, or the same for longitude) through;
    math.floor(inf) in the grid lookup raised (HTTP 500). Contract §1:
    an out-of-range bbox is clamped, never an error."""
    r = get(client, token, "/api/aprs/map", **bbox, zoom=zoom)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["total"] == total


def test_lookup_backoff_survives_weeks_of_failures(client, token, aprsfi, monkeypatch):
    """Same overflow as B3-2R1, in the aprs.fi backoff: 30.0 * 2**(failures - 2)
    raised OverflowError on the 1,026th failure in a row (a revoked key for
    ~3 weeks), so every later failure was a 500 and the pause was never set
    again. It must stay a 503 that pauses for the 30-minute cap."""
    monkeypatch.setattr(aprsis, "_failures", 5000)
    FakeHttp.responder = staticmethod(lambda url, params: httpx.Response(
        200, json={"result": "fail", "description": "authentication failed"}))
    r = get(client, token, "/api/aprs/lookup/OH7RDA")
    assert r.status_code == 503 and len(FakeHttp.calls) == 1
    assert aprsis._paused_until == aprsfi["t"] + 1800.0
    r = get(client, token, "/api/aprs/lookup/OH7RDA")  # paused: aprs.fi is not asked
    assert r.status_code == 503 and "paused" in r.json()["detail"] and len(FakeHttp.calls) == 1
