#!/usr/bin/env python3
"""Galicia recurring data pipeline (single CLI, runs inside the geotools container).

Downloads the two time-varying wildfire inputs for the Galicia region and loads
them into the existing PostGIS tables with region + date metadata:

  * sentinel  -- Sentinel-2 B04/B08/B8A/B11, weekly  -> s2_<band>
  * meteo     -- MeteoGalicia WRF (FWI inputs), daily -> fwi_<var>

Rasters are loaded with PostGIS ``ST_FromGDALRaster`` over psycopg2 (no
raster2pgsql): the GeoTIFF bytes are sent to the server, turned into a raster,
tiled to 256x256 and appended. Because we write the INSERT ourselves we control
the schema, so region/date columns are populated in the same statement.

Run (inside the geotools container, repo mounted at /data):
    python3 /data/scripts-galicia/galicia.py export-aoi
    python3 /data/scripts-galicia/galicia.py meteo  [--date YYYY-MM-DD]
    python3 /data/scripts-galicia/galicia.py sentinel [--date-from YYYY-MM-DD --date-to YYYY-MM-DD]

DB connection comes from the container env (PGHOST=postgis, PGUSER, ...).
Copernicus OAuth creds + overrides are read from scripts-galicia/.env.galicia.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
from psycopg2 import sql
import requests

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

# --- config / env -----------------------------------------------------------
def load_env() -> None:
    """Load scripts-galicia/.env.galicia (KEY=VALUE) into os.environ if present."""
    f = HERE / ".env.galicia"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} [galicia] {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} [galicia][ERROR] {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


REGION = os.environ.get("REGION", "Galicia")
ACOM_NAME = os.environ.get("ACOM_NAME", "Galicia")

# Sentinel Hub / Copernicus Data Space
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"
SENTINEL_BANDS = ("B04", "B08", "B8A", "B11")

# MeteoGalicia WRF (same grid as the existing fwi_* tables -> no clipping)
WRF_BASE = "https://thredds.meteogalicia.gal/thredds/ncss/grid/modelos/WRF_ARW_1KM_HIST"


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    params = {
        "host": os.environ.get("PGHOST", "postgis"),
        "port": os.environ.get("PGPORT", "5432"),
        "dbname": os.environ.get("PGDATABASE", "gis"),
        "user": os.environ.get("PGUSER", "gis"),
        "password": os.environ.get("PGPASSWORD", "gis"),
    }
    return " ".join(f"{k}={v}" for k, v in params.items())


def connect():
    return psycopg2.connect(_dsn())


# --- raster load (replaces raster2pgsql) ------------------------------------
def require_table(conn, table: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
        if not cur.fetchone()[0]:
            die(
                f"Required table public.{table} does not exist. Run the one-time bulk "
                "load first (scripts/load-ndxi.sh creates s2_*, scripts/load-fwi.sh creates fwi_*)."
            )


def load_raster(conn, table: str, tif_bytes: bytes, *, filename: str, region: str, dates: dict) -> int:
    """Append a single-band GeoTIFF into `table`, tiled, with region/date metadata.

    `dates` maps date column name -> ISO date string (e.g. {"date_from":..,"date_to":..}
    for Sentinel or {"fdate":..} for meteo). Idempotent: existing rows for the same
    region + dates are deleted first.
    """
    tbl = sql.Identifier(table)
    date_cols = list(dates.keys())
    with conn.cursor() as cur:
        cur.execute("SET postgis.gdal_enabled_drivers = 'GTiff';")
        # Drop all raster constraints so a differently-aligned raster can be appended.
        cur.execute(
            "SELECT DropRasterConstraints('public', %s, 'rast', "
            "TRUE,TRUE,TRUE,TRUE,TRUE,TRUE,TRUE,TRUE,TRUE,TRUE,TRUE,TRUE)",
            (table,),
        )
        add_cols = [sql.SQL("ADD COLUMN IF NOT EXISTS region text")]
        add_cols += [
            sql.SQL("ADD COLUMN IF NOT EXISTS {} date").format(sql.Identifier(c)) for c in date_cols
        ]
        cur.execute(
            sql.SQL("ALTER TABLE {tbl} {cols}").format(tbl=tbl, cols=sql.SQL(", ").join(add_cols))
        )

        # Idempotent clear of any prior load for this region + date(s).
        pred = [sql.SQL("region = %s")] + [
            sql.SQL("{} = %s").format(sql.Identifier(c)) for c in date_cols
        ]
        cur.execute(
            sql.SQL("DELETE FROM {tbl} WHERE {pred}").format(
                tbl=tbl, pred=sql.SQL(" AND ").join(pred)
            ),
            [region, *dates.values()],
        )

        cols = [sql.Identifier("rast"), sql.Identifier("filename"), sql.Identifier("region")]
        cols += [sql.Identifier(c) for c in date_cols]
        placeholders = sql.SQL(", ").join(sql.Placeholder() * (len(date_cols) + 2))
        cur.execute(
            sql.SQL(
                "INSERT INTO {tbl} ({cols}) "
                "SELECT ST_Tile(ST_FromGDALRaster(%s), 256, 256), {ph}"
            ).format(tbl=tbl, cols=sql.SQL(", ").join(cols), ph=placeholders),
            [psycopg2.Binary(tif_bytes), filename, region, *dates.values()],
        )
        inserted = cur.rowcount

        cur.execute(
            sql.SQL(
                "CREATE INDEX IF NOT EXISTS {idx} ON {tbl} USING gist (ST_ConvexHull(rast))"
            ).format(idx=sql.Identifier(f"{table}_rast_gist"), tbl=tbl)
        )
        cur.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {idx} ON {tbl} (region)").format(
                idx=sql.Identifier(f"{table}_region_idx"), tbl=tbl
            )
        )
    return inserted


# --- AOI --------------------------------------------------------------------
def fetch_aoi(conn) -> tuple[dict, list[float]]:
    """Return (GeoJSON geometry, [minLon,minLat,maxLon,maxLat]) for the region polygon."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_AsGeoJSON(ST_Union(geom)) FROM spain_autonomous_communities WHERE acom_name = %s",
            (ACOM_NAME,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            die(f"No polygon found for acom_name='{ACOM_NAME}'.")
        geom = json.loads(row[0])
        cur.execute(
            "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
            "FROM (SELECT ST_Extent(geom) e FROM spain_autonomous_communities WHERE acom_name = %s) s",
            (ACOM_NAME,),
        )
        bbox = [float(v) for v in cur.fetchone()]
    return geom, bbox


def cmd_export_aoi(args) -> None:
    conn = connect()
    try:
        geom, bbox = fetch_aoi(conn)
    finally:
        conn.close()
    out = REPO_ROOT / "INPUT" / "AOI" / "galicia.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "Feature", "properties": {"region": REGION}, "geometry": geom}))
    log(f"AOI written: {out} bbox={bbox}")


# --- Sentinel ---------------------------------------------------------------
EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04","B08","B8A","B11"], units: "DN" }],
    output: [
      { id: "B04", bands: 1, sampleType: "UINT16" },
      { id: "B08", bands: 1, sampleType: "UINT16" },
      { id: "B8A", bands: 1, sampleType: "UINT16" },
      { id: "B11", bands: 1, sampleType: "UINT16" }
    ]
  };
}
function evaluatePixel(s) { return { B04:[s.B04], B08:[s.B08], B8A:[s.B8A], B11:[s.B11] }; }
"""


def oauth_token() -> str:
    token = os.environ.get("ACCESS_TOKEN", "").strip()
    if token:
        return token
    cid = os.environ.get("SH_CLIENT_ID", "").strip()
    secret = os.environ.get("SH_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        die("Set SH_CLIENT_ID and SH_CLIENT_SECRET in scripts-galicia/.env.galicia (or ACCESS_TOKEN).")
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
        timeout=60,
    )
    if not resp.ok:
        die(f"OAuth token request failed (HTTP {resp.status_code}): {resp.text[:500]}")
    return resp.json()["access_token"]


def process_request(token: str, geom: dict, frm: str, to: str) -> bytes:
    width = int(os.environ.get("WIDTH", "2048"))
    height = int(os.environ.get("HEIGHT", "2048"))
    max_cloud = float(os.environ.get("MAX_CLOUD", "30"))
    mosaicking = os.environ.get("MOSAICKING_ORDER", "mostRecent")
    body = {
        "input": {
            "bounds": {
                "geometry": geom,
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": frm, "to": to},
                    "maxCloudCoverage": max_cloud,
                    "mosaickingOrder": mosaicking,
                },
            }],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [{"identifier": b, "format": {"type": "image/tiff"}} for b in SENTINEL_BANDS],
        },
        "evalscript": EVALSCRIPT,
    }
    resp = requests.post(
        PROCESS_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/tar"},
        json=body,
        timeout=600,
    )
    if not resp.ok:
        die(f"Process API request failed (HTTP {resp.status_code}): {resp.text[:800]}")
    return resp.content


def cmd_sentinel(args) -> None:
    today = datetime.now(timezone.utc).date()
    date_to = args.date_to or today.isoformat()
    date_from = args.date_from or (datetime.fromisoformat(date_to).date() - timedelta(days=7)).isoformat()
    frm = f"{date_from}T00:00:00Z"
    to = f"{date_to}T23:59:59Z"

    conn = connect()
    try:
        geom, bbox = fetch_aoi(conn)
        log(f"Sentinel {REGION} {date_from}..{date_to} (clip to polygon, bbox={bbox})")
        token = oauth_token()
        archive = process_request(token, geom, frm, to)

        members: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    members[Path(m.name).name] = tf.extractfile(m).read()

        for band in SENTINEL_BANDS:
            tif = members.get(f"{band}.tif") or members.get(band)
            if not tif:
                die(f"Band {band} missing from Process API response (members: {sorted(members)}).")
            table = f"s2_{band.lower()}"
            require_table(conn, table)
            fname = f"galicia_{band.lower()}_{date_from.replace('-', '')}_{date_to.replace('-', '')}.tif"
            n = load_raster(conn, table, tif, filename=fname, region=REGION,
                            dates={"date_from": date_from, "date_to": date_to})
            log(f"  {band} -> {table}: {n} tiles")
        conn.commit()
    finally:
        conn.close()
    log("Sentinel load complete")


# --- Meteo ------------------------------------------------------------------
def _gdal_subdatasets(nc_path: str) -> list[str]:
    out = subprocess.run(["gdalinfo", nc_path], capture_output=True, text=True, check=True).stdout
    subs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SUBDATASET_") and "_NAME=" in line:
            subs.append(line.split("=", 1)[1])
    return subs


def cmd_meteo(args) -> None:
    target = args.date or (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    day = datetime.fromisoformat(target).date()
    path_day = day.strftime("%Y%m%d")
    time_start = f"{(datetime.combine(day, datetime.min.time()) + timedelta(hours=1)):%Y-%m-%dT%H:%M:%S}Z"
    time_end = f"{(day + timedelta(days=4)):%Y-%m-%d}T00:00:00Z"

    north = os.environ.get("FWI_NORTH", "44.636")
    south = os.environ.get("FWI_SOUTH", "41.348")
    west = os.environ.get("FWI_WEST", "-10.293")
    east = os.environ.get("FWI_EAST", "-5.749")
    url = (
        f"{WRF_BASE}/{path_day}/wrf_arw_det_history_d02_{path_day}_0000.nc4"
        "?var=prec&var=mod&var=dir&var=u&var=v&var=temp&var=rh&var=lon&var=lat"
        f"&north={north}&west={west}&east={east}&south={south}&horizStride=1"
        f"&time_start={time_start}&time_end={time_end}&accept=netcdf3"
    )

    conn = connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, f"wrf_arw_{path_day}.nc")
            log(f"Meteo {REGION} {target}: downloading WRF")
            with requests.get(url, stream=True, timeout=600) as r:
                if not r.ok:
                    die(f"WRF download failed (HTTP {r.status_code}): {r.text[:500]}")
                with open(nc_path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)

            subs = _gdal_subdatasets(nc_path)
            if not subs:
                die(f"No NetCDF subdatasets found in {nc_path}")

            for sd in subs:
                var = sd.rsplit(":", 1)[-1].lower()
                if var in {"lon", "lat", "longitude", "latitude", "x", "y"}:
                    continue
                table = f"fwi_{var}"
                require_table(conn, table)
                tif_path = os.path.join(tmp, f"{var}.tif")
                subprocess.run(["gdal_translate", "-q", sd, tif_path], check=True)
                with open(tif_path, "rb") as fh:
                    tif = fh.read()
                fname = f"galicia_{var}_{path_day}.tif"
                n = load_raster(conn, table, tif, filename=fname, region=REGION, dates={"fdate": target})
                log(f"  {var} -> {table}: {n} tiles")
            conn.commit()
    finally:
        conn.close()
    log("Meteo load complete")


# --- CLI --------------------------------------------------------------------
def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description="Galicia recurring data pipeline.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("export-aoi", help="Write the Galicia polygon to INPUT/AOI/galicia.geojson")

    p_s = sub.add_parser("sentinel", help="Weekly Sentinel-2 refresh")
    p_s.add_argument("--date-from", dest="date_from", help="YYYY-MM-DD (default: date-to minus 7 days)")
    p_s.add_argument("--date-to", dest="date_to", help="YYYY-MM-DD (default: today UTC)")

    p_m = sub.add_parser("meteo", help="Daily MeteoGalicia WRF refresh")
    p_m.add_argument("--date", help="YYYY-MM-DD (default: yesterday UTC)")

    args = parser.parse_args()
    {"export-aoi": cmd_export_aoi, "sentinel": cmd_sentinel, "meteo": cmd_meteo}[args.command](args)


if __name__ == "__main__":
    main()
