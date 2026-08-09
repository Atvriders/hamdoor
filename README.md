# hamdoor

**The neighborhood network for amateur radio operators** — like Nextdoor, but
your neighbors are hams and your address comes from the FCC database. Plus a
live **activity toolbox** that shows the RF world around you.

## FCC directory (every US ham)

On first start the container downloads the FCC's weekly ULS amateur extract
(~200 MB) and imports every active US license — callsign, name, city/state/
ZIP, license class, expiry, and the email the licensee published to the FCC —
into the local database. A background scheduler re-imports weekly
(`ULS_REFRESH_DAYS`, default 7). The import takes a few minutes; the site
works while it runs, and `GET /api/hams/count` shows progress state.

What it's used for:

- **Signup prefill** — the lookup endpoint now answers from the local FCC
  snapshot when callook.info misses or is down, including the **email** when
  the licensee published one (editable before submitting).
- **The map's "All US hams" layer** — every ham in the country. Zoomed out
  you get count bubbles; from zoom 10 in you see **every single ham** as an
  individual pin (up to `HAMS_MAP_MAX_RESULTS` per viewport — zoom further in
  dense metros). Pins are colored by license class (Extra/Advanced/General/
  Technician/Novice/Club), expired-but-in-grace-period licenses show in red,
  and **newly licensed hams (first 90 days, tunable via `NEW_HAM_DAYS`) get a
  gold star**.

**Pin locations are street-level**: after the fast initial import (ZIP-code
centroids, ~1 minute), a background job geocodes every FCC license address
through the free US Census batch geocoder (~1–2 hours, one time). Results are
cached in the database, so the weekly ULS refresh only geocodes new or
changed addresses. Addresses that don't match fall back to a jittered ZIP
centroid so same-ZIP pins never stack into one dot. FCC license addresses are
public record; the API never returns the street text, only coordinates.

## Neighborhood network

