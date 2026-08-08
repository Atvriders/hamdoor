# hamdoor

**The neighborhood network for amateur radio operators** — like Nextdoor, but
your neighbors are hams and your address comes from the FCC database.

- Sign up with your **callsign**; name and address autofill from
  [callook.info](https://callook.info) (US callsigns). Email is added manually —
  public callsign data doesn't include it.
- See **nearby operators** on a Leaflet/OpenStreetMap map (pins sit on grid
  squares, never exact addresses).
- Post to a **local feed** (general, for-sale, nets & events, help wanted,
  Elmer) with comments.
- Set your **neighborhood range from 5 to 500 miles**; the feed and operator
  list only show what's within range.
- API-first (JWT bearer tokens, OpenAPI at `/api/docs`) so a Play Store app can
  reuse the exact same backend later.

## Run with Docker (the intended way)

The image is built by GitHub Actions and published to GHCR.

```bash
cp .env.example .env
# put a random SECRET_KEY in .env:
python3 -c "import secrets; print(secrets.token_hex(32))"

docker compose up -d
# open http://localhost:8000
```

To build locally instead of pulling from GHCR:

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

Interactive docs: `http://localhost:8000/api/docs`.

## Privacy model

Other users only ever see your callsign, name, grid square, and distance.
Your street address and email are visible only to you.

## Roadmap

- Android app on the Play Store consuming this same REST API
- Email verification / password reset (needs SMTP)
- Non-US callsign providers (the provider interface is pluggable)
