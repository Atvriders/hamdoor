"""US Census Bureau batch geocoder (free, no key, US addresses).

POSTs CSV batches of up to ~10k addresses to
https://geocoding.geo.census.gov/geocoder/locations/addressbatch and parses
the matched lon,lat back out. Used to upgrade hams from ZIP-centroid
locations to street-level locations from their FCC address.
"""

import csv
import io
import logging
import time

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


def address_key(street: str, city: str, state: str, zip_: str) -> str:
    """Normalized cache key for one address."""
    norm = lambda s: " ".join((s or "").upper().split())
    return f"{norm(street)}|{norm(city)}|{norm(state)}|{norm(zip_)[:5]}"


def parse_batch_response(text: str) -> dict[str, tuple[float, float, str]]:
    """Parse the batch CSV response.

    Columns: id, input address, status, match type, matched address,
    "lon,lat", tigerLineId, side. Returns {input_id: (lat, lon, quality)}.
    """
    out: dict[str, tuple[float, float, str]] = {}
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) < 6 or row[2] not in ("Match", "Tie"):
            continue
        try:
            lon_s, lat_s = row[5].split(",")
            out[row[0]] = (float(lat_s), float(lon_s), row[3] or row[2])
        except (ValueError, IndexError):
            continue
    return out


def _post_chunk(client: httpx.Client, url: str, csv_text: str, label: str,
                expected: int = 1) -> dict:
    """POST one chunk with backoff — the Census service throttles under load
    (read timeouts, 502s, or a 200 response that is nearly all No_Match), so
    retry instead of skipping the whole batch."""
    delay = 30.0
    for attempt in range(1, 5):
        try:
            resp = client.post(
                url,
                data={"benchmark": "Public_AR_Current"},
                files={"addressFile": ("addresses.csv", csv_text, "text/csv")},
            )
            if resp.status_code == 200:
                matched = parse_batch_response(resp.text)
                # a 200 that matches <20% of rows is a throttled junk reply,
                # not a real result (this dataset normally matches ~95%)
                if expected and len(matched) < 0.2 * expected and attempt < 4:
                    log.warning("[geocode] %s: only %d/%d matched — throttled response, backing off",
                                label, len(matched), expected)
                    time.sleep(delay)
                    delay *= 2
                    continue
                return matched
            log.warning("[geocode] %s HTTP %s (attempt %d/4)", label, resp.status_code, attempt)
        except httpx.HTTPError as exc:
            log.warning("[geocode] %s failed (attempt %d/4): %s", label, attempt, exc)
        if attempt < 4:
            time.sleep(delay)
            delay *= 2  # 30s, 60s, 120s between retries
    log.warning("[geocode] %s skipped after retries (will be retried at next scheduled run)", label)
    return {}


def geocode_batch(rows: list[tuple[str, str, str, str, str]],
                  pause_seconds: float = 1.0,
                  on_chunk=None) -> dict[str, tuple[float, float, str]]:
    """Geocode (id, street, city, state, zip) rows in Census-sized chunks.

    Returns {id: (lat, lon, quality)} for matched addresses only.
    `on_chunk(matched_dict)` fires after every chunk so callers can persist
    incrementally instead of waiting for the whole run.
    """
    s = get_settings()
    results: dict[str, tuple[float, float, str]] = {}
    chunk = s.census_batch_size
    total_chunks = (len(rows) + chunk - 1) // chunk

    buf = io.StringIO()
    with httpx.Client(timeout=s.census_timeout_seconds,
                      headers={"User-Agent": s.http_user_agent}) as client:
        for i in range(0, len(rows), chunk):
            part = rows[i : i + chunk]
            buf.seek(0)
            buf.truncate(0)
            w = csv.writer(buf)
            for rid, street, city, state, zip_ in part:
                w.writerow([rid, street, city, state, zip_])
            n = i // chunk + 1
            matched = _post_chunk(client, s.census_url, buf.getvalue(),
                                  f"batch {n}/{total_chunks}", expected=len(part))
            if matched:
                results.update(matched)
                log.info("[geocode] batch %d/%d: %d/%d matched",
                         n, total_chunks, len(matched), len(part))
                if on_chunk:
                    on_chunk(matched)
            # back off politely after a failed batch; 1s after successes
            time.sleep(pause_seconds if matched else 30.0)
    return results
