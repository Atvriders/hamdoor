# hamdoor

**The neighbourhood network for amateur radio operators** — like Nextdoor, but
your neighbours are hams and your identity is your callsign. It pairs a local
feed with a live RF map: every licensed ham in the US, the repeaters around
you, live APRS traffic, and who is activating a park or a summit right now.

## What's in it

**The map** (the home screen)

- **Every licensed US ham** from the FCC's weekly database. Zoomed out you get
  count bubbles placed where the hams actually are; from zoom 10 every licence
  is a dot coloured by class (Technician, General, Extra, and Other for
  Advanced, Novice and club stations), newly licensed hams get a star, expired
  licences still in their grace period a hollow ring, and callsigns appear
  from zoom 14.
- **People on hamdoor** — your registered neighbours (red means a person on
  hamdoor), you, and your neighbourhood range ring.
- **Repeaters** with frequency plates (`2 | 146.940 − T151.4`), filterable by
  band, mode (FM, DMR, D-STAR, Fusion, P25, NXDN, M17…), on-air status and
  linked simplex nodes. Shown from zoom 10.
- **Live APRS traffic**, aprs.fi-style: APRS symbols with callsign labels,
  moving-station tracks, weather stations, digipeaters and iGates, objects,
  and a station card with speed, heading, path and a Follow mode. Shown from
  zoom 9; choose how far back to look (30 minutes, 1 hour or 3 hours).
- **POTA and SOTA activators** on the air right now, and **local posts**.
- **Layers panel** with live counts per category and a filter for each one;
  **search** for a callsign, a place, a ZIP code or an APRS station;
  **basemaps**: Streets, Satellite, Topo and Dark.
- **Made for phones**: the map fills the screen and the controls tuck away
  while you pan, coming back when you stop or tap; lists hide their bars as you
  scroll down and bring them back as you scroll up. Works in landscape (car
  mounts) and with a keyboard or screen reader.

**Feed** — posts from hams within your range (general, for sale, nets and
events with a date and weekly repeat, help wanted, Elmer), replies, and
frequencies in posts that open the matching repeater on the map.

**Nearby** — registered hams in your range. **Activity** — band conditions,
repeaters and APRS stations near you, PSK Reporter, WSPR, DX cluster, CW
Reverse Beacon Network, POTA and SOTA. **You** — your profile, range, map
privacy, theme and password.

## How locations work

Pins come from the address on each FCC licence (public record):

- **Street level** for most hams: a weekly GitHub Actions job geocodes every
  licence address with the free US Census batch geocoder and publishes the
  result as a release asset (`geocodes.sqlite.gz`); each container downloads it
  instead of geocoding ~700k addresses itself. Census answers that name a
  different street than the licence are not trusted.
