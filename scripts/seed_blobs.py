#!/usr/bin/env python3
"""Seed the file-based layers (FWI NetCDF + HIST PRE/POST Sentinel scenes) into
PostGIS as byte-exact blobs.

FR.FWI / FR.FHIST read these as structured files (netCDF4 / GeoTIFF by filename),
so they cannot be round-tripped as PostGIS rasters without rewriting the engine.
Storing the original bytes and writing them back verbatim on reconstruction keeps
the engine untouched while removing all on-disk INPUT reliance.

Run inside the storcito container (has psycopg2/netCDF4 and sees /app/INPUT):
    docker exec storcito-api-1 micromamba run -n storcito \
        python scripts/seed_blobs.py all          # FWI (all dates) + HIST scenes
    ... python scripts/seed_blobs.py fwi 2         # only first 2 FWI dates (test)
Connection comes from PG* env vars.
"""
from __future__ import annotations

import glob
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import psycopg2

FWI_DATE_RE = re.compile(r"_(\d{8})_\d{4}\.nc4\.nc$")


def _source_dir() -> Path:
    cands = [os.environ.get("SEED_SRC"), "INPUT", "INPUT-old"]
    # Prefer a folder that actually contains FWI data, so an empty INPUT/ that
    # Docker auto-creates for the bind mount doesn't shadow the real INPUT-old/.
    for cand in cands:
        if cand and (Path(cand) / "FWI").is_dir() and any((Path(cand) / "FWI").glob("*.nc")):
            return Path(cand)
    for cand in cands:
        if cand and Path(cand).is_dir():
            return Path(cand)
    raise SystemExit("No INPUT/ or INPUT-old/ source folder with data found")


def _connect():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "gis"),
        user=os.environ.get("PGUSER", "gis"),
        password=os.environ.get("PGPASSWORD", "gis"),
    )


DDL = """
CREATE TABLE IF NOT EXISTS public.fwi_files (
    id       bigserial PRIMARY KEY,
    fdate    date,
    filename text UNIQUE,
    data     bytea NOT NULL,
    nbytes   bigint
);
CREATE INDEX IF NOT EXISTS fwi_files_fdate_idx ON public.fwi_files (fdate);

CREATE TABLE IF NOT EXISTS public.hist_scenes (
    id       bigserial PRIMARY KEY,
    phase    text,
    filename text,
    data     bytea NOT NULL,
    nbytes   bigint,
    UNIQUE (phase, filename)
);

-- peak_temp = max air temperature at the FWI reference level, recorded at seed
-- time so /available-static-dates can rank "warmest day per year" without
-- reading the NetCDF blobs back out.
ALTER TABLE public.fwi_files ADD COLUMN IF NOT EXISTS peak_temp double precision;
"""

# Reference vertical level used by FR.FWI.highest_temperature_fwi_dates.
_FWI_VERTICAL_LEVEL = 15


def _fwi_peak_temp(path: str) -> float | None:
    """Max temperature at the FWI reference level, or None if unreadable."""
    try:
        import netCDF4 as nc
        import numpy.ma as ma

        with nc.Dataset(path) as ds:
            temperature = ds["temp"][_FWI_VERTICAL_LEVEL]
        val = float(ma.masked_invalid(temperature).max())
        return val if val == val else None  # drop NaN
    except Exception:
        return None


def seed_fwi(cur, src: Path, limit: int | None) -> int:
    files = sorted(glob.glob(str(src / "FWI" / "*.nc")))
    if limit:
        files = files[:limit]
    for f in files:
        name = os.path.basename(f)
        m = FWI_DATE_RE.search(name)
        fdate = datetime.strptime(m.group(1), "%Y%m%d").date() if m else None
        peak = _fwi_peak_temp(f)
        data = Path(f).read_bytes()
        cur.execute(
            """INSERT INTO fwi_files (fdate, filename, data, nbytes, peak_temp)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (filename)
               DO UPDATE SET fdate=EXCLUDED.fdate, data=EXCLUDED.data,
                             nbytes=EXCLUDED.nbytes, peak_temp=EXCLUDED.peak_temp""",
            (fdate, name, psycopg2.Binary(data), len(data), peak),
        )
        print(f"  fwi  {name}  {len(data)/1e6:.1f} MB  (fdate={fdate}, peak_temp={peak})", flush=True)
    return len(files)


def seed_hist(cur, src: Path) -> int:
    n = 0
    for phase in ("PRE_FIRE", "POST_FIRE"):
        for f in sorted(glob.glob(str(src / "HIST" / phase / "*.tiff"))):
            name = os.path.basename(f)
            data = Path(f).read_bytes()
            cur.execute(
                """INSERT INTO hist_scenes (phase, filename, data, nbytes)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (phase, filename)
                   DO UPDATE SET data=EXCLUDED.data, nbytes=EXCLUDED.nbytes""",
                (phase, name, psycopg2.Binary(data), len(data)),
            )
            print(f"  hist {phase}/{name}  {len(data)/1e6:.1f} MB", flush=True)
            n += 1
    return n


def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    if what not in {"all", "fwi", "hist"}:
        raise SystemExit("Usage: scripts/seed_blobs.py [all|fwi|hist] [limit]")
    src = _source_dir()
    print(f"[seed_blobs] source={src} what={what} limit={limit}", flush=True)
    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            if what in ("all", "fwi"):
                print(f"FWI: {seed_fwi(cur, src, limit)} files")
            if what in ("all", "hist"):
                print(f"HIST scenes: {seed_hist(cur, src)} files")
        conn.commit()
    finally:
        conn.close()
    print("[seed_blobs] done", flush=True)


if __name__ == "__main__":
    main()
