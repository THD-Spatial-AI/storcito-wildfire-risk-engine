"""Reconstruct engine input files from the PostGIS database.

The risk engines in ``app/engines/`` read a fixed ``INPUT/`` tree of GeoTIFFs
and shapefiles. This module materialises that tree, per request, from the
PostGIS tables that were loaded with raster2pgsql / ogr2ogr, optionally clipping
every dataset to a request boundary.

Postgres is reached through GDAL/OGR's built-in PG drivers (which use libpq
directly) because the conda environment ships no Python Postgres driver. The
``gdalwarp`` / ``gdal_translate`` / ``ogr2ogr`` CLIs are used for robustness and
parity with how the data was loaded.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry


def _pg_connect():
    """psycopg2 connection to the gis DB, built from the same PG* params used for
    the GDAL/OGR exports. Used to fetch the blob-stored FWI / HIST scene files."""
    import psycopg2

    p = _pg_params()
    return psycopg2.connect(
        host=p["host"], port=p["port"], dbname=p["dbname"],
        user=p["user"], password=p["password"],
    )


# ---------------------------------------------------------------------------
# Connection strings (built from the PG* environment variables)
# ---------------------------------------------------------------------------
def _pg_params() -> dict[str, str]:
    return {
        "host": os.environ.get("PGHOST", "postgis"),
        "port": os.environ.get("PGPORT", "5432"),
        "dbname": os.environ.get("PGDATABASE", "gis"),
        "user": os.environ.get("PGUSER", "gis"),
        "password": os.environ.get("PGPASSWORD", "gis"),
    }


def _ogr_dsn() -> str:
    """OGR/vector PG connection string."""
    p = _pg_params()
    return "PG:" + " ".join(f"{k}={v}" for k, v in p.items())


def _gdal_raster_dsn(table: str, *, schema: str = "public") -> str:
    """GDAL PostGISRaster connection string (mode=2 = one coverage per table)."""
    p = _pg_params()
    parts = [f"{k}={v}" for k, v in p.items()]
    parts += [f"schema='{schema}'", f"table='{table}'", "mode='2'"]
    return "PG:" + " ".join(parts)


# ---------------------------------------------------------------------------
# Cutline helper
# ---------------------------------------------------------------------------
def _write_cutline(geometry: BaseGeometry, crs: str, dest_dir: Path) -> Path:
    """Write a clip geometry to a GeoJSON file usable as a gdal/ogr cutline.

    No explicit ``crs`` member is written: GeoJSON implies WGS84 (lon/lat), which
    OGR reads as the layer SRS, so gdalwarp/ogr2ogr reproject the cutline to each
    dataset's CRS automatically. ``clip_geom`` is therefore expected in WGS84.
    """
    if crs not in ("EPSG:4326", "EPSG:CRS84", "OGC:CRS84"):
        raise ValueError(
            f"Cutline geometry must be WGS84 (got {crs}); reproject before clipping."
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".geojson", dir=dest_dir)
    os.close(fd)
    path = Path(name)
    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": mapping(geometry)}
        ],
    }
    path.write_text(json.dumps(feature_collection))
    return path


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd[:3])} ...\n"
            f"stderr: {result.stderr.strip()[:2000]}"
        )


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------
def export_raster_table(
    table: str,
    dest_tif: str | Path,
    *,
    clip_geom: BaseGeometry | None = None,
    clip_geom_crs: str = "EPSG:4326",
    target_srs: str | None = None,
) -> Path:
    """Export a PostGIS raster table to a GeoTIFF, optionally clipped/reprojected.

    When ``clip_geom`` is given it is used as a gdalwarp cutline (GDAL reprojects
    it to the raster CRS), so the geometry may be supplied in any CRS (default
    WGS84). When ``target_srs`` is given the output is reprojected to that CRS;
    otherwise it keeps the source raster's CRS.
    """
    dest_tif = Path(dest_tif)
    dest_tif.parent.mkdir(parents=True, exist_ok=True)
    src = _gdal_raster_dsn(table)

    if clip_geom is None:
        cmd = ["gdalwarp", "-of", "GTiff", "-co", "TILED=YES", "-overwrite"]
        if target_srs is not None:
            cmd += ["-t_srs", target_srs]
        cmd += [src, str(dest_tif)]
        _run(cmd)
        return dest_tif

    cutline = _write_cutline(clip_geom, clip_geom_crs, dest_tif.parent)
    try:
        cmd = ["gdalwarp", "-of", "GTiff",
               "-cutline", str(cutline), "-crop_to_cutline", "-overwrite"]
        if target_srs is not None:
            cmd += ["-t_srs", target_srs]
        cmd += [src, str(dest_tif)]
        _run(cmd)
    finally:
        cutline.unlink(missing_ok=True)
    return dest_tif


def _ts_date_for(ts_table: str, target_date) -> str | None:
    try:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"public.{ts_table}",))
            if cur.fetchone()[0] is None:
                return None
            cur.execute(
                f"SELECT max(capture_date) FROM {ts_table} WHERE capture_date <= %s",
                (target_date,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0].isoformat()
            cur.execute(
                f"SELECT min(capture_date) FROM {ts_table} WHERE capture_date > %s",
                (target_date,),
            )
            row = cur.fetchone()
            if row and row[0]:
                if (row[0] - target_date).days > 16:
                    raise LookupError(
                        f"{ts_table} has no data on/before {target_date} and the "
                        f"nearest later capture ({row[0]}) is too far ahead; seed "
                        f"the missing period first."
                    )
                return row[0].isoformat()
            return None
    except LookupError:
        raise
    except Exception:
        return None


def export_ts_raster(
    ts_table: str,
    current_table: str,
    target_date,
    dest_tif: str | Path,
    *,
    clip_geom: BaseGeometry | None = None,
    clip_geom_crs: str = "EPSG:4326",
    target_srs: str | None = None,
) -> tuple[Path, str]:
    """Export the raster matching the assessment date from a *_ts time series.

    Returns (path, capture_date_used). Falls back to the current single-mosaic
    table when the time series is missing/empty.
    """
    dest_tif = Path(dest_tif)
    capture_date = _ts_date_for(ts_table, target_date)
    if capture_date is None:
        export_raster_table(current_table, dest_tif, clip_geom=clip_geom,
                            clip_geom_crs=clip_geom_crs, target_srs=target_srs)
        return dest_tif, "current"

    dest_tif.parent.mkdir(parents=True, exist_ok=True)
    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT ST_AsGDALRaster(ST_Union(rast), 'GTiff') FROM {ts_table} "
            "WHERE capture_date = %s",
            (capture_date,),
        )
        raw = cur.fetchone()[0]
    fd, tmp_name = tempfile.mkstemp(suffix=".tif", dir=dest_tif.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(bytes(raw))
        cmd = ["gdalwarp", "-of", "GTiff", "-overwrite"]
        if clip_geom is not None:
            cutline = _write_cutline(clip_geom, clip_geom_crs, dest_tif.parent)
            cmd += ["-cutline", str(cutline), "-crop_to_cutline"]
        if target_srs is not None:
            cmd += ["-t_srs", target_srs]
        _run(cmd + [str(tmp), str(dest_tif)])
    finally:
        tmp.unlink(missing_ok=True)
        if clip_geom is not None:
            cutline.unlink(missing_ok=True)
    return dest_tif, capture_date


def export_vector_table(
    table: str,
    dest_shp: str | Path,
    *,
    clip_geom: BaseGeometry | None = None,
    clip_geom_crs: str = "EPSG:4326",
    t_srs: str | None = None,
    select_sql: str | None = None,
) -> Path:
    """Export a PostGIS vector table to an ESRI Shapefile, optionally clipped.

    ``select_sql`` is an optional OGR SQL statement (must include the ``geom``
    column) used instead of the whole table -- e.g. to re-alias columns to the
    casing the engine expects, since PostgreSQL lowercases identifiers on import.
    """
    dest_shp = Path(dest_shp)
    dest_shp.parent.mkdir(parents=True, exist_ok=True)
    src = _ogr_dsn()

    cmd = ["ogr2ogr", "-f", "ESRI Shapefile", "-overwrite"]
    if t_srs is not None:
        cmd += ["-t_srs", t_srs]

    cutline: Path | None = None
    if clip_geom is not None:
        cutline = _write_cutline(clip_geom, clip_geom_crs, dest_shp.parent)
        # -clipsrc with a datasource clips to its geometries (both in clip_geom_crs).
        cmd += ["-clipsrc", str(cutline)]

    if select_sql is not None:
        cmd += ["-sql", select_sql, str(dest_shp), src]
    else:
        cmd += [str(dest_shp), src, table]
    try:
        _run(cmd)
    finally:
        if cutline is not None:
            cutline.unlink(missing_ok=True)
    return dest_shp


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATA_ROOT = Path(os.environ.get("STORCITO_DATA_DIR", _REPO_ROOT / "data"))
_FWI_CACHE_DIR = Path(
    os.environ.get("FFRM_FWI_CACHE", _DATA_ROOT / "OUTPUT" / "_fwi_cache")
)


def reconstruct_fwi(target_date, dest_fwi_dir: str | Path) -> list[Path]:
    """Provide the date-selected FWI NetCDF files (all dates <= target) into
    ``dest_fwi_dir`` from the `fwi_files` blob table.

    FWI is not round-tripped as PostGIS rasters: the engine reads multi-variable
    WRF NetCDF via netCDF4 and accumulates indices sequentially, so the original
    bytes are stored verbatim. Files are cached on disk (written once) and then
    hardlinked into each job dir -- instant and with no extra disk per request.
    """
    dest_fwi_dir = Path(dest_fwi_dir)
    dest_fwi_dir.mkdir(parents=True, exist_ok=True)
    _FWI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    with _pg_connect() as conn, conn.cursor() as cur:
        # Window: FWI_RUNUP_DAYS before the earliest plausible range start.
        from FR.FWI import FWI_RUNUP_DAYS

        window_start = target_date - timedelta(days=FWI_RUNUP_DAYS + 40)
        cur.execute(
            "SELECT filename FROM fwi_files "
            "WHERE fdate IS NOT NULL AND fdate <= %s AND fdate >= %s ORDER BY fdate",
            (target_date, window_start),
        )
        names = [r[0] for r in cur.fetchall()]
        if not names:
            cur.execute("SELECT min(fdate), max(fdate) FROM fwi_files")
            lo, hi = cur.fetchone()
            raise RuntimeError(
                f"No FWI files in the database for date <= {target_date} "
                f"(available range: {lo} .. {hi}). Seed with scripts/seed_blobs.py."
            )
        # Fetch (heavy) blobs only for files not already in the cache.
        missing = [n for n in names if not (_FWI_CACHE_DIR / n).exists()]
        if missing:
            cur.execute("SELECT filename, data FROM fwi_files WHERE filename = ANY(%s)", (missing,))
            for filename, data in cur.fetchall():
                tmp = _FWI_CACHE_DIR / f"{filename}.part"
                tmp.write_bytes(bytes(data))
                tmp.replace(_FWI_CACHE_DIR / filename)  # atomic publish

    copied: list[Path] = []
    for n in names:
        link = dest_fwi_dir / n
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            os.link(_FWI_CACHE_DIR / n, link)  # hardlink: instant, no extra disk
        except OSError:
            link.write_bytes((_FWI_CACHE_DIR / n).read_bytes())  # cross-device fallback
        copied.append(link)
    return copied


def available_fwi_dates_db() -> list[date]:
    """All FWI dates available in the `fwi_files` blob table (sorted ascending).

    DB-backed replacement for FR.FWI.available_fwi_dates, which reads INPUT/FWI.
    """
    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT fdate FROM fwi_files WHERE fdate IS NOT NULL ORDER BY fdate"
        )
        return [r[0] for r in cur.fetchall()]


def highest_temperature_fwi_dates_db() -> list[date]:
    """Warmest FWI day per calendar year, from `fwi_files.peak_temp` (sorted asc).

    DB-backed replacement for FR.FWI.highest_temperature_fwi_dates. Relies on the
    peak_temp recorded by scripts/seed_blobs.py at seed time.
    """
    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (EXTRACT(YEAR FROM fdate)) fdate "
            "FROM fwi_files "
            "WHERE fdate IS NOT NULL AND peak_temp IS NOT NULL "
            "ORDER BY EXTRACT(YEAR FROM fdate), peak_temp DESC"
        )
        return sorted(r[0] for r in cur.fetchall())


def reconstruct_hist(
    dest_hist_dir: str | Path,
    *,
    clip_geom: BaseGeometry | None = None,
    clip_geom_crs: str = "EPSG:4326",
    target_date=None,
) -> dict[str, object]:
    """Rebuild the HIST/ folder that FR.FHIST.fire_history reads, entirely from DB.

    Two parts:
      * Historico_incendios/hist_<year>.shp -- exported from the `hist` PostGIS
        table, split back into one shapefile per year.
      * PRE_FIRE/ and POST_FIRE/ Sentinel-2 scenes -- written back byte-exact from
        the `hist_scenes` blob table (their filenames encode date+band, which
        FR.FHIST parses, so they are stored as blobs rather than as rasters).
    """
    dest_hist_dir = Path(dest_hist_dir)
    years_dir = dest_hist_dir / "Historico_incendios"
    years_dir.mkdir(parents=True, exist_ok=True)

    produced: list[str] = []
    copied_scenes: list[str] = []
    with _pg_connect() as conn, conn.cursor() as cur:
        max_year = target_date.year if target_date is not None else None
        if max_year is not None:
            # No future leakage: a 2025 run must not see 2026 fires.
            cur.execute(
                "SELECT DISTINCT year FROM hist WHERE year IS NOT NULL AND year <= %s ORDER BY year",
                (max_year,),
            )
        else:
            cur.execute("SELECT DISTINCT year FROM hist WHERE year IS NOT NULL ORDER BY year")
        years = [r[0] for r in cur.fetchall()]

        cur.execute("SELECT DISTINCT left(filename, 4) FROM hist_scenes")
        scene_years = sorted({int(r[0]) for r in cur.fetchall() if r[0].isdigit()})
        if max_year is not None:
            scene_years = [y for y in scene_years if y <= max_year]
        missing = [y for y in scene_years if y not in years]
        if missing:
            raise RuntimeError(
                f"hist_scenes has scenes for {missing} but the hist table has no "
                f"hotspots for those years (available: {years}). Seed them with "
                + " and ".join(f"`make hist START={y}-05-01 END={y}-10-31`" for y in missing)
            )

        cur.execute("SELECT phase, filename, data FROM hist_scenes ORDER BY phase, filename")
        rows = [
            (phase, filename, data)
            for phase, filename, data in cur.fetchall()
            if target_date is None or filename[:10] <= str(target_date)
        ]
        # Keep only complete PRE/POST pairs per year: dropping a future POST
        # scene must also drop its PRE partner or FHIST crashes on the pair.
        years_with = {}
        for phase, filename, _ in rows:
            years_with.setdefault(filename[:4], set()).add(phase)
        complete = {y for y, phases in years_with.items()
                    if {"PRE_FIRE", "POST_FIRE"} <= phases}
        for phase, filename, data in rows:
            if filename[:4] not in complete:
                continue
            phase_dir = dest_hist_dir / phase
            phase_dir.mkdir(parents=True, exist_ok=True)
            out = phase_dir / filename
            out.write_bytes(bytes(data))
            copied_scenes.append(str(out))

    for year in years:
        dest = years_dir / f"hist_{year}.shp"
        export_vector_table(
            "hist", dest, t_srs=ENGINE_VECTOR_SRS,
            select_sql=(
                f"SELECT * FROM hist WHERE year = {year}"
                + (f" AND acq_date <= '{target_date}'" if target_date is not None else "")
            ),
        )
        produced.append(str(dest))

    return {"years": years, "perimeters": produced, "scenes": copied_scenes}


# ---------------------------------------------------------------------------
# Per-engine reconstruction plan
# ---------------------------------------------------------------------------
# Each entry: (kind, table, relative destination path under INPUT/)
_RASTER = "raster"
_VECTOR = "vector"

# The whole-region engines work in a projected (metric) CRS -- FR.infra computes
# pixel counts as extent/25 m and FR.cropped reprojects to EPSG:32629. The stored
# rasters are geographic (dtm/s2_* = 4326) or a different projection (fuels =
# 25830), so reconstructed rasters are reprojected to this CRS for the engine.
ENGINE_RASTER_SRS = "EPSG:32629"

ENGINE_VECTOR_SRS = "EPSG:32629"

# PostgreSQL lowercases identifiers on import, but the engine modules expect the
# original shapefile column casing. Re-alias on export (the SELECT must include
# the geometry column so OGR carries it through).
_VECTOR_SELECT_SQL: dict[str, str] = {
    "iuf": 'SELECT geom, code_18 AS "Code_18" FROM iuf',
}


_COMMON_PLAN: list[tuple[str, str, str]] = [
    (_RASTER, "dtm", "DTM/DTM.tif"),
    (_RASTER, "mdt", "MDT/DEM_NationalScenario_2013.tif"),
    (_RASTER, "twi", "TWI/TWI.tif"),
    (_RASTER, "lst", "LST/LST.tiff"),
    (_RASTER, "sentinel_b4", "Sentinel/B4.tiff"),
    (_RASTER, "sentinel_b8", "Sentinel/B8.tiff"),
    (_RASTER, "sentinel_b11", "Sentinel/B11.tiff"),
    (_RASTER, "fuels", "FUELS/FUELS.tif"),
    (_RASTER, "fuels", "FUELS/FMT_NationalScenario_2019.tif"),
    (_VECTOR, "infra", "INFRA/galicia_entera.shp"),
    (_VECTOR, "infra", "INFRA/galicia_solo_vehiculos.shp"),
    (_VECTOR, "iuf", "IUF/CLC_galicia.shp"),
]

_ENGINE_PLANS: dict[str, list[tuple[str, str, str]]] = {
    "static": _COMMON_PLAN,
    "dynamic": _COMMON_PLAN,
}


def reconstruct_inputs(
    dest_input_dir: str | Path,
    *,
    engine: str,
    target_date,
    clip_geom: BaseGeometry | None = None,
    clip_geom_crs: str = "EPSG:4326",
) -> dict[str, object]:
    """Materialise the engine-expected INPUT/ tree from PostGIS (+ FWI copy).

    Returns a dict with the produced file paths keyed by their INPUT-relative path,
    plus the list of FWI files copied.
    """
    if engine not in _ENGINE_PLANS:
        raise ValueError(f"Unknown engine '{engine}'. Expected one of {sorted(_ENGINE_PLANS)}.")

    dest_input_dir = Path(dest_input_dir)
    produced: dict[str, str] = {}


    print("[reconstruct] copying FWI weather files (run-up window) from DB blobs", flush=True)
    fwi_files = reconstruct_fwi(target_date, dest_input_dir / "FWI")
    print(f"[reconstruct] FWI: {len(fwi_files)} day file(s) materialised", flush=True)

    # Layers with a *_ts time series export the raster matching the run's
    # assessment date, so historical runs never mix in newer imagery.
    _TS_TABLES = {
        "lst": "lst_ts",
        "sentinel_b4": "sentinel_b4_ts",
        "sentinel_b8": "sentinel_b8_ts",
        "sentinel_b11": "sentinel_b11_ts",
    }
    ts_dates_used: dict[str, str] = {}
    _plan = _ENGINE_PLANS[engine]
    for _n, (kind, table, rel) in enumerate(_plan, start=1):
        print(f"[reconstruct] {_n:>2}/{len(_plan)} exporting {table} -> {rel}", flush=True)
        dest = dest_input_dir / rel
        if table in _TS_TABLES:
            _, ts_dates_used[table] = export_ts_raster(
                _TS_TABLES[table], table, target_date, dest, clip_geom=clip_geom,
                clip_geom_crs=clip_geom_crs, target_srs=ENGINE_RASTER_SRS)
        elif kind == _RASTER:
            export_raster_table(table, dest, clip_geom=clip_geom,
                                clip_geom_crs=clip_geom_crs, target_srs=ENGINE_RASTER_SRS)
        else:
            export_vector_table(table, dest, clip_geom=clip_geom, clip_geom_crs=clip_geom_crs,
                                t_srs=ENGINE_VECTOR_SRS, select_sql=_VECTOR_SELECT_SQL.get(table))
        produced[rel] = str(dest)

    # Historical fire (both engines): yearly perimeters from the `hist` table
    # plus the on-disk PRE_FIRE / POST_FIRE Sentinel scenes.
    print("[reconstruct] exporting fire history (hotspot shapefiles + dNBR scenes)", flush=True)
    hist_info = reconstruct_hist(dest_input_dir / "HIST",
                                 clip_geom=clip_geom, clip_geom_crs=clip_geom_crs,
                                 target_date=target_date)

    return {
        "input_dir": str(dest_input_dir),
        "produced": produced,
        "fwi_files": [str(p) for p in fwi_files],
        "hist": hist_info,
        "layer_dates": ts_dates_used,
        "skipped_layers": [],
    }