- **Approximate (ZIP level)** when the address can't be matched — PO boxes,
  rural routes, typos. These licences are grouped into one dashed marker per
  ZIP (at the middle of that ZIP's street-level hams, or the ZIP centroid), so
  they never pose as an exact location.
- **Shared addresses** (households, clubs, mail-forwarding services) are one
  marker with a count, listing every licensee there.
- The importer reads each callsign's **current** licence record; recycled
  callsigns no longer inherit a previous holder's name and address. US
  territories (PR, GU, VI, MP, AS) are included.
- Addresses Census can't match are retried monthly, not on every pass.

Street points are interpolated along the street by the Census geocoder, so a
pin usually sits on the road in front of the house rather than on the roof.

## Run it — one file, no .env needed

The image is built by GitHub Actions and published to GHCR.
**`docker-compose.yml` is the whole configuration** — every option is listed
there with its default.

```bash
# 1. edit docker-compose.yml (optional): APRS_LOGIN = your callsign plus an
#    app SSID for the APRS feed (e.g. N0CALL-HD, 9 characters max; blank = an
#    auto-generated login). SECRET_KEY may stay blank: hamdoor then generates
#    a random signing key once and keeps it in /data/secret_key.

# 2. start it
docker compose up -d

# 3. open http://localhost:3033
```

On the first start the container downloads the FCC extract (~200 MB) and the
geocode release, imports every active licence (a few minutes — the site works
meanwhile), fetches the repeater directory (~25 MB) and connects to APRS-IS.
`GET /api/hams/count`, `/api/repeaters/status` and `/api/aprs/status` show
progress.

- Run **one** uvicorn worker (the default): the APRS feed and its station
  store live in the process.
- **Behind Cloudflare** (a tunnel or the proxy), set `TRUSTED_IP_HEADER:
  "CF-Connecting-IP"` so the log-in, sign-up and lookup limits count each
  visitor rather than the proxy. Leave it blank if the server can also be
  reached directly.
- The container needs outbound HTTPS and TCP port 14580 (APRS-IS).
- **Data** lives in the `hamdoor_data` volume (SQLite plus small caches). To
  keep it on the host instead, swap the volume line for a bind mount like
  `- /path/to/hamdoor-data:/data`; the entrypoint fixes ownership at startup
  (the app runs as uid 10001). The database runs in SQLite's write-ahead-log
  mode, so back it up with `sqlite3 /data/hamdoor.db ".backup backup.db"`
  (or copy `hamdoor.db` together with its `-wal` and `-shm` files) and keep
  `/data` on a local disk or the named volume.

Upgrading from an older version: start the new image on the same volume.
Database changes are applied automatically at startup, and the FCC data is
re-imported once in the background with the corrected importer. Everyone has
to log in once afterwards: sessions are now tied to the password (changing it
ends your other sessions), and a blank, short or placeholder `SECRET_KEY` is
replaced by a generated key.

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

The frontend is plain JavaScript and CSS in `static/` — no build step. Run the
tests (no network needed):

```bash
pytest -q
```

## Data sources

| What | Source | How it's used |
| --- | --- | --- |
| Licensed hams | FCC ULS weekly amateur extract | Imported weekly into the local database |
| Street locations | US Census batch geocoder | Pre-geocoded weekly by CI, published as a release asset |
| ZIP centroids, places | GeoNames postal codes and cities500 (CC BY 4.0) | Approximate locations, place search, repeater sanity checks |
| Repeaters | HearHam.com (primary), BrandMeister (DMR), IRLP | Fetched live by each installation (weekly / daily / twice a day); nothing is redistributed. RepeaterBook is linked to, never copied |
| APRS | APRS-IS (aprs2.net), one receive-only connection | Logs in with `pass -1` — never with anyone's passcode. Cards link to aprs.fi; an optional `APRSFI_API_KEY` enables on-demand callsign lookups |
| Activity | hamqsl.com, PSK Reporter, wspr.live, a DX cluster, the Reverse Beacon Network, POTA, SOTAwatch | Fetched on demand and cached briefly |

## API overview

Everything the web app does goes through this REST API (the same one a mobile
app would use). All endpoints need the bearer token from log in, except
`/api/config`, `/api/health`, `/api/lookup/*` and `/api/auth/*` (those three
groups are rate-limited). Interactive docs: `http://localhost:3033/api/docs`
(paste a token from log in into Authorize); the schema is at `/api/openapi.json`.

| Endpoint | Description |
| --- | --- |
| `POST /api/auth/signup`, `POST /api/auth/login` | create an account (callsign checked against FCC data) / log in → token |
| `GET /api/lookup/{callsign}` | sign-up autofill |
| `GET, PATCH, DELETE /api/me` | your profile (range 5–500 mi, `map_precision` `address` or `grid`); delete your account (needs your password) |
| `POST /api/me/password`, `POST /api/me/refresh-callsign` | change password; re-sync from FCC data |
| `GET /api/operators/nearby` | registered hams in range |
| `GET /api/posts/feed?category=&author=&page=` | posts within your range |
| `POST /api/posts`, `GET/PATCH/DELETE /api/posts/{id}` | posts (events: `event_at`, `repeats_weekly`) |
| `POST /api/posts/{id}/comments`, `DELETE /api/comments/{id}` | replies |
| `GET /api/hams/map?min_lat=&max_lat=&min_lon=&max_lon=&zoom=` | licences in view: count bubbles (with `by_class`) or points; filters `classes`, `expired`, `new`, `approx` |
| `GET /api/hams/search?q=`, `GET /api/hams/{callsign}` | callsign search; one licence (never street or email) |
| `GET /api/hams/count`, `GET /api/hams/near-count` | import progress; licences within your range |
| `GET /api/places/search?q=` | city or ZIP search |
| `GET /api/repeaters/map`, `/near`, `/{id}`, `/status` | repeaters in view (filters `bands`, `modes`, `onair`, `simplex`, `approx`), near you, one repeater, sources and attribution |
| `GET /api/aprs/map`, `/station?key=`, `/search?q=`, `/status`, `/lookup/{callsign}` | live APRS stations in view (window, categories, tracks), one station, search, feed status, aprs.fi lookup (when a key is set) |
| `GET /api/activity/{bands,pskreporter,wspr,dxcluster,rbn,aprs,pota,sota}` | activity toolbox; **502** means the upstream service didn't answer |
| `GET /api/config` | basemaps and feature switches for the browser |

## Privacy model

- **Your pin.** By default you appear at your FCC licence location — the same
  public-record address every licensee has on the map. In You → Map privacy you
  can choose **My grid square** instead: you then appear at the centre of your
  6-character grid square, distances to you are rounded to whole miles, your
  posts carry only that point, and your licence dot on the map no longer marks
  you as a member. (Your FCC record itself is public, and the sign-up and log-in
  forms can still tell someone whether a callsign has an account.)
- Posts are visible to members within range of where they were posted.
- Your **street address and email** are never shown to anyone as text, in any
  response.
- Delete your account any time (You → Delete account): your posts, replies and
  account are removed; your FCC licence stays in the public directory.
- Activity data is public RF information (spots, beacons, positions).

## Troubleshooting

- **"that callsign is already registered" when trying to log in** — you're on
  the **Sign up** tab; the app moves you to Log in.
- **"no account registered for …" at login** — the server's database doesn't
  have your account, usually because the data location changed (a new volume or
  bind mount). `curl http://localhost:3033/api/health` shows how many accounts
  the current database holds.
- **Everyone was logged out after upgrading** — expected once; see the
  upgrade note above.
- **"Too many attempts. Try again in a minute."** — the log-in, sign-up and
  lookup limits. Behind a proxy, set `TRUSTED_IP_HEADER` (see above) so one
  visitor can't use up everyone's allowance.
- **Forgotten password** — there's no email reset yet; an admin can delete the
  account row (`DELETE FROM users WHERE callsign='…'` in `/data/hamdoor.db`) so
  the person can sign up again.
- **No APRS traffic** — check `/api/aprs/status`: `connected`, `server` and
  `last_error`. Some APRS-IS servers refuse generic logins; set `APRS_LOGIN` to
  your callsign plus an SSID.
- **Container restart loops** — `docker compose logs hamdoor`; a permissions
  error on `/data` means the bind-mounted directory isn't writable by uid 10001.

## Credits

Map data © OpenStreetMap contributors; vector tiles by OpenFreeMap / OpenMapTiles;
imagery © Esri and its data providers; topographic tiles courtesy of the U.S.
Geological Survey. Repeater data from HearHam.com, BrandMeister and IRLP. APRS
data via APRS-IS (aprs2.net); APRS symbols from
[hessu/aprs-symbols](https://github.com/hessu/aprs-symbols) (see
`static/img/aprs/COPYRIGHT.md`); device names from aprs-deviceid by OH7LZB
(CC BY-SA 2.0). Place data © GeoNames (CC BY 4.0). Licence data from the FCC
Universal Licensing System; geocoding by the US Census Bureau. Typeface:
Atkinson Hyperlegible Next by the Braille Institute (SIL Open Font License,
`static/fonts/OFL.txt`). Maps drawn with Leaflet and MapLibre GL.

## Roadmap

- Android app on the same REST API
- Password reset by email (needs SMTP)
- Rooftop-level locations from national address-point data
- Non-US callsign providers (the provider interface is pluggable)
