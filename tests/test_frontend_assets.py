"""The single-page shell: every local asset index.html references exists,
and the served page cache-busts each script and stylesheet."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


def _refs():
    html = (STATIC / "index.html").read_text()
    return re.findall(r'(?:src|href)="(/static/[^"?#]+)"', html)


def test_every_local_asset_exists():
    refs = _refs()
    assert refs, "index.html references no local assets"
    missing = [r for r in refs if not (STATIC / r[len("/static/"):]).is_file()]
    assert not missing, f"index.html references missing files: {missing}"


def test_the_door_mark_is_the_favicon(client):
    """QA BE-8: no icon, and every visit's /favicon.ico was a 404 (cached by the CDN for hours)."""
    html = (STATIC / "index.html").read_text()
    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in html
    svg = client.get("/static/favicon.svg")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")
    assert 'stroke="currentColor"' not in svg.text and "prefers-color-scheme: dark" in svg.text
    ico = client.get("/favicon.ico")
    assert ico.status_code == 200 and ico.headers["content-type"].startswith("image/svg+xml")
    assert ico.content == svg.content
    assert client.head("/favicon.ico").status_code == 200


def test_index_serves_versioned_assets(client):
    html = client.get("/").text
    for ref in _refs():
        if ref.endswith((".js", ".css")):
            assert re.search(re.escape(ref) + r"\?v=[0-9a-f]{10}", html), f"{ref} is not versioned"


# the fixed script list and order (build/CONTRACT-frontend.md §1)
CONTRACT_SCRIPTS = [
    "/static/js/hd-core.js",
    "/static/js/hd-icons.js",
    "/static/js/hd-chrome.js",
    "/static/js/map/map-core.js",
    "/static/js/map/map-marks.js",
    "/static/js/map/map-ui.js",
    "/static/js/map/layer-fcc.js",
    "/static/js/map/layer-people.js",
    "/static/js/map/layer-repeaters.js",
    "/static/js/map/layer-aprs.js",
    "/static/js/map/layer-spots.js",
    "/static/js/map/layer-posts.js",
    "/static/js/views/auth.js",
    "/static/js/views/feed.js",
    "/static/js/views/nearby.js",
    "/static/js/views/activity.js",
    "/static/js/views/you.js",
    "/static/js/app.js",
]


def test_index_keeps_the_contract_script_order():
    html = (STATIC / "index.html").read_text()
    scripts = re.findall(r'<script\s+src="([^"]+)"', html)
    assert scripts[0].startswith("https://unpkg.com/leaflet@1.9.4/"), "Leaflet loads first"
    assert scripts[1:] == CONTRACT_SCRIPTS


def test_index_keeps_the_contract_markup():
    html = (STATIC / "index.html").read_text()
    for name in ("auth", "map", "feed", "nearby", "activity", "you"):
        sections = re.findall(rf'<section data-view="{name}" class="hd-view\b[^"]*" hidden>', html)
        assert len(sections) == 1, f"one hidden <section data-view={name!r} class='hd-view'> per route"
    assert re.search(r'<div id="hd-map" class="hd-map" tabindex="0" aria-label="Map"></div>\s*<div id="hd-map-ui"></div>', html)
    assert 'id="hd-toasts"' in html


def test_the_old_frontend_is_gone():
    assert not (STATIC / "app.js").exists() and not (STATIC / "style.css").exists()
    html = (STATIC / "index.html").read_text()
    assert "/static/app.js" not in html and "/static/style.css" not in html


def test_no_eval_or_inline_event_handlers():
    """Contract §0: no eval, no inline event-handler attributes (in index.html or in HTML the scripts build)."""
    handler = re.compile(r"<[a-z][^<>]*\son(?:click|load|error|submit|input|change|key\w+|mouse\w+|pointer\w+|touch\w+|focus|blur)\s*=", re.I)
    files = [STATIC / "index.html", *sorted((STATIC / "js").rglob("*.js"))]
    for f in files:
        text = f.read_text()
        assert not re.search(r"(?<![\w.])eval\s*\(|new\s+Function\s*\(", text), f"eval in {f.name}"
        assert not handler.search(text), f"inline event handler attribute in {f.name}"


