#!/usr/bin/env python3
"""Build the weekly geocode data release.

Runs a full FCC ULS import plus a complete Census geocode backfill, then
exports the persistent `geocodes` cache table as geocodes.sqlite.gz for
publishing as a GitHub Release asset. hamdoor containers download that
artifact instead of geocoding ~700k addresses themselves.

Intended to run in CI (takes ~1.5–3 h, mostly Census throttling waits).
"""

import gzip
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db  # noqa: E402

init_db()

from app.integrations import uls  # noqa: E402
from app.config import get_settings  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build")
DB_PATH = get_settings().database_url.split("///", 1)[-1]


def main():
    print("[release] importing ULS + geocoding all addresses (this takes hours)…")
    uls.import_hams()
    added = uls.geocode_backfill()
    print(f"[release] backfill added {added} geocodes")

    raw = os.path.join(OUT_DIR, "geocodes.sqlite")
    gz = raw + ".gz"
    os.makedirs(OUT_DIR, exist_ok=True)
    for p in (raw, gz):
        if os.path.exists(p):
            os.remove(p)

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(raw)
    dst.execute("CREATE TABLE geocodes (address_key TEXT PRIMARY KEY, lat REAL, lon REAL, quality TEXT)")
    dst.executemany(
        "INSERT INTO geocodes VALUES (?, ?, ?, ?)",
        src.execute("SELECT address_key, lat, lon, quality FROM geocodes"),
    )
    dst.commit()
    total = dst.execute("SELECT COUNT(*) FROM geocodes").fetchone()[0]
    dst.close()
    src.close()

    with open(raw, "rb") as f_in, gzip.open(gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw)
    print(f"[release] {gz}: {total} geocodes, {os.path.getsize(gz)/1e6:.1f} MB")
    if total < 400_000:
        sys.exit(f"[release] suspiciously few geocodes ({total}) — refusing to publish")


if __name__ == "__main__":
    main()
