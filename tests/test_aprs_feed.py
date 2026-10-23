"""APRS-IS feed + station store: tracks, categories, pruning, snapshot,
login rules and the connection loop (scripted fake sockets, fake clock).

No network: aprs_feed._open_connection is replaced by FakeSocket and
aprs_feed._clock by FakeClock everywhere a test touches the feed.
"""

import gzip
import json
import math
import random
import socket
import time
from pathlib import Path

import pytest

from app.config import get_settings
from app.integrations import aprs_feed, aprs_parser
from app.integrations.aprs_feed import StationStore, category

FIXTURES = Path(__file__).parent / "fixtures"
T0 = 1_800_000_000.0
A_KEYS = {"key", "call", "from", "kind", "cat", "sym", "lat", "lon", "amb", "last", "first",
          "speed_kmh", "course", "alt_m", "comment", "status", "path", "igate", "messaging",
          "mice_msg", "emergency", "device", "wx", "phg", "freq", "track", "aprsfi_url"}


class FakeClock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeSocket:
    """recv() plays a script: bytes are returned, "timeout" raises
    socket.timeout, callables are called (and may advance a clock / raise);
    an empty script means the server closed the connection."""

    def __init__(self, script):
        self.script = list(script)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, t):
        pass

    def recv(self, n):
        if not self.script:
            return b""
        item = self.script.pop(0)
        if item == "timeout":
            raise socket.timeout()
        if callable(item):
            return item()
        return item

    def sendall(self, data):
        self.sent.append(data)

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def pos(src, lat, lon, sym="/-", comment="", dti="!"):
    """A real uncompressed position line for (lat, lon)."""
    def dm(v, deg_digits, hemis):
        h = hemis[0] if v >= 0 else hemis[1]
        v = abs(v)
        d = int(v)
        return f"{d:0{deg_digits}d}{(v - d) * 60:05.2f}{h}"
    return f"{src}>APRS,WIDE1-1,qAR,IGATE-1:{dti}{dm(lat, 2, 'NS')}{sym[0]}{dm(lon, 3, 'EW')}{sym[1]}{comment}"


def feed_line(store, line, now):
    p = aprs_parser.parse_line(line)
    assert p is not None, line
    return store.ingest(p, now)


@pytest.fixture()
def store():
    return StationStore(retention_s=6 * 3600, track_max=120)