- Sign up with your **callsign**; name and address autofill from
  [callook.info](https://callook.info) (US callsigns). Email is added manually —
  public callsign data doesn't include it.
- See **nearby operators** on a Leaflet/OpenStreetMap map (pins sit on grid
  squares, never exact addresses).
- Post to a **local feed** (general, for-sale, nets & events, help wanted,
  Elmer) with comments.
- Set your **neighborhood range from 5 to 500 miles**; the feed and operator
  list only show what's within range.

## Activity toolbox

One tap from the web UI (or straight from the API — same endpoints a mobile
app will use):

| Panel | Source | What you get |
| --- | --- | --- |
| Band conditions | hamqsl.com | SFI/A/K indices, solar wind, day/night HF band outlook |
| PSK Reporter | pskreporter.info | "Heard me" / "I heard" FT8 & digital reception reports with distances |
| WSPR | wspr.live | Your WSPR spots over the last 24 h |
| DX cluster | telnet (`ve7cc.net`) | Live DX spots, sampled in real time |
| CW RBN | telnet (`telnet.reversebeacon.net`) | Live CW skimmer spots — optionally filtered to *your* signal |
| APRS | APRS-IS (`rotate.aprs2.net`) | Live stations/objects within 10–2000 km of you |
| POTA | api.pota.app | Active Parks on the Air activators, with distance |
| SOTA | api2.sota.org.uk | Summit spots from the last 2 h, with distance |

Live telnet/APRS panels open a short connection and sample for
`ACTIVITY_SAMPLE_SECONDS` (default 15 s) — the first click takes a moment,
then results are cached and shared. Panels with distances get a
"within my range only" filter tied to your neighborhood range.

## Run it — one file, no .env needed

The image is built by GitHub Actions and published to GHCR.
**`docker-compose.yml` is the whole configuration.**

```bash
# 1. edit docker-compose.yml and set a random SECRET_KEY
#    (python3 -c "import secrets; print(secrets.token_hex(32))")

# 2. start it
docker compose up -d

# 3. open http://localhost:3033
```

All activity sources, ports, and cache/sample timings can be tuned in the
`environment:` block of `docker-compose.yml` — every option is listed there
with its default.

**Data:** SQLite lives in the `hamdoor_data` named volume by default. To keep
the database on the host instead (easy backups), swap the volume line for a
bind mount like `- /srv/hamdoor:/data` — the container entrypoint
fixes directory ownership automatically at startup (the app itself runs as
unprivileged uid 10001).

To build from source instead of pulling the image:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

## Development (no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DATABASE_URL=sqlite:///./dev.db SECRET_KEY=dev uvicorn app.main:app --reload
# open http://localhost:8000
```

Run the tests:

```bash
pytest -q
```

## API overview

Neighborhood:

| Endpoint | Description |
| --- | --- |
| `GET /api/lookup/{callsign}` | public autofill for the signup form |
| `POST /api/auth/signup` | create account (callsign verified against FCC data) |
| `POST /api/auth/login` | → JWT bearer token |
| `GET/PATCH /api/me` | own profile, address, `range_miles` (5–500) |
| `POST /api/me/password` | change password |
| `POST /api/me/refresh-callsign` | re-sync name/address from FCC data |
| `GET /api/operators/nearby?range=` | hams within range (no addresses/emails exposed) |
| `GET /api/posts/feed?category=&page=` | posts within your range |
| `POST /api/posts`, `GET/PATCH/DELETE /api/posts/{id}` | post CRUD (author-only edits) |
| `POST /api/posts/{id}/comments`, `DELETE /api/comments/{id}` | comments |

Activity toolbox (all require the same JWT):

| Endpoint | Description |
| --- | --- |
| `GET /api/activity/bands` | solar indices + HF band conditions |
| `GET /api/activity/pskreporter?direction=sent\|received` | digital reception reports |
| `GET /api/activity/wspr` | your WSPR reports, last 24 h |
| `GET /api/activity/dxcluster` | live DX cluster sample |
| `GET /api/activity/rbn?mine_only=true` | live CW RBN sample / your own signal reports |
| `GET /api/activity/aprs?radius_km=150` | live APRS stations near you |
| `GET /api/activity/pota` | POTA activator spots |
| `GET /api/activity/sota` | SOTA spots (last 2 h) |

FCC directory:

| Endpoint | Description |
| --- | --- |
| `GET /api/hams/count` | imported hams + last import time |
| `GET /api/hams/map?min_lat=…&max_lat=…&min_lon=…&max_lon=…&zoom=…` | clustered cells (zoom < 10) or individual hams in the viewport |
| `GET /api/hams/{callsign}` | public directory entry (never street/email) |

Interactive docs: `http://localhost:3033/api/docs` (port 8000 in the no-Docker dev setup).

## Troubleshooting

- **"that callsign is already registered" when trying to log in** — you're on
  the **Sign up** tab, not **Log in**. The app now auto-switches you to the
  Log in tab when this happens.
- **"no account registered for …" at login** — the server's database does not
  contain your account. Almost always this means the data location changed
  (e.g. switched from a named volume to a bind mount, or wiped the volume)
  and you're looking at a fresh empty DB. Check
  `curl http://localhost:3033/api/health` — `users` shows how many accounts
  the current database holds. If it's 0, sign up again (and make sure your
  `volumes:` mapping is stable across recreates).
- **"incorrect password"** — the account exists; the password is wrong.
  Passwords are bcrypt-hashed; there is no reset email in v1, so an admin can
  delete the row from `/data/hamdoor.db` (`DELETE FROM users WHERE
  callsign='…'`) and the user can sign up again.
- **Container restart loops** — `docker compose logs hamdoor`. A
  permissions error on `/data` means the bind-mounted host directory isn't
  writable by uid 10001; the entrypoint normally fixes this, and prints the
  manual `chown` command if it can't.

## Privacy model

Other users only ever see your callsign, name, grid square, and distance.
Your street address and email are visible only to you. Activity data is public
RF information (spots, beacons, positions) — nothing private is shared.

## Roadmap

- Android app on the Play Store consuming this same REST API
- Email verification / password reset (needs SMTP)
- Non-US callsign providers (the provider interface is pluggable)
