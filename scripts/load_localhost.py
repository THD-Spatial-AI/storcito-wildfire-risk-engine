#!/usr/bin/env python3
"""Load staged source data (from fetch_sources.py) into PostGIS."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable


FWI_DATE_RE = re.compile(r"_(\d{8})_\d{4}\.nc4\.nc$")


class LoadError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[load_localhost] {msg}", flush=True)


def pg_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PGHOST", "127.0.0.1")
    env.setdefault("PGPORT", "5435")
    env.setdefault("PGDATABASE", "gis")
    env.setdefault("PGUSER", "gis")
    env.setdefault("PGPASSWORD", "gis")
    # Drop NOTICE chatter; warnings and errors still come through.
    env.setdefault("PGOPTIONS", "-c client_min_messages=warning")
    return env


def pg_dsn() -> str:
    env = pg_env()
    return (
        f"PG:host={env['PGHOST']} port={env['PGPORT']} dbname={env['PGDATABASE']} "
        f"user={env['PGUSER']} password={env['PGPASSWORD']}"
    )


def psql_cmd() -> list[str]:
    env = pg_env()
    return [
        "psql",
        "-h",
        env["PGHOST"],
        "-p",
        env["PGPORT"],
        "-U",
        env["PGUSER"],
        "-d",
        env["PGDATABASE"],
        "-v",
        "ON_ERROR_STOP=1",
    ]


def run(cmd: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    log(" ".join(cmd[:8]) + (" ..." if len(cmd) > 8 else ""))
    return subprocess.run(
        cmd,
        input=input_bytes,
        env=pg_env(),
        check=True,
    )


def require_file(path: Path) -> Path:
    if not path.is_file():
        raise LoadError(f"missing file: {path}")
    return path


def raster2pgsql_load(
    path: Path, table: str, srid: int, mode: str, *, constraints: bool = True
) -> None:
    require_file(path)
    pg_mode = {"replace": "-d", "append": "-a"}[mode]
    raster_cmd = [
        "raster2pgsql",
        "-s",
        str(srid),
        pg_mode,
        "-I",
        # -C adds a max-extent constraint that rejects later appends; skip for tiled loads.
        *(["-C"] if constraints else []),
        "-M",
        "-F",
        "-t",
        "256x256",
        str(path),
        f"public.{table}",
    ]
    log(f"raster {table} <- {path}")
    raster = subprocess.run(raster_cmd, capture_output=True, check=True)
    # Quiet: suppress per-row INSERT tags; errors still reach stderr.
    subprocess.run(
        psql_cmd() + ["-q", "-o", "/dev/null"],
        input=raster.stdout,
        env=pg_env(),
        check=True,
    )


def ogr_load(path: Path, table: str, *, t_srs: str | None, overwrite: bool) -> None:
    require_file(path)
    cmd = [
        "ogr2ogr",
        "-f",
        "PostgreSQL",
        pg_dsn(),
        str(path),
        "-nln",
        table,
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-lco",
        "GEOMETRY_NAME=geom",
        "-lco",
        "PRECISION=NO",
    ]
    if t_srs:
        cmd.extend(["-t_srs", t_srs])
    cmd.append("-overwrite" if overwrite else "-append")
    log(f"vector {table} <- {path}")
    run(cmd)


def cmd_load_raster(args: argparse.Namespace) -> int:
    raster2pgsql_load(args.path, args.table, args.srid, args.mode)
    return 0


def cmd_load_vector(args: argparse.Namespace) -> int:
    ogr_load(args.path, args.table, t_srs=args.t_srs, overwrite=args.mode == "replace")
    return 0


def first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


SENTINEL_BANDS = {
    "B04": "sentinel_b4",
    "B08": "sentinel_b8",
    "B8A": "sentinel_b8a",
    "B11": "sentinel_b11",
}

SENTINEL_WINDOW_RE = re.compile(r"^(\d{8})_(\d{8})$")


def psql_sql(sql: str) -> None:
    run(psql_cmd() + ["-q", "-c", sql])


def sentinel_capture_date(args: argparse.Namespace) -> str:
    if args.date:
        return datetime.strptime(args.date, "%Y-%m-%d").date().isoformat()
    match = SENTINEL_WINDOW_RE.match(args.dir.name)
    if not match:
        raise LoadError(
            f"cannot derive capture date from folder name {args.dir.name!r}; pass --date YYYY-MM-DD"
        )
    return datetime.strptime(match.group(2), "%Y%m%d").date().isoformat()


def sentinel_ts_append(path: Path, table: str, srid: int, capture_date: str) -> None:
    """Append one band mosaic into the {table}_ts time-series table."""
    ts = f"{table}_ts"
    staging = f"{table}_ts_stage"
    raster2pgsql_load(path, staging, srid, "replace")
    psql_sql(
        f"""
        CREATE TABLE IF NOT EXISTS public.{ts} (
            rid bigserial PRIMARY KEY,
            rast raster,
            filename text,
            capture_date date NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {ts}_capture_date_idx
            ON public.{ts} (capture_date);
        CREATE INDEX IF NOT EXISTS {ts}_st_convexhull_idx
            ON public.{ts} USING gist (ST_ConvexHull(rast));
        DELETE FROM public.{ts} WHERE capture_date = '{capture_date}';
        INSERT INTO public.{ts} (rast, filename, capture_date)
            SELECT rast, filename, '{capture_date}' FROM public.{staging};
        DROP TABLE public.{staging};
        """
    )
    log(f"{ts} <- {path.name} capture_date={capture_date}")


def cmd_load_sentinel(args: argparse.Namespace) -> int:
    capture_date = sentinel_capture_date(args)
    for band, table in SENTINEL_BANDS.items():
        path = first_existing(
            [
                args.dir / f"{band}.tif",
                args.dir / f"{band}.tiff",
                args.dir / f"{band.lower()}.tif",
                args.dir / f"{band.lower()}.tiff",
            ]
        )
        if path is None:
            if args.allow_missing or band == "B8A":
                log(f"skip {band}: not found in {args.dir}")
                continue
            raise LoadError(f"missing {band}.tif in {args.dir}")
        sentinel_ts_append(path, table, args.srid, capture_date)
        # Engines read {table} as one coverage (mode=2): keep a single window in it.
        if not args.skip_current:
            raster2pgsql_load(path, table, args.srid, "replace")
    return 0


def cmd_load_clcplus(args: argparse.Namespace) -> int:
    tifs = sorted(args.dir.glob("*.tif"))
    if not tifs:
        raise LoadError(f"no .tif tiles found in {args.dir}")
    for idx, path in enumerate(tifs):
        mode = "replace" if idx == 0 and args.mode == "replace" else "append"
        raster2pgsql_load(path, args.table, args.srid, mode, constraints=False)
    psql_sql(f"SELECT AddRasterConstraints('public'::name, '{args.table}'::name, 'rast'::name, 'srid');")
    log(f"{args.table} <- {len(tifs)} tiles")
    return 0


def cmd_load_borders(args: argparse.Namespace) -> int:
    mapping = {
        "spain-municipalities.geojson": "spain_municipalities",
        "spain-provinces.geojson": "spain_provinces",
        "spain-autonomous-communities.geojson": "spain_autonomous_communities",
    }
    for filename, table in mapping.items():
        path = args.dir / filename
        if not path.exists():
            # New fetcher uses source labels, older script uses exact same names
            # for provinces/autonomous communities but this keeps the lookup explicit.
            raise LoadError(f"missing border file: {path}")
        ogr_load(path, table, t_srs="EPSG:4326", overwrite=True)
    run(
        psql_cmd()
        + [
            "-c",
            """
            DROP TABLE IF EXISTS public.spain_national_boundary;
            CREATE TABLE public.spain_national_boundary AS
            SELECT ST_Multi(ST_Union(geom))::geometry(MultiPolygon, 4326) AS geom
            FROM public.spain_provinces;
            CREATE INDEX spain_national_boundary_geom_gist
            ON public.spain_national_boundary USING gist (geom);
            """,
        ]
    )
    return 0


def cmd_load_iuf(args: argparse.Namespace) -> int:
    ogr_load(args.path, "iuf", t_srs=args.t_srs, overwrite=True)
    return 0


def _fwi_peak_temp(path: Path) -> float | None:
    try:
        import netCDF4 as nc  # type: ignore[import-not-found]
        import numpy.ma as ma
    except ImportError:
        return None
    try:
        with nc.Dataset(path) as ds:
            temperature = ds["temp"][15]
        val = float(ma.masked_invalid(temperature).max())
        return val if val == val else None
    except Exception:
        return None


def connect_db():
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LoadError("psycopg2 is required for load-fwi-files") from exc
    env = pg_env()
    return psycopg2.connect(
        host=env["PGHOST"],
        port=env["PGPORT"],
        dbname=env["PGDATABASE"],
        user=env["PGUSER"],
        password=env["PGPASSWORD"],
    )


FWI_DDL = """
CREATE TABLE IF NOT EXISTS public.fwi_files (
    id       bigserial PRIMARY KEY,
    fdate    date,
    filename text UNIQUE,
    data     bytea NOT NULL,
    nbytes   bigint
);
CREATE INDEX IF NOT EXISTS fwi_files_fdate_idx ON public.fwi_files (fdate);
ALTER TABLE public.fwi_files ADD COLUMN IF NOT EXISTS peak_temp double precision;
"""

FWI_CHUNK_BYTES = 64 << 20


def cmd_load_fwi_files(args: argparse.Namespace) -> int:
    files = sorted(args.dir.glob("*.nc"))
    if not files:
        raise LoadError(f"no .nc files found in {args.dir}")
    # Only load the requested window; staging may hold wider earlier fetches.
    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(FWI_DDL)
            skipped = 0
            for path in files:
                match = FWI_DATE_RE.search(path.name)
                fdate = datetime.strptime(match.group(1), "%Y%m%d").date() if match else None
                if fdate is not None and (
                    (start is not None and fdate < start) or (end is not None and fdate > end)
                ):
                    skipped += 1
                    continue
                size = path.stat().st_size
                peak = _fwi_peak_temp(path)
                with path.open("rb") as fh:
                    first = fh.read(FWI_CHUNK_BYTES)
                    cur.execute(
                        """INSERT INTO fwi_files (fdate, filename, data, nbytes, peak_temp)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (filename)
                           DO UPDATE SET fdate=EXCLUDED.fdate, data=EXCLUDED.data,
                                         nbytes=EXCLUDED.nbytes, peak_temp=EXCLUDED.peak_temp""",
                        (fdate, path.name, first, size, peak),
                    )
                    while chunk := fh.read(FWI_CHUNK_BYTES):
                        cur.execute(
                            "UPDATE fwi_files SET data = data || %s WHERE filename = %s",
                            (chunk, path.name),
                        )
                conn.commit()
                log(f"fwi_files <- {path.name} date={fdate} bytes={size} peak_temp={peak}")
            if skipped:
                log(f"skipped {skipped} staged file(s) outside {start}..{end}")
    finally:
        conn.close()
    return 0


FIRMS_CSV_RE = re.compile(r"hotspots_.+_(\d{4})\.csv$")

HIST_DDL = """
CREATE TABLE IF NOT EXISTS public.hist (
    ogc_fid    serial PRIMARY KEY,
    latitude   double precision,
    longitude  double precision,
    brightness double precision,
    scan       double precision,
    track      double precision,
    acq_date   date,
    acq_time   varchar,
    satellite  varchar,
    instrument varchar,
    confidence integer,
    version    varchar,
    bright_t31 double precision,
    frp        double precision,
    daynight   varchar,
    type       bigint,
    year       integer,
    geom       geometry(MultiPoint, 4326)
);
CREATE INDEX IF NOT EXISTS hist_geom_geom_idx ON public.hist USING gist (geom);
"""

HIST_SCENES_DDL = """
CREATE TABLE IF NOT EXISTS public.hist_scenes (
    id       bigserial PRIMARY KEY,
    phase    text,
    filename text,
    data     bytea NOT NULL,
    nbytes   bigint,
    UNIQUE (phase, filename)
);
"""


def cmd_load_firms(args: argparse.Namespace) -> int:
    """Load FIRMS hotspot CSVs into hist (Galicia-clipped; replaces each CSV's year)."""
    import csv

    files = sorted(args.dir.glob("hotspots_*.csv")) if args.dir else [args.file]
    if not files or files == [None]:
        raise LoadError("provide --dir with hotspots_*.csv files or --file")
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(HIST_DDL)
            for path in files:
                match = FIRMS_CSV_RE.search(path.name)
                if not match:
                    raise LoadError(f"cannot parse year from {path.name}")
                year = int(match.group(1))
                rows = list(csv.DictReader(path.open()))
                cur.execute("DELETE FROM hist WHERE year = %s", (year,))
                inserted = 0
                for r in rows:
                    cur.execute(
                        """
                        INSERT INTO hist (latitude, longitude, brightness, scan, track,
                            acq_date, acq_time, satellite, instrument, confidence,
                            version, bright_t31, frp, daynight, type, year, geom)
                        SELECT %(lat)s, %(lon)s, %(brightness)s, %(scan)s, %(track)s,
                            %(acq_date)s, %(acq_time)s, %(satellite)s, %(instrument)s,
                            %(confidence)s, %(version)s, %(bright_t31)s, %(frp)s,
                            %(daynight)s, %(type)s, %(year)s,
                            ST_Multi(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326))
                        WHERE EXISTS (
                            SELECT 1 FROM spain_autonomous_communities g
                            WHERE g.acom_name ILIKE '%%galicia%%'
                              AND ST_Within(
                                    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                                    g.geom)
                        )
                        """,
                        {
                            "lat": float(r["latitude"]),
                            "lon": float(r["longitude"]),
                            "brightness": float(r["brightness"]),
                            "scan": float(r["scan"]),
                            "track": float(r["track"]),
                            "acq_date": r["acq_date"],
                            "acq_time": r["acq_time"],
                            "satellite": r["satellite"],
                            "instrument": r["instrument"],
                            "confidence": int(r["confidence"]),
                            "version": r["version"],
                            "bright_t31": float(r["bright_t31"]),
                            "frp": float(r["frp"]),
                            "daynight": r["daynight"],
                            "type": int(r.get("type") or 0),
                            "year": year,
                        },
                    )
                    inserted += cur.rowcount
                conn.commit()
                log(f"hist <- {path.name}: {inserted} of {len(rows)} rows (Galicia clip) year={year}")
    finally:
        conn.close()
    return 0


def cmd_load_fuels(args: argparse.Namespace) -> int:
    """Rasterize staged MFE fuel-model polygons (20 m, EPSG:32629) into fuels."""
    import tempfile

    require_file(args.geojson)
    out_tif = args.geojson.parent / "FUELS.tif"
    with tempfile.TemporaryDirectory() as work:
        gpkg = Path(work) / "mfe.gpkg"
        run(
            [
                "ogr2ogr",
                "-f",
                "GPKG",
                "-t_srs",
                "EPSG:32629",
                "-nlt",
                "PROMOTE_TO_MULTI",
                str(gpkg),
                str(args.geojson),
            ]
        )
        run(
            [
                "gdal_rasterize",
                "-a",
                args.attribute,
                "-tr",
                "20",
                "20",
                "-ot",
                "Int16",
                "-a_nodata",
                "0",
                "-init",
                "0",
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "TILED=YES",
                str(gpkg),
                str(out_tif),
            ]
        )
    raster2pgsql_load(out_tif, args.table, 32629, "replace")
    log(f"{args.table} <- {out_tif}")
    return 0


def cmd_compute_twi(args: argparse.Namespace) -> int:
    """Compute TWI from staged MDT tiles (GRASS r.fill.dir + r.topidx) into twi."""
    import tempfile

    tiles = sorted(args.dir.glob("*.tif"))
    if not tiles:
        raise LoadError(f"no MDT tiles in {args.dir}; run `make dtm` first")
    out_tif = args.dir.parent.parent / "twi" / "TWI.tif"
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        vrt = Path(work) / "dem.vrt"
        dem = Path(work) / "dem_32629.tif"
        twi = Path(work) / "twi.tif"
        run(["gdalbuildvrt", "-q", str(vrt), *map(str, tiles)])
        run(
            ["gdalwarp", "-q", "-t_srs", "EPSG:32629", "-tr", "25", "25",
             "-r", "bilinear", "-co", "COMPRESS=DEFLATE", str(vrt), str(dem)]
        )
        # float() cast avoids the DCELL->Float32 precision warning; -c skips the
        # color table GDAL rejects on float TIFF bands.
        grass_script = (
            f"r.in.gdal --q input={dem} output=dem && "
            "g.region raster=dem && "
            "r.fill.dir --q input=dem output=dem_filled direction=fdir && "
            "r.topidx --q input=dem_filled output=twi && "
            "r.mapcalc --q expression='twi_f=float(twi)' && "
            f"r.out.gdal --q -c input=twi_f output={twi} format=GTiff "
            "type=Float32 createopt=COMPRESS=DEFLATE"
        )
        run(["grass", "--tmp-project", str(dem), "--exec", "bash", "-c", grass_script])
        import shutil  # move survives crossing filesystems (/tmp vs bind mount)

        shutil.move(str(twi), str(out_tif))
    raster2pgsql_load(out_tif, args.table, 32629, "replace")
    log(f"{args.table} <- {out_tif}")
    return 0


def cmd_load_mdt(args: argparse.Namespace) -> int:
    """Mosaic staged ASTER tiles into the mdt reference grid (30 m, EPSG:32629)."""
    import tempfile

    tiles = sorted(args.dir.glob("ASTGTMV003_*_dem.tif"))
    if not tiles:
        raise LoadError(f"no ASTGTMV003_*_dem.tif tiles in {args.dir}; run dtm-aster first")
    out_tif = args.dir.parent / "mdt" / "MDT.tif"
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        vrt = Path(work) / "dem.vrt"
        run(["gdalbuildvrt", "-q", str(vrt), *map(str, tiles)])
        run(
            ["gdalwarp", "-q", "-t_srs", "EPSG:32629", "-tr", "30", "30",
             "-r", "bilinear", "-co", "COMPRESS=DEFLATE", "-overwrite",
             str(vrt), str(out_tif)]
        )
    raster2pgsql_load(out_tif, args.table, 32629, "replace")
    log(f"{args.table} <- {out_tif} ({len(tiles)} ASTER tiles)")
    return 0


def cmd_load_infra(args: argparse.Namespace) -> int:
    """Load OSM roads + railways from a Geofabrik .pbf into infra (engine uses geometry only)."""
    require_file(args.pbf)
    cmd = [
        "ogr2ogr",
        "-f",
        "PostgreSQL",
        pg_dsn(),
        str(args.pbf),
        "lines",
        "-nln",
        args.table,
        "-where",
        "highway IS NOT NULL OR railway IS NOT NULL",
        "-select",
        "osm_id,name,highway,railway",
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-t_srs",
        "EPSG:4326",
        "-lco",
        "GEOMETRY_NAME=geom",
        "-overwrite",
        "--config",
        "OSM_MAX_TMPFILE_SIZE",
        "4096",
    ]
    log(f"infra <- {args.pbf} (OSM lines: highway or railway)")
    run(cmd)
    psql_sql(f"SELECT count(*) AS infra_features FROM public.{args.table};")
    return 0


def cmd_load_hist_scenes(args: argparse.Namespace) -> int:
    """Load staged B8A/B12 tiffs into hist_scenes (dNBR), using the filename pattern FHIST parses."""
    match = SENTINEL_WINDOW_RE.match(args.dir.name)
    if not match:
        raise LoadError(f"dir name {args.dir.name!r} is not a YYYYMMDD_YYYYMMDD window")
    day = datetime.strptime(match.group(2), "%Y%m%d").date().isoformat()
    if args.phase not in ("PRE_FIRE", "POST_FIRE"):
        raise LoadError("phase must be PRE_FIRE or POST_FIRE")

    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(HIST_SCENES_DDL)
            loaded = 0
            for band in ("B8A", "B12"):
                path = first_existing([args.dir / f"{band}.tif", args.dir / f"{band}.tiff"])
                if path is None:
                    raise LoadError(f"missing {band}.tif in {args.dir}")
                scene_name = f"{day}-00_00_{day}-23_59_Sentinel-2_L2A_{band}_(Raw).tiff"
                data = path.read_bytes()
                cur.execute(
                    """INSERT INTO hist_scenes (phase, filename, data, nbytes)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (phase, filename)
                       DO UPDATE SET data = EXCLUDED.data, nbytes = EXCLUDED.nbytes""",
                    (args.phase, scene_name, data, len(data)),
                )
                loaded += 1
                log(f"hist_scenes <- {args.phase}/{scene_name} ({len(data)} bytes)")
        conn.commit()
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load-raster", help="generic raster2pgsql load")
    p.add_argument("--path", type=Path, required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--srid", type=int, required=True)
    p.add_argument("--mode", choices=["replace", "append"], default="replace")
    p.set_defaults(func=cmd_load_raster)

    p = sub.add_parser("load-vector", help="generic ogr2ogr vector load")
    p.add_argument("--path", type=Path, required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--t-srs", default="EPSG:4326")
    p.add_argument("--mode", choices=["replace", "append"], default="replace")
    p.set_defaults(func=cmd_load_vector)

    p = sub.add_parser(
        "load-sentinel",
        help="append staged band TIFFs to sentinel_*_ts and refresh the current-mosaic tables",
    )
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--date", help="capture date YYYY-MM-DD; default from dir name YYYYMMDD_YYYYMMDD")
    p.add_argument("--srid", type=int, default=4326)
    p.add_argument(
        "--skip-current",
        action="store_true",
        help="only append to the *_ts tables; keep current-mosaic tables untouched",
    )
    p.add_argument("--allow-missing", action="store_true")
    p.set_defaults(func=cmd_load_sentinel)

    p = sub.add_parser("load-clcplus", help="load extracted CLC+ Backbone raster tiles")
    p.add_argument("--dir", type=Path, required=True, help="folder containing the tile .tif files")
    p.add_argument("--table", default="clcplus_2023")
    p.add_argument("--srid", type=int, default=3035)
    p.add_argument("--mode", choices=["replace", "append"], default="replace")
    p.set_defaults(func=cmd_load_clcplus)

    p = sub.add_parser("load-dtm", help="load staged IGN WCS elevation tiles into dtm")
    p.add_argument("--dir", type=Path, required=True, help="folder containing mdt_*.tif tiles")
    p.add_argument("--table", default="dtm")
    p.add_argument("--srid", type=int, default=4258)
    p.add_argument("--mode", choices=["replace", "append"], default="replace")
    p.set_defaults(func=cmd_load_clcplus)

    p = sub.add_parser("load-borders", help="load staged Spain boundary GeoJSON files")
    p.add_argument("--dir", type=Path, required=True)
    p.set_defaults(func=cmd_load_borders)

    p = sub.add_parser("load-iuf", help="load CLC/CORINE vector into public.iuf")
    p.add_argument("--path", type=Path, required=True)
    p.add_argument("--t-srs", default="EPSG:4326")
    p.set_defaults(func=cmd_load_iuf)

    p = sub.add_parser("load-fwi-files", help="load staged MeteoGalicia NetCDF files into fwi_files")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--start", help="only load files dated >= YYYY-MM-DD")
    p.add_argument("--end", help="only load files dated <= YYYY-MM-DD")
    p.set_defaults(func=cmd_load_fwi_files)

    p = sub.add_parser("load-firms", help="load FIRMS hotspot CSVs into hist (replaces those years)")
    p.add_argument("--dir", type=Path, help="folder containing hotspots_*_<year>.csv files")
    p.add_argument("--file", type=Path, help="single hotspots CSV")
    p.set_defaults(func=cmd_load_firms)

    p = sub.add_parser("load-fuels", help="rasterize staged MFE fuel polygons into fuels")
    p.add_argument(
        "--geojson",
        type=Path,
        default=Path("data/OUTPUT/source_data/fuels/mfe_fuels.geojson"),
    )
    p.add_argument("--attribute", default="modcom", help="fuel-model attribute (n_MODCOM for shp)")
    p.add_argument("--table", default="fuels")
    p.set_defaults(func=cmd_load_fuels)

    p = sub.add_parser("compute-twi", help="compute TWI from staged MDT tiles into twi")
    p.add_argument("--dir", type=Path, default=Path("data/OUTPUT/source_data/dtm_cnig/25m"))
    p.add_argument("--table", default="twi")
    p.set_defaults(func=cmd_compute_twi)

    p = sub.add_parser("load-mdt", help="mosaic staged ASTER tiles into the mdt reference grid")
    p.add_argument("--dir", type=Path, default=Path("data/OUTPUT/source_data/dtm_aster"))
    p.add_argument("--table", default="mdt")
    p.set_defaults(func=cmd_load_mdt)

    p = sub.add_parser("load-infra", help="load OSM roads+railways from a .pbf into infra")
    p.add_argument("--pbf", type=Path, required=True)
    p.add_argument("--table", default="infra")
    p.set_defaults(func=cmd_load_infra)

    p = sub.add_parser(
        "load-hist-scenes", help="load staged B8A/B12 tiffs into hist_scenes (dNBR pre/post)"
    )
    p.add_argument("--dir", type=Path, required=True, help="sentinel window dir with B8A/B12.tif")
    p.add_argument("--phase", required=True, choices=["PRE_FIRE", "POST_FIRE"])
    p.set_defaults(func=cmd_load_hist_scenes)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (LoadError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