@pytest.fixture()
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(aprs_feed, "_clock", c)
    return c


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(aprs_feed, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(aprs_feed, "_login_cache", None)
    return tmp_path


# ---------------------------------------------------------------- store: tracks

def test_track_grows_only_on_real_moves(store):
    """Multi-call: identical / <30 m fixes add nothing, >=30 m adds a point."""
    feed_line(store, pos("K0CAR-9", 39.0, -94.5, "/>"), T0)
    feed_line(store, pos("K0CAR-9", 39.0, -94.5, "/>"), T0 + 60)  # parked
    feed_line(store, pos("K0CAR-9", 39.0001, -94.5, "/>"), T0 + 120)  # ~11 m jitter
    st = store._st["K0CAR-9"]
    assert len(st.track) == 1 and st.moved_at is None
    feed_line(store, pos("K0CAR-9", 39.0010, -94.5, "/>"), T0 + 180)  # ~111 m
    feed_line(store, pos("K0CAR-9", 39.0100, -94.5, "/>"), T0 + 240)
    assert [p[2] for p in st.track] == [int(T0), int(T0 + 180), int(T0 + 240)]
    assert st.moved_at == T0 + 240 and st.last == T0 + 240 and st.first == T0
    assert (st.lat, st.lon) == (pytest.approx(39.01), pytest.approx(-94.5))


def test_track_is_capped(store):
    store.track_max = 3
    for i in range(6):
        feed_line(store, pos("K0CAR-9", 39.0 + i * 0.01, -94.5, "/>"), T0 + i * 60)
    track = store._st["K0CAR-9"].track
    assert len(track) == 3 and track[-1][2] == int(T0 + 300) and track[0][2] == int(T0 + 180)


def test_packets_log_is_capped(store):
    for i in range(aprs_feed.PACKETS_KEPT + 5):
        feed_line(store, pos("K0FIX", 39.0, -94.5, comment=f"beacon {i}"), T0 + i)
    out = store.station("K0FIX", now=T0 + 30, window_s=3600)
    assert len(out["packets"]) == aprs_feed.PACKETS_KEPT
    assert out["packets"][0]["raw"].endswith("beacon 24")  # newest first
    assert out["packets"][0]["t"] == int(T0 + 24)


# ---------------------------------------------------------------- store: lifecycle

def test_killed_object_is_removed_then_can_return(store):
    alive = "OUNFLS>APRS,qAO,AE5PL-WX:;OUNFLS90A*162100z3534.50N\\09816.80Ww FLOOD"
    killed = "OUNFLS>APRS,qAO,AE5PL-WX:;OUNFLS90A_162100z3534.50N\\09816.80Ww FLOOD"
    assert feed_line(store, alive, T0) == "obj:OUNFLS90A"
    assert store.positioned_count() == 1
    assert feed_line(store, killed, T0 + 10) is None
    assert "obj:OUNFLS90A" not in store._st and store.positioned_count() == 0
    assert store._grid == {}
    feed_line(store, alive, T0 + 20)
    assert store._st["obj:OUNFLS90A"].first == T0 + 20


def test_items_are_keyed_separately(store):
    feed_line(store, "NEWPRT>APRS,qAR,INDIAN:)PCH!4437.74N/12403.57Wh", T0)
    feed_line(store, "Q4AU-13>APMI06,TCPIP*,qAS,QA4G:)W21_3844.04N\\07750.01W_", T0)
    assert set(store._st) == {"item:PCH"}
    st = store._st["item:PCH"]
    assert st.kind == "item" and st.call == "PCH" and st.src == "NEWPRT"


def test_status_before_and_after_position(store):
    feed_line(store, "Q8ACT-15>APRS,qAR,K8XX:>On the air", T0)
    assert store.positioned_count() == 0  # nothing to draw yet
    total, rows = store.query(30, 50, -100, -70, now=T0, window_s=3600)
    assert total == 0 and rows == []
    feed_line(store, pos("Q8ACT-15", 41.0, -83.0, "/-"), T0 + 5)
    total, rows = store.query(30, 50, -100, -70, now=T0 + 5, window_s=3600)
    assert total == 1 and rows[0]["status"] == "On the air"
    feed_line(store, "Q8ACT-15>APRS,qAR,K8XX:>QRT", T0 + 10)
    assert store.station("Q8ACT-15", now=T0 + 10, window_s=3600)["status"] == "QRT"


def test_positionless_weather_attaches_to_station(store):
    feed_line(store, pos("BENRDG", 46.0, -119.0, "/_", comment="090/005t050"), T0)
    feed_line(store, "BENRDG>APRS,WIDE2-2,qAR,QA7ABI:_10161529c102s000g000t040h43e1w", T0 + 60)
    st = store.station("BENRDG", now=T0 + 60, window_s=3600)
    assert st["cat"] == "wx" and st["wx"]["humidity"] == 43 and len(st["packets"]) == 2
    # `last` is the position's time: weather/status do not make a pin look fresher (B3-R6)
    assert st["last"] == int(T0) and store._st["BENRDG"].last == T0 + 60


def test_null_island_is_ignored(store):
    assert feed_line(store, pos("N0FIX", 0.0, 0.0), T0) is None
    assert len(store) == 0


def test_prune_retention_multi_call(store):
    store.retention_s = 3600
    feed_line(store, pos("K0OLD", 39.0, -94.0), T0)
    for i in range(5):  # a mover beaconing every 20 min
        feed_line(store, pos("K0CAR-9", 39.0 + i * 0.01, -94.5, "/>"), T0 + i * 1200)
    store.prune(T0 + 1800)
    assert "K0OLD" in store._st  # 30 min old: kept
    removed = store.prune(T0 + 4801)
    assert removed == 1 and "K0OLD" not in store._st
    track = store._st["K0CAR-9"].track
    assert [p[2] for p in track] == [int(T0 + 1200 * 2), int(T0 + 1200 * 3), int(T0 + 1200 * 4)]
    removed = store.prune(T0 + 4800 + 3601)
    assert removed == 1 and len(store) == 0 and store._grid == {}


def test_grid_follows_a_station_across_cells(store):
    feed_line(store, pos("K0CAR-9", 39.995, -94.5, "/>"), T0)
    feed_line(store, pos("K0CAR-9", 40.005, -94.5, "/>"), T0 + 60)
    assert store._grid == {(40, -95): {"K0CAR-9"}}
    assert store.query(39.0, 39.999, -95, -94, now=T0 + 60, window_s=3600)[0] == 0
    assert store.query(40.0, 40.5, -95, -94, now=T0 + 60, window_s=3600)[0] == 1


# ---------------------------------------------------------------- categories

@pytest.mark.parametrize("line,expected", [
    (pos("N0WX", 39, -94, "/_", "090/005t050"), "wx"),
    (pos("N0DIG", 39, -94, "/#"), "digi"),
    (pos("N0DIG-1", 39, -94, "S#"), "digi"),  # overlay digi
    (pos("N0IG", 39, -94, "I&"), "digi"),  # iGate
    (pos("N0RPT", 39, -94, "/r"), "repeater"),
    (pos("N0HOME", 39, -94, "/-"), "fixed"),
    (pos("N0CAR-9", 39, -94, "/>", "090/030"), "mobile"),  # speed > 0
    (pos("N0CAR-9", 39, -94, "/>"), "fixed"),  # vehicle symbol, never moved
    ("Q0AAF>APN391,qAR,QA6ABG:;443.775NE*111111z4104.25N/09613.63WrW0EQU", "repeater"),
    ("K0ABC>APRS:;146.94-KC*111111z3900.00N/09400.00W/T151 R75m", "repeater"),  # freq name
    ("K0ABC>APRS:;FIELDDAY *111111z3900.00N/09400.00W;net 146.520MHz", "repeater"),  # freq comment
    ("K0ABC>APRS:;FIELDDAY *111111z3900.00N/09400.00W;Field Day site", "object"),
    ("NEWPRT>APRS,qAR,INDIAN:)PCH!4437.74N/12403.57Wh", "object"),
])
def test_category_rules(store, line, expected):
    key = feed_line(store, line, T0)
    assert category(store._st[key], T0, 3600, 6 * 3600) == expected


def test_mobile_by_displacement_and_window(store):
    """Non-vehicle symbol: mobile only while its last move is inside the
    window. Vehicle symbol: mobile while the move is inside the retention."""
    for call, sym in (("N0HT-7", "/-"), ("N0CAR-9", "/>")):
        feed_line(store, pos(call, 39.0, -94.0, sym), T0)
        feed_line(store, pos(call, 39.01, -94.0, sym), T0 + 60)  # moved, no speed field
    for call, sym in (("N0HT-7", "/-"), ("N0CAR-9", "/>")):
        st = store._st[call]
        assert category(st, T0 + 600, 3600, 6 * 3600) == "mobile"
        later = T0 + 60 + 2 * 3600  # 2 h after the move
        assert category(st, later, 3600, 6 * 3600) == ("mobile" if sym == "/>" else "fixed")
        assert category(st, later, 3 * 3600, 6 * 3600) == "mobile"  # wider window


# ---------------------------------------------------------------- queries

def test_query_window_cats_order_and_cap(store):
    feed_line(store, pos("N0OLD", 39.0, -94.0), T0)
    feed_line(store, pos("N0WX", 39.1, -94.1, "/_", "090/005t050"), T0 + 3000)
    feed_line(store, pos("N0NEW", 39.2, -94.2), T0 + 3500)
    now = T0 + 4000
    total, rows = store.query(38, 40, -95, -93, now=now, window_s=3600)
    assert total == 2 and [r["key"] for r in rows] == ["N0NEW", "N0WX"]  # N0OLD outside 1 h
    total, rows = store.query(38, 40, -95, -93, now=now, window_s=7200)
    assert total == 3 and [r["key"] for r in rows] == ["N0NEW", "N0WX", "N0OLD"]
    total, rows = store.query(38, 40, -95, -93, now=now, window_s=7200, cats=("wx",))
    assert total == 1 and rows[0]["cat"] == "wx"
    total, rows = store.query(38, 40, -95, -93, now=now, window_s=7200, limit=1)
    assert total == 3 and len(rows) == 1 and rows[0]["key"] == "N0NEW"
    assert store.count(38, 40, -95, -93, now=now, window_s=7200, cats=("fixed",)) == 2


def test_station_json_shape(store):
    line = 'Q1ABF-9>TP3Y1R,WIDE1-1,qAR,QA1I:`fJiprgv/`"4^}_4'
    feed_line(store, line, T0)
    feed_line(store, line.replace("`fJi", "`fJj"), T0 + 60)  # ~25 m east... and again
    feed_line(store, line.replace("`fJi", "`fJm"), T0 + 120)
    total, rows = store.query(40, 41, -75, -74, now=T0 + 120, window_s=3600, tracks=True)
    a = rows[0]
    assert set(a) == A_KEYS
    assert a["key"] == a["call"] == a["from"] == "Q1ABF-9" and a["kind"] == "station"
    assert a["sym"] == {"table": "/", "code": "v", "overlay": None}
    assert a["cat"] == "mobile" and a["speed_kmh"] == 88.9 and a["course"] == 275 and a["alt_m"] == 71.0
    assert a["path"] == "WIDE1-1,qAR,QA1I" and a["igate"] == "QA1I"
    assert a["messaging"] is True and a["mice_msg"] == "En Route" and a["emergency"] is False
    assert a["device"] == "Yaesu FTM-500D"
    assert a["first"] == int(T0) and a["last"] == int(T0 + 120)
    assert a["aprsfi_url"] == "https://aprs.fi/#!call=a%2FQ1ABF-9"
    assert len(a["track"]) >= 2 and all(len(p) == 3 for p in a["track"])
    # without tracks the same station carries track = None
    assert store.query(40, 41, -75, -74, now=T0 + 120, window_s=3600)[1][0]["track"] is None


def test_object_json_and_aprsfi_link(store):
    feed_line(store, "Q1ABH>APWW11,STERCT*,WIDE2-1,qAR,QA1AAK:;Q1ABH-1  *162100z/9Hu_<E]U- sTQTH", T0)
    feed_line(store, "WINLINK>APWL2K,TCPIP*,qAS,WLNK:;Q9ABY-10 *160856z3948.  NW08932.  Wa145.610MHz Winlink", T0)
    obj = store.station("obj:Q1ABH-1", now=T0, window_s=3600)
    assert obj["kind"] == "object" and obj["call"] == "Q1ABH-1" and obj["from"] == "Q1ABH"
    assert obj["aprsfi_url"] == "https://aprs.fi/#!call=a%2FQ1ABH-1" and obj["packets"]
    wl = store.station("obj:Q9ABY-10", now=T0, window_s=3600)
    assert wl["amb"] == 2 and wl["freq"] == 145.61 and wl["cat"] == "repeater"
    assert wl["sym"] == {"table": "\\", "code": "a", "overlay": "W"}


def test_near_and_search(store):
    feed_line(store, pos("K0NEAR", 39.10, -94.60), T0)
    feed_line(store, pos("K0FAR", 39.10, -96.60), T0)  # ~172 km west
    feed_line(store, pos("K0NEARB-9", 39.11, -94.61, "/>"), T0)
    total, near = store.near(39.1, -94.6, 50, now=T0, window_s=3600)
    assert total == 2 and [a["key"] for _, a in near] == ["K0NEAR", "K0NEARB-9"]
    assert near[0][0] == pytest.approx(0.0, abs=0.01)
    res = store.search("k0near", now=T0)
    assert [r["key"] for r in res] == ["K0NEAR", "K0NEARB-9"]  # exact first
    assert set(res[0]) == {"key", "call", "cat", "lat", "lon", "last"}
    assert store.search("", now=T0) == [] and store.search("ZZ", now=T0) == []


# ---------------------------------------------------------------- snapshot

def test_snapshot_round_trip_drops_expired(store, data_dir, clock):
    feed_line(store, pos("K0OLD", 39.0, -94.0), T0 - 7 * 3600)  # older than 6 h at T0
    for i in range(3):
        feed_line(store, pos("K0CAR-9", 39.0 + i * 0.01, -94.5, "/>"), T0 - 600 + i * 60)
    feed_line(store, "Q0AAF>APN391,qAR,QA6ABG:;443.775NE*111111z4104.25N/09613.63WrW0EQU", T0 - 60)
    assert aprs_feed.save_snapshot(store)
    path = data_dir / aprs_feed.SNAPSHOT_FILE
    data = json.loads(gzip.decompress(path.read_bytes()))
    assert {s["key"] for s in data["stations"]} == {"K0OLD", "K0CAR-9", "obj:443.775NE"}

    fresh = StationStore(retention_s=6 * 3600)
    assert aprs_feed.load_snapshot(fresh) == 2  # K0OLD expired on load
    assert set(fresh._st) == {"K0CAR-9", "obj:443.775NE"}
    car = fresh.station("K0CAR-9", now=T0, window_s=3600)
    assert len(car["track"]) == 3 and car["cat"] == "mobile" and car["packets"]
    assert fresh.query(38, 42, -100, -90, now=T0, window_s=3600)[0] == 2  # grid rebuilt


def test_snapshot_missing_or_corrupt(store, data_dir, clock):
    assert aprs_feed.load_snapshot(store) == 0
    (data_dir / aprs_feed.SNAPSHOT_FILE).write_bytes(b"not gzip")
    assert aprs_feed.load_snapshot(store) == 0


# ---------------------------------------------------------------- login

@pytest.mark.parametrize("login,ok", [
    ("HAMDOOR-T", True), ("W1AW", True), ("W1AW-10", True), ("N0ABC-HD", True),
    ("HAMDOOR-XX", False),  # 10 characters: aprsc answers "# Invalid username format"
    ("W1AW-100", False), ("W1", False), ("N0CALL", False), ("N0CALL-9", False),
    ("12345678", False),  # 8 hex digits are reserved (q.aspx)
    ("W1 AW", False), ("", False),
])
def test_valid_login(login, ok):
    assert aprs_feed.valid_login(login) is ok


def test_generated_login_persists(data_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "aprs_login", "")
    first = aprs_feed.get_login()
    assert first.startswith("HAMDOOR-") and aprs_feed.valid_login(first) and len(first) <= 9
    assert (data_dir / aprs_feed.LOGIN_FILE).read_text().strip() == first
    monkeypatch.setattr(aprs_feed, "_login_cache", None)  # "restart"
    assert aprs_feed.get_login() == first


def test_configured_login_and_invalid_fallback(data_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "aprs_login", "n0abc-hd")
    assert aprs_feed.get_login() == "N0ABC-HD"
    monkeypatch.setattr(aprs_feed, "_login_cache", None)
    monkeypatch.setattr(get_settings(), "aprs_login", "N0ABC-HAMDOOR")
    assert aprs_feed.get_login().startswith("HAMDOOR-")


def test_login_line_is_receive_only():
    assert aprs_feed.login_line("HAMDOOR-T", "a/72/-180/17/-64") == (
        b"user HAMDOOR-T pass -1 vers hamdoor 1.0.0 filter a/72/-180/17/-64\r\n")
    assert aprs_feed.login_line("HAMDOOR-T", "") == b"user HAMDOOR-T pass -1 vers hamdoor 1.0.0\r\n"


# ---------------------------------------------------------------- connection loop

BANNER = b"# aprsc 2.1.23-test 20 Oct 2026 12:00:00 GMT T2TEST 192.0.2.1:14580\r\n"
LOGRESP = b"# logresp HAMDOOR-T unverified, server T2TEST\r\n"


def _sample_lines():
    return [ln + b"\n" for ln in (FIXTURES / "aprs_sample.txt").read_bytes().split(b"\n") if ln]


@pytest.fixture()
def feed(store, data_dir, clock, monkeypatch):
    monkeypatch.setattr(get_settings(), "aprs_login", "HAMDOOR-T")
    return aprs_feed._Feed(store)


def test_session_logs_in_and_ingests(feed, store, monkeypatch):
    lines = _sample_lines()
    sock = FakeSocket([BANNER, LOGRESP + b"".join(lines[:100]), b"".join(lines[100:])])
    monkeypatch.setattr(aprs_feed, "_open_connection", lambda h, p, t: sock)
    with pytest.raises(ConnectionError, match="closed by server"):
        feed._session()
    assert sock.sent[0] == b"user HAMDOOR-T pass -1 vers hamdoor 1.0.0 filter a/72/-180/17/-64\r\n"
    assert feed.server == "T2TEST" and feed.connected is True
    assert feed.packets == 202 and feed.parsed_total == 178
    assert store.positioned_count() > 100
    assert feed.packets_per_min(aprs_feed._clock()) == 202


def test_session_split_lines_across_reads(feed, store, monkeypatch):
    line = pos("K0SPLIT", 39.0, -94.0).encode() + b"\r\n"
    sock = FakeSocket([BANNER + LOGRESP, line[:10], line[10:25], line[25:]])
    monkeypatch.setattr(aprs_feed, "_open_connection", lambda h, p, t: sock)
    with pytest.raises(ConnectionError):
        feed._session()
    assert "K0SPLIT" in store._st


def test_session_login_refused(feed, monkeypatch):
    sock = FakeSocket([BANNER, b"# Login by user not allowed\r\n"])
    monkeypatch.setattr(aprs_feed, "_open_connection", lambda h, p, t: sock)
    with pytest.raises(aprs_feed.LoginRefused):
        feed._session()


def test_session_silence_reconnects(feed, clock, monkeypatch):
    def quiet():
        clock.advance(5)
        raise socket.timeout()
    sock = FakeSocket([BANNER + LOGRESP] + [quiet] * 20)
    monkeypatch.setattr(aprs_feed, "_open_connection", lambda h, p, t: sock)
    with pytest.raises(TimeoutError, match="no data"):
        feed._session()
    assert clock() - T0 <= aprs_feed.SILENCE_S + 10


def test_session_sends_keepalive_every_15_min(feed, clock, monkeypatch):
    line = pos("K0BEEP", 39.0, -94.0).encode() + b"\r\n"

    def beacon():
        clock.advance(300)
        return line
    sock = FakeSocket([BANNER + LOGRESP] + [beacon] * 7)  # 35 minutes of traffic
    monkeypatch.setattr(aprs_feed, "_open_connection", lambda h, p, t: sock)
    with pytest.raises(ConnectionError):
        feed._session()
    assert sock.sent.count(b"#keepalive\r\n") == 2


def test_run_backs_off_exponentially_then_caps(feed, monkeypatch):
    """Multi-call: refused connects wait ~5, 10, 20 ... capped at 300 s."""
    monkeypatch.setattr(aprs_feed, "_open_connection",
                        lambda h, p, t: (_ for _ in ()).throw(ConnectionRefusedError("refused")))
    delays = []

    def wait(d):
        delays.append(d)
        if len(delays) >= 8:
            feed.stop_event.set()
        return feed.stop_event.is_set()
    monkeypatch.setattr(feed.stop_event, "wait", wait)
    feed.run()
    nominal = [5, 10, 20, 40, 80, 160, 300, 300]
    for got, want in zip(delays, nominal):
        assert want * 0.8 - 1e-9 <= got <= min(300, want * 1.2) + 1e-9
    assert "ConnectionRefusedError" in feed.last_error and feed.connected is False


def test_run_refused_login_waits_the_cap_and_healthy_session_resets(feed, clock, monkeypatch):
    sessions = []

    def connect(h, p, t):
        n = len(sessions)
        sessions.append(n)
        if n == 0:
            return FakeSocket([BANNER, b"# Login by user not allowed\r\n"])
        # a long healthy session (200 s of traffic), then the server drops us
        line = pos("K0OK", 39.0, -94.0).encode() + b"\r\n"

        def tick():
            clock.advance(50)
            return line
        return FakeSocket([BANNER + LOGRESP] + [tick] * 4)
    monkeypatch.setattr(aprs_feed, "_open_connection", connect)
    delays, errors = [], []

    def wait(d):
        delays.append(d)
        errors.append(feed.last_error)
        if len(delays) >= 3:
            feed.stop_event.set()
        return feed.stop_event.is_set()
    monkeypatch.setattr(feed.stop_event, "wait", wait)
    feed.run()
    assert delays[0] >= 240  # refused login: straight to the cap
    assert errors[0] == "Login by user not allowed"
    assert delays[1] <= 6 and delays[2] <= 6  # after healthy sessions: back to ~5 s
    assert errors[2] == "ConnectionError: closed by server"


def test_disabled_feed_starts_no_thread(monkeypatch):
    assert get_settings().aprs_feed_enabled is False  # tests/conftest.py
    aprs_feed.start()
    assert aprs_feed.is_running() is False and aprs_feed._feed is None
    aprs_feed.stop()  # no-op


def test_start_stop_snapshot_multi_call(data_dir, monkeypatch):
    """start -> ingest -> stop writes a snapshot; the next start restores it."""
    s = get_settings()
    monkeypatch.setattr(s, "aprs_feed_enabled", True)
    monkeypatch.setattr(s, "aprs_snapshot_enabled", True)
    monkeypatch.setattr(s, "aprs_login", "HAMDOOR-T")
    monkeypatch.setattr(aprs_feed, "_store", None)
    monkeypatch.setattr(aprs_feed, "_feed", None)
    line = pos("K0LIVE", 39.0, -94.0).encode() + b"\r\n"

    def idle():
        time.sleep(0.01)
        raise socket.timeout()
    monkeypatch.setattr(aprs_feed, "_open_connection",
                        lambda h, p, t: FakeSocket([BANNER + LOGRESP + line] + [idle] * 5000))
    aprs_feed.start()
    try:
        assert aprs_feed.is_running()
        deadline = time.time() + 5
        while "K0LIVE" not in aprs_feed.get_store()._st and time.time() < deadline:
            time.sleep(0.01)
        assert aprs_feed.status()["connected"] is True
        assert aprs_feed.status()["login"] == "HAMDOOR-T"
    finally:
        aprs_feed.stop()
    assert not aprs_feed.is_running()
    assert (data_dir / aprs_feed.SNAPSHOT_FILE).exists()

    monkeypatch.setattr(aprs_feed, "_store", None)
    monkeypatch.setattr(aprs_feed, "_open_connection",
                        lambda h, p, t: (_ for _ in ()).throw(OSError("offline")))
    aprs_feed.start()
    try:
        assert "K0LIVE" in aprs_feed.get_store()._st  # restored before any connection
    finally:
        aprs_feed.stop()


def test_status_shape_when_disabled():
    st = aprs_feed.status()
    assert set(st) == {"enabled", "connected", "server", "login", "filter", "since", "packets",
                       "packets_per_min", "stations", "window_max_s", "last_error"}
    assert st["enabled"] is False and st["connected"] is False
    assert st["window_max_s"] == int(get_settings().aprs_retention_hours * 3600)


# ---------------------------------------------------------------- review fixes (B3-R*)

NAN = float("nan")
INF = float("inf")


def test_store_never_holds_a_non_finite_number(store):
    """B3-R1, defence in depth: whatever reaches ingest, the store keeps no
    NaN/inf. Starlette's JSONResponse refuses them (allow_nan=False) -> 500
    for every response with the station, and math.floor(nan) in the grid
    index would raise inside the feed thread."""
    base = aprs_parser.parse_line(pos("K0NUM", 39.0, -94.0, "/>"))
    assert store.ingest(dict(base, speed_kmh=NAN, course=INF, alt_m=-INF), T0) == "K0NUM"
    a = store.station("K0NUM", now=T0, window_s=3600)
    assert (a["speed_kmh"], a["course"], a["alt_m"]) == (None, None, None)
    json.dumps(a, allow_nan=False)
    for lat, lon in ((NAN, -94.0), (39.0, INF), (91.0, -94.0), (39.0, -181.0)):
        assert store.ingest(dict(base, src="K0BAD", lat=lat, lon=lon), T0) is None
    assert "K0BAD" not in store._st and store.positioned_count() == 1


def test_snapshot_with_non_finite_numbers_loads_clean(store, data_dir, clock):
    """B3-R1: json.dump writes NaN tokens and json.load reads them back, so a
    snapshot saved before the fix carried the poison across restarts."""
    feed_line(store, pos("K0OK", 39.0, -94.0), T0 - 60)
    feed_line(store, pos("K0NAN", 39.1, -94.1), T0 - 60)
    data = store.snapshot(T0)
    ok = next(d for d in data["stations"] if d["key"] == "K0OK")
    nan = next(d for d in data["stations"] if d["key"] == "K0NAN")
    nan.update(alt_m=NAN, speed_kmh=INF, course=NAN)
    nan["track"] = nan["track"] + [[NAN, -94.1, int(T0 - 30)]]
    data["stations"] += [dict(ok, key="K0BADLAT", lat=NAN), dict(ok, key="K0BADTIME", last=NAN)]
    (data_dir / aprs_feed.SNAPSHOT_FILE).write_bytes(gzip.compress(json.dumps(data).encode()))
    fresh = StationStore(retention_s=6 * 3600)
    assert aprs_feed.load_snapshot(fresh) == 2
    assert set(fresh._st) == {"K0OK", "K0NAN"}
    total, rows = fresh.query(38, 40, -95, -93, now=T0, window_s=3600, tracks=True)
    assert total == 2
    json.dumps(rows, allow_nan=False)
    a = fresh.station("K0NAN", now=T0, window_s=3600)
    assert (a["alt_m"], a["speed_kmh"], a["course"]) == (None, None, None)
    assert fresh._st["K0NAN"].track == [(39.1, -94.1, int(T0 - 60))]


def test_status_and_weather_do_not_make_a_stale_position_look_fresh(store):
    """B3-R6: '>' status and '_' weather packets refreshed `last` and the
    window membership, so a 5-hour-old position showed in "last 1 hour" as
    heard 10 minutes ago. Window and `last` follow the position's time;
    last-heard only keeps the record alive (retention)."""
    feed_line(store, pos("K0CAR-9", 39.0, -94.0, "/>", "090/030"), T0 - 5 * 3600)
    feed_line(store, pos("K0WX", 39.2, -94.2, "/_", "090/005t050"), T0 - 5 * 3600)
    feed_line(store, "K0CAR-9>APRS,qAR,K0IG:>On the air", T0 - 600)
    feed_line(store, "K0WX>APRS,qAR,K0IG:_10161529c102s000g000t040h43", T0 - 300)
    assert store.query(38, 40, -95, -93, now=T0, window_s=3600) == (0, [])
    assert store.count(38, 40, -95, -93, now=T0, window_s=3600) == 0
    assert store.near(39.0, -94.0, 50, now=T0, window_s=3600) == (0, [])
    total, rows = store.query(38, 40, -95, -93, now=T0, window_s=6 * 3600)
    assert total == 2 and {r["last"] for r in rows} == {int(T0 - 5 * 3600)}  # the position's age
    car = next(r for r in rows if r["key"] == "K0CAR-9")
    wx = next(r for r in rows if r["key"] == "K0WX")
    assert car["status"] == "On the air" and wx["wx"]["humidity"] == 43  # still attached
    assert {r["last"] for r in store.search("K0", now=T0)} == {int(T0 - 5 * 3600)}
    feed_line(store, pos("K0CAR-9", 39.1, -94.0, "/>"), T0 - 60)  # a new position: current again
    total, rows = store.query(38, 40, -95, -93, now=T0, window_s=3600)
    assert total == 1 and rows[0]["key"] == "K0CAR-9" and rows[0]["last"] == int(T0 - 60)


def test_prune_drops_a_position_older_than_the_retention(store):
    """B3-R6, multi-call: status keeps the record (last heard), but a
    position older than the retention leaves the map, search and detail
    until the station sends a new one."""
    store.retention_s = 3600
    feed_line(store, pos("K0DIG", 39.0, -94.0, "/#"), T0)
    for t in (T0 + 1800, T0 + 3600):
        feed_line(store, "K0DIG>APRS,qAR,K0IG:>digi up", t)
    assert store.prune(T0 + 3000) == 0 and store.positioned_count() == 1  # position 50 min old: kept
    assert store.prune(T0 + 3700) == 0  # the record stays...
    assert "K0DIG" in store._st and store.positioned_count() == 0 and store._grid == {}  # ...the pin goes
    assert store.search("K0DIG", now=T0 + 3700) == []
    assert store.station("K0DIG", now=T0 + 3700, window_s=3600) is None
    feed_line(store, pos("K0DIG", 39.0, -94.0, "/#"), T0 + 3800)
    assert store.positioned_count() == 1
    a = store.station("K0DIG", now=T0 + 3800, window_s=3600)
    assert a["last"] == int(T0 + 3800) and a["status"] == "digi up" and a["first"] == int(T0)
    assert store.prune(T0 + 3800 + 3601) == 1 and len(store) == 0  # silent for an hour: gone


def test_near_builds_json_only_for_the_rows_it_returns(store, monkeypatch):
    """B3-R7: the Activity tab kept 200 rows but built JSON for every station
    in the radius first (18k at 2000 km / 6 h), all under the store lock the
    feed thread needs."""
    for i in range(10):
        feed_line(store, pos(f"K0N{i}", 39.0 + i * 0.01, -94.0), T0)
    built = []
    real = StationStore._json
    monkeypatch.setattr(StationStore, "_json", lambda self, st, *a: built.append(st.key) or real(self, st, *a))
    total, rows = store.near(39.0, -94.0, 50, now=T0, window_s=3600, limit=3)
    assert total == 10 and [a["key"] for _, a in rows] == ["K0N0", "K0N1", "K0N2"]
    assert built == ["K0N0", "K0N1", "K0N2"]
    assert [d for d, _ in rows] == sorted(d for d, _ in rows)
    total, rows = store.near(39.0, -94.0, 50, now=T0, window_s=3600, cats=("wx",), limit=3)
    assert (total, rows) == (0, [])


def test_housekeeping_prunes_every_minute_and_snapshots_every_5(feed, store, clock, monkeypatch):
    """B3-R11, multi-call: 12 one-minute ticks -> a prune at each tick time,
    a snapshot once 300 s have passed since the last one (minutes 5 and
    10); a failing tick is logged and the loop carries on; no snapshots
    when they are disabled."""
    pruned, saved, waits = [], [], []

    def prune(now):
        pruned.append(now)
        if len(pruned) == 3:
            raise RuntimeError("disk on fire")
        return 0
    monkeypatch.setattr(store, "prune", prune)
    monkeypatch.setattr(aprs_feed, "save_snapshot", lambda s: saved.append(aprs_feed._clock()) or True)

    def wait(timeout):
        waits.append(timeout)
        if len(waits) > 12:
            return True  # stop() was called
        clock.advance(timeout)
        return False
    monkeypatch.setattr(feed.stop_event, "wait", wait)
    monkeypatch.setattr(get_settings(), "aprs_snapshot_enabled", True)
    feed.housekeeping()
    assert waits == [aprs_feed.PRUNE_EVERY_S] * 13
    assert pruned == [T0 + 60 * i for i in range(1, 13)]
    assert saved == [T0 + 300, T0 + 600]

    pruned.clear(), saved.clear(), waits.clear()
    monkeypatch.setattr(get_settings(), "aprs_snapshot_enabled", False)
    feed.housekeeping()
    assert len(pruned) == 12 and saved == []


# ---------------------------------------------------------------- second review (B3-2R*)

def test_run_keeps_reconnecting_through_a_multi_day_outage(feed, store, clock, monkeypatch):
    """B3-2R1, multi-call: APRS-IS unreachable for ~5 days (connection
    refused, the odd refused login). run() doubled an unbounded exponent:
    5.0 * 2**1024 raised OverflowError outside the try at the 1,025th
    failure (~3.4 days in), the thread ended for good and is_running() kept
    saying True. It must keep waiting at the cap and reconnect once the
    network is back."""
    attempts = []
    line = pos("K0BACK", 39.0, -94.0).encode() + b"\r\n"

    def connect(h, p, t):
        attempts.append(clock())
        if len(attempts) <= 1500:
            if len(attempts) % 50 == 0:
                return FakeSocket([BANNER, b"# Server full\r\n"])  # refused logins count too
            raise ConnectionRefusedError(111, "Connection refused")
        return FakeSocket([BANNER + LOGRESP + line])  # back: one packet, then closed
    monkeypatch.setattr(aprs_feed, "_open_connection", connect)
    delays = []

    def wait(d):
        delays.append(d)
        clock.advance(d)
        if len(attempts) > 1500:
            feed.stop_event.set()
        return feed.stop_event.is_set()
    monkeypatch.setattr(feed.stop_event, "wait", wait)
    feed.run()
    assert len(attempts) == 1501 and "K0BACK" in store._st  # still alive, reconnected
    assert clock() - T0 > 4 * 86400  # past the ~3.4 days that ended the thread
    assert max(delays) <= aprs_feed.BACKOFF_MAX_S
    assert min(delays[10:]) >= 0.8 * aprs_feed.BACKOFF_MAX_S  # held at the cap, never reset


@pytest.mark.parametrize("attempt", [0, 1, 5, 6, 7, 1023, 1024, 1025, 10**6])
def test_backoff_delay_is_bounded_for_any_attempt(attempt):
    nominal = min(aprs_feed.BACKOFF_MAX_S, aprs_feed.BACKOFF_MIN_S * 2 ** min(attempt, 10))
    for _ in range(50):
        assert 0.8 * nominal - 1e-9 <= aprs_feed.backoff_delay(attempt) <= aprs_feed.BACKOFF_MAX_S
        assert 0.8 * aprs_feed.BACKOFF_MAX_S <= aprs_feed.backoff_delay(attempt, refused=True) <= 300


def test_query_track_budget_goes_to_the_most_recent_stations(store, monkeypatch):
    """B3-2R2: tails depended only on the client's zoom, so a CONUS bbox at
    zoom=10 got every tail in the store, built under the store lock (84k
    points / 5.9 MB / 230 ms locked; 350k / 19 MB / 594 ms at saturation).
    One response now holds at most TRACK_POINTS_MAX points: the most recent
    stations get theirs first, a tail that does not fit is None (a shorter
    one further down may still fit)."""
    monkeypatch.setattr(aprs_feed, "TRACK_POINTS_MAX", 100, raising=False)

    def drive(call, n, last_t):
        for k in range(n):  # ~110 m apart, 30 s apart: every fix is a track point
            feed_line(store, pos(call, 39.0 + k * 0.001, -94.0, "/>"), last_t - (n - 1 - k) * 30)
    drive("K0A-9", 40, T0 - 10)  # the most recent position
    drive("K0B-9", 40, T0 - 20)
    drive("K0C-9", 40, T0 - 30)  # 120 > 100: no room
    drive("K0D-9", 15, T0 - 40)  # fits in the 20 left
    drive("K0E-9", 10, T0 - 50)  # 5 left: no room
    total, rows = store.query(38, 40, -95, -93, now=T0, window_s=3600, tracks=True)
    tails = {r["key"]: len(r["track"]) if r["track"] else None for r in rows}
    assert total == 5 and list(tails) == ["K0A-9", "K0B-9", "K0C-9", "K0D-9", "K0E-9"]
    assert tails == {"K0A-9": 40, "K0B-9": 40, "K0C-9": None, "K0D-9": 15, "K0E-9": None}
    # one station's detail view is not budgeted: its whole tail in the window
    assert len(store.station("K0C-9", now=T0, window_s=3600)["track"]) == 40


def test_track_window_starts_inclusive(store):
    """Guard for the bisect slice that replaced the per-point filter: points
    reported at or after the window start, oldest first; < 2 points -> None."""
    for k, age in enumerate((5000, 3600, 1000, 10)):
        feed_line(store, pos("K0T-9", 39.0 + k * 0.01, -94.0, "/>"), T0 - age)
    a = store.station("K0T-9", now=T0, window_s=3600)
    assert [p[2] for p in a["track"]] == [int(T0 - 3600), int(T0 - 1000), int(T0 - 10)]
    assert json.loads(json.dumps(a["track"]))[0] == [pytest.approx(39.01), pytest.approx(-94.0), int(T0 - 3600)]
    assert store.station("K0T-9", now=T0, window_s=900)["track"] is None


def test_store_bbox_reads_never_raise_on_non_finite_edges(store):
    """B3-2R3: FastAPI accepts "inf" for a float parameter and
    app.geo.clamp_bbox lets min_lat=max_lat=-inf through, so math.floor(-inf)
    in the grid lookup raised OverflowError (HTTP 500)."""
    feed_line(store, pos("K0KC", 39.0, -94.5), T0)
    for edges in ((-INF, -INF, -95, -94), (INF, INF, -95, -94), (38, 40, INF, INF), (38, 40, -INF, -INF),
                  (NAN, NAN, NAN, NAN)):
        assert store.count(*edges, now=T0, window_s=3600) == 0, edges
        assert store.query(*edges, now=T0, window_s=3600, tracks=True) == (0, []), edges
    assert store.count(-INF, INF, -INF, INF, now=T0, window_s=3600) == 1  # infinite edges = unbounded


def _destination(lat, lon, bearing_deg, dist_km):
    """The point dist_km from (lat, lon) along a great circle."""
    d, b = dist_km / 6371.0088, math.radians(bearing_deg)
    p1 = math.radians(lat)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    dl = math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (lon + math.degrees(dl) + 540.0) % 360.0 - 180.0


@pytest.mark.parametrize("centre,station,radius", [
    ((61.2181, -149.9003), (67.0, -110.51), 2000),  # Anchorage: the circle bulges poleward (1978 km)
    ((-61.2181, -149.9003), (-67.0, -110.51), 2000),  # the same south of the equator
    ((61.2181, -149.9003), (67.0, 170.71), 2000),  # the bulge on the far side of the antimeridian
    ((51.88, -176.65), (52.0, 179.0), 400),  # Adak -> across 180 degrees (298 km)
    ((52.0, 179.0), (51.88, -176.65), 400),  # and back
    ((85.0, 0.0), (87.0, 180.0), 1000),  # a pole inside the circle: every longitude (890 km)
])
def test_near_prefilter_holds_the_whole_circle(store, centre, station, radius):
    """B3-2R4: near() widened its bbox by radius / cos(centre latitude), but
    a circle on the sphere spans the most longitude poleward of its centre,
    covers every longitude once it reaches a pole, and wraps at 180 degrees
    (the old box was clamped there), so in-radius stations were never seen."""
    assert aprs_feed._haversine_km(*centre, *station) < radius
    store.ingest({"kind": "position", "src": "K0EDGE", "lat": station[0], "lon": station[1]}, T0 - 60)
    total, rows = store.near(*centre, radius, now=T0, window_s=3600)
    assert total == 1 and rows[0][1]["key"] == "K0EDGE"


def test_near_count_matches_a_brute_force_scan(store):
    """B3-2R4: stations scattered at 0.85-1.15x the radius around centres at
    high latitude, near both poles, on the antimeridian and mid-latitude:
    near() must count exactly what a haversine scan of the store counts,
    and never return a station twice (the antimeridian uses two boxes)."""
    rng = random.Random(42)
    centres = [(61.2181, -149.9003), (-61.2181, -149.9003), (51.88, -176.65), (52.0, 179.0), (85.0, 0.0),
               (-88.0, 45.0), (0.0, 179.5), (40.0, -75.0), (70.0, 30.0)]
    radii = (150, 800, 2000)
    n = 0
    for clat, clon in centres:
        for radius in radii:
            for _ in range(40):
                lat, lon = _destination(clat, clon, rng.uniform(0, 360), radius * rng.uniform(0.85, 1.15))
                store.ingest({"kind": "position", "src": f"K{n}", "lat": lat, "lon": lon}, T0 - 60)
                n += 1
    pts = [(st.lat, st.lon) for st in store._st.values()]
    assert len(pts) == n
    for clat, clon in centres:
        for radius in radii:
            want = sum(1 for lat, lon in pts if aprs_feed._haversine_km(clat, clon, lat, lon) <= radius)
            total, rows = store.near(clat, clon, radius, now=T0, window_s=3600)
            assert total == want, (clat, clon, radius)
            assert len({a["key"] for _, a in rows}) == total


# ---------------------------------------------------------------- late copies and impossible jumps (V1-07)

def test_a_late_copy_of_an_earlier_fix_does_not_move_the_station(store):
    """V1-07 regression: APRS-IS drops repeats only within ~30 s, so a
    digipeated copy of an earlier beacon arrived later and was appended:
    the marker jumped back, the track drew a star and the card showed
    '864 mph' for a car."""
    feed_line(store, pos("K0CAR-9", 38.8435, -94.5, "/>"), T0)
    feed_line(store, pos("K0CAR-9", 38.8533, -94.5, "/>"), T0 + 60)   # ~1.1 km on, 65 km/h
    feed_line(store, pos("K0CAR-9", 38.8435, -94.5, "/>"), T0 + 66)   # the first beacon, again
    st = store._st["K0CAR-9"]
    assert [round(p[0], 4) for p in st.track] == [38.8435, 38.8533]  # old code: + 38.8435
    assert round(st.lat, 4) == 38.8533 and st.pos_t == T0 + 60
    assert st.last == T0 + 66 and len(st.packets) == 3                # heard, logged
    feed_line(store, pos("K0CAR-9", 38.8633, -94.5, "/>"), T0 + 120)  # and it drives on
    assert [round(p[0], 4) for p in st.track] == [38.8435, 38.8533, 38.8633]


def test_a_fix_beyond_ground_speed_waits_for_a_second_one(store):
    feed_line(store, pos("K0CAR-9", 39.0, -94.5, "/>"), T0)
    feed_line(store, pos("K0CAR-9", 39.01, -94.5, "/>"), T0 + 60)
    feed_line(store, pos("K0CAR-9", 39.5, -94.5, "/>"), T0 + 70)     # 54 km in 10 s
    st = store._st["K0CAR-9"]
    assert round(st.lat, 2) == 39.01 and len(st.track) == 2           # held back
    feed_line(store, pos("K0CAR-9", 39.02, -94.5, "/>"), T0 + 120)   # the old track goes on
    assert round(st.lat, 2) == 39.02 and len(st.track) == 3 and st.pending is None
    feed_line(store, pos("K0CAR-9", 40.0, -94.5, "/>"), T0 + 130)
    feed_line(store, pos("K0CAR-9", 40.003, -94.5, "/>"), T0 + 160)  # a second fix agrees
    assert round(st.lat, 3) == 40.003
    assert [round(p[0], 3) for p in st.track] == [40.0, 40.003]       # the track starts again


def test_aircraft_may_move_fast(store):
    feed_line(store, pos("N0PLANE", 39.0, -94.5, "/'"), T0)
    feed_line(store, pos("N0PLANE", 39.2, -94.5, "/'"), T0 + 60)     # 22 km a minute
    assert len(store._st["N0PLANE"].track) == 2


def test_the_held_fix_is_not_in_snapshots(store):
    feed_line(store, pos("K0CAR-9", 39.0, -94.5, "/>"), T0)
    feed_line(store, pos("K0CAR-9", 39.01, -94.5, "/>"), T0 + 60)
    feed_line(store, pos("K0CAR-9", 39.5, -94.5, "/>"), T0 + 70)
    st = store._st["K0CAR-9"]
    assert st.pending is not None
    d = st.to_dict()
    assert "pending" not in d
    assert aprs_feed.Station.from_dict(json.loads(json.dumps(d))).pending is None
