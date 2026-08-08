# hamdoor

**The neighborhood network for amateur radio operators** — like Nextdoor, but
your neighbors are hams and your address comes from the FCC database. Plus a
live **activity toolbox** that shows the RF world around you.

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

Interactive docs: `http://localhost:3033/api/docs` (port 8000 in the no-Docker dev setup).

## Privacy model

Other users only ever see your callsign, name, grid square, and distance.
Your street address and email are visible only to you. Activity data is public
RF information (spots, beacons, positions) — nothing private is shared.

## Roadmap

- Android app on the Play Store consuming this same REST API
- Email verification / password reset (needs SMTP)
- Non-US callsign providers (the provider interface is pluggable)