def test_config_is_public(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    ids = [b["id"] for b in body["basemaps"]]
    assert ids[0] == "streets" and "osm" in ids
    assert set(body["features"]) >= {"repeaters", "aprs", "aprsfi_lookup"}


def test_a_delete_sent_as_the_session_ends_still_counts(client, db):
    """Review RF1-06. Log out is client-side, so the web app sends a delete still waiting for its Undo toast
    (DESIGN_SYSTEM §14.5) with the session's token while the session ends (HD.session.clear() emits
    "session-ending" first; feed.js commits with keepalive). The server honours it. Sent after the token is
    forgotten, the old behaviour, it is refused and the post stays published to every neighbour."""
    from tests.conftest import auth, set_location, signup

    r = client.post("/api/auth/login", json={"callsign": "W1DDD", "password": "password123"})
    token = r.json()["token"] if r.status_code == 200 else signup(client, "W1DDD")["token"]
    set_location(db, "W1DDD", 41.69, -72.73)
    pid = client.post("/api/posts", headers=auth(token), json={"title": "Delete me, then log out", "body": "x", "category": "general"}).json()["id"]
    cid = client.post(f"/api/posts/{pid}/comments", headers=auth(token), json={"body": "and this reply"}).json()["id"]

    # without the token (a delete fired after Log out forgot it): refused, both stay
    assert client.delete(f"/api/comments/{cid}").status_code == 401
    assert client.delete(f"/api/posts/{pid}").status_code == 401
    assert client.get(f"/api/posts/{pid}", headers=auth(token)).json()["comment_count"] == 1

    # with it (what the app sends as the session ends): gone
    assert client.delete(f"/api/comments/{cid}", headers=auth(token)).status_code == 204
    assert client.delete(f"/api/posts/{pid}", headers=auth(token)).status_code == 204
    assert client.get(f"/api/posts/{pid}", headers=auth(token)).status_code == 404

    # the web app's side of it: the views hear "session-ending" before the token is dropped
    core = (STATIC / "js" / "hd-core.js").read_text()
    clear = re.search(r"\n    clear\(reason\) \{\n(.*?)\n    \},", core, re.S)
    assert clear, "HD.session.clear(reason) not found"
    body = clear.group(1)
    assert body.index('HD.emit("session-ending"') < body.index("this.set(null, null)")
    feed = (STATIC / "js" / "views" / "feed.js").read_text()
    assert re.search(r'HD\.on\("session-ending".*?commitDelete\(id, true\).*?commitDeleteComment\(cid, true\)', feed, re.S)


def test_theme_color_metas_carry_the_surface_tokens():
    """Review RF1-10. The two theme-color metas in index.html (right before any script runs, and for System) are
    the light and dark surface tokens, keyed on prefers-color-scheme; hd-core.js gives both the chosen theme's
    surface colour when the person picks Light or Dark, so the browser's bars never show the OS scheme's colour."""
    html = (STATIC / "index.html").read_text()
    tokens = (STATIC / "css" / "tokens.css").read_text()
    light = re.search(r"--hd-stock:\s*(#[0-9a-fA-F]{3,8})", tokens).group(1).lower()
    dark = re.search(r':root\[data-theme="dark"\]\s*\{[^}]*?--hd-stock:\s*(#[0-9a-fA-F]{3,8})', tokens, re.S).group(1).lower()
    metas = {scheme: colour.lower() for colour, scheme in re.findall(
        r'<meta name="theme-color" content="(#[0-9a-fA-F]{3,8})" media="\(prefers-color-scheme: (light|dark)\)">', html)}
    assert metas == {"light": light, "dark": dark}
    core = (STATIC / "js" / "hd-core.js").read_text()
    assert "meta[name=\"theme-color\"]" in core and '"--hd-surface"' in core


def test_keyboard_focus_is_scrolled_clear_of_the_bars():
    """QA F11 (WCAG 2.2 SC 2.4.11): list pages scroll the document under a fixed (phone) or sticky (desktop) top bar
    and the phone tab bar; tabbing hid every third row under the tab bar. The page's scroll-padding keeps what takes
    focus clear of both: hd-chrome.js publishes the visible top bar's height as --hd-scroll-top."""
    css = (STATIC / "css" / "app.css").read_text()
    assert re.search(r"html \{ scroll-padding: calc\(var\(--hd-scroll-top, 0px\)", css)
    assert re.search(r"scroll-padding-bottom: calc\(var\(--hd-tab-h\) \+ var\(--hd-safe-bottom\)", css)
    chrome = (STATIC / "js" / "hd-chrome.js").read_text()
    assert 'root.style.setProperty("--hd-scroll-top"' in chrome
