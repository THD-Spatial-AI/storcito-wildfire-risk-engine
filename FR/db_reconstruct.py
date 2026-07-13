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
from typing import Iterable

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
        "password": os.environ.get("PGPASSWORD", ""),
    }


def _conninfo_value(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ogr_dsn() -> str:
    """OGR/vector PG connection string."""
    p = _pg_params()
    return "PG:" + " ".join(f"{k}={_conninfo_value(v)}" for k, v in p.items())


def _gdal_raster_dsn(table: str, *, schema: str = "public", where: str | None = None) -> str:
    """GDAL PostGISRaster connection string (mode=2 = one coverage per table)."""
    p = _pg_params()
    parts = [f"{k}={_conninfo_value(v)}" for k, v in p.items()]
    parts += [f"schema='{schema}'", f"table='{table}'", "mode='2'"]
    if where:
        parts.append(f"where='{where}'")
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
    resampling: str = "near",
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
        cmd = ["gdalwarp", "-of", "GTiff", "-co", "TILED=YES", "-r", resampling, "-overwrite"]
        if target_srs is not None:
            cmd += ["-t_srs", target_srs]
        cmd += [src, str(dest_tif)]
        _run(cmd)
        return dest_tif

    cutline = _write_cutline(clip_geom, clip_geom_crs, dest_tif.parent)
    try:
        cmd = ["gdalwarp", "-of", "GTiff", "-r", resampling,
               "-cutline", str(cutline), "-crop_to_cutline", "-overwrite"]
        if target_srs is not None:
            cmd += ["-t_srs", target_srs]
        cmd += [src, str(dest_tif)]
        _run(cmd)
    finally:
        cutline.unlink(missing_ok=True)
    return dest_tif


def _ts_date_for(ts_table: str, target_date, *, max_age_days: int | None = None) -> str | None:
    """Newest capture on or before the assessment date; never look forward."""
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{ts_table}",))
        if cur.fetchone()[0] is None:
            return None
        cur.execute(
            f"SELECT max(capture_date) FROM {ts_table} WHERE capture_date <= %s",
            (target_date,),
        )
        row = cur.fetchone()
        capture = row[0] if row and row[0] else None
    if capture is not None and max_age_days is not None:
        age = (target_date - capture).days
        if age > max_age_days:
            raise LookupError(
                f"{ts_table} newest capture {capture} is {age} days old; "
                f"maximum allowed is {max_age_days}"
            )
    return capture.isoformat() if capture else None


def _common_ts_dates(
    ts_tables: tuple[str, ...], target_date, *, max_age_days: int
) -> list[str]:
    """Common capture dates, newest first, within the freshness window."""
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    if not ts_tables or any(not table.replace("_", "").isalnum() for table in ts_tables):
        raise ValueError("invalid time-series table list")
    with _pg_connect() as conn, conn.cursor() as cur:
        for table in ts_tables:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            if cur.fetchone()[0] is None:
                raise LookupError(f"required time-series table is missing: {table}")
        intersection = " INTERSECT ".join(
            f"SELECT capture_date FROM {table} WHERE capture_date <= %s" for table in ts_tables
        )
        cur.execute(
            f"SELECT capture_date FROM ({intersection}) common_dates "
            "ORDER BY capture_date DESC",
            (target_date,) * len(ts_tables),
        )
        captures = [row[0] for row in cur.fetchall()]
    if not captures:
        raise LookupError(
            f"no common capture on or before {target_date} across {', '.join(ts_tables)}"
        )
    newest_age = (target_date - captures[0]).days
    fresh = [capture for capture in captures if (target_date - capture).days <= max_age_days]
    if not fresh:
        raise LookupError(
            f"common Sentinel capture {captures[0]} is {newest_age} days old; "
            f"maximum allowed is {max_age_days}"
        )
    return [capture.isoformat() for capture in fresh]


def _common_ts_date(
    ts_tables: tuple[str, ...], target_date, *, max_age_days: int
) -> str:
    """Newest date present in every supplied time-series table."""
    return _common_ts_dates(
        ts_tables, target_date, max_age_days=max_age_days
    )[0]



def _composite_newest_valid(
    primary_bytes: bytes,
    older: list[bytes],
    *,
    return_used: bool = False,
) -> bytes | tuple[bytes, list[int]]:
    """Fill invalid pixels of the primary raster from older ones (newest first).

    All inputs share the ts table's native grid (same fetch pipeline), so a
    straight array overlay is exact. Falls back to the primary bytes on any
    library/shape mismatch.
    """
    try:
        import io as _io

        import numpy as np
        import rasterio

        with rasterio.open(_io.BytesIO(primary_bytes)) as src:
            base = src.read(1).astype("float64")
            profile = src.profile
        invalid = ~np.isfinite(base) | (base <= 0)
        used_fillers: list[int] = []
        for index, blob in enumerate(older):
            if not invalid.any():
                break
            with rasterio.open(_io.BytesIO(blob)) as src:
                cand = src.read(1).astype("float64")
            if cand.shape != base.shape:
                continue
            usable = invalid & np.isfinite(cand) & (cand > 0)
            if np.any(usable):
                used_fillers.append(index)
            base[usable] = cand[usable]
            invalid &= ~usable
        buf = _io.BytesIO()
        profile.update(driver="GTiff")
        with rasterio.open(buf, "w", **profile) as dst:
            dst.write(base.astype(profile.get("dtype", "float32")), 1)
        result = buf.getvalue()
        return (result, used_fillers) if return_used else result
    except Exception:
        return (primary_bytes, []) if return_used else primary_bytes




def _composite_files_newest_valid(primary: Path, fillers: list) -> list:
    """Fill invalid pixels of the primary GeoTIFF from older captures.

    Each filler is exported to a temp file and applied in order (newest
    first). Returns the capture dates actually used.
    """
    used: list = []
    try:
        import numpy as np
        import rasterio

        with rasterio.open(primary) as src:
            base = src.read(1).astype("float64")
            profile = src.profile
        invalid = ~np.isfinite(base) | (base <= 0)
        for capture, ts_table in fillers:
            if not invalid.any():
                break
            fd, name = tempfile.mkstemp(suffix=".tif", dir=primary.parent)
            os.close(fd)
            fpath = Path(name)
            try:
                _export_capture_to_file(ts_table, capture, fpath)
                with rasterio.open(fpath) as src:
                    cand = src.read(1).astype("float64")
                if cand.shape != base.shape:
                    continue
                usable = invalid & np.isfinite(cand) & (cand > 0)
                if usable.any():
                    base[usable] = cand[usable]
                    invalid &= ~usable
                    used.append(capture)
            finally:
                fpath.unlink(missing_ok=True)
        with rasterio.open(primary, "w", **profile) as dst:
            dst.write(base.astype(profile.get("dtype", "float32")), 1)
    except Exception:
        return used
    return used


def _export_capture_to_file(ts_table: str, capture_date, dest: Path) -> Path:
    """Stream one capture_date of a *_ts table to a GeoTIFF via gdalwarp.

    The PostGISRaster driver reads tiles windowed, so raster size is not
    limited by PostgreSQL's 1 GB single-value allocation cap (which
    ST_AsGDALRaster on a unioned large raster breaches).
    """
    src = _gdal_raster_dsn(ts_table, where=f"capture_date = \\'{capture_date}\\'")
    _run(["gdalwarp", "-of", "GTiff", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
          "-overwrite", src, str(dest)])
    return dest


def export_ts_raster(
    ts_table: str,
    current_table: str,
    target_date,
    dest_tif: str | Path,
    *,
    clip_geom: BaseGeometry | None = None,
    clip_geom_crs: str = "EPSG:4326",
    target_srs: str | None = None,
    resampling: str = "bilinear",
    capture_date: str | None = None,
    max_age_days: int | None = None,
) -> tuple[Path, str]:
    """Export the raster matching the assessment date from a *_ts time series.

    Returns (path, capture_date_used). Future or excessively stale captures are
    never substituted.
    """
    dest_tif = Path(dest_tif)
    capture_date = capture_date or _ts_date_for(
        ts_table, target_date, max_age_days=max_age_days
    )
    if capture_date is None:
        raise LookupError(
            f"{ts_table} has no capture on or before {target_date}; "
            "seed the requested historical period first"
        )

    dest_tif.parent.mkdir(parents=True, exist_ok=True)
    source_dates = [str(capture_date)]
    fd, tmp_name = tempfile.mkstemp(suffix=".tif", dir=dest_tif.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    filler_dates: list = []
    if max_age_days is not None and max_age_days > 0:
        if isinstance(target_date, str):
            _t = date.fromisoformat(target_date)
        else:
            _t = target_date
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT capture_date FROM {ts_table} "
                "WHERE capture_date >= %s AND capture_date < %s "
                "ORDER BY capture_date DESC",
                (_t - timedelta(days=max_age_days), capture_date),
            )
            filler_dates = [row[0] for row in cur.fetchall()]
    try:
        _export_capture_to_file(ts_table, capture_date, tmp)
        if filler_dates:
            # Per-pixel composite within the freshness window: fill invalid
            # pixels from older captures the gate already permits, newest
            # valid value wins. File-based, so raster size is unbounded.
            used = _composite_files_newest_valid(
                tmp, [(d, ts_table) for d in filler_dates]
            )
            source_dates.extend(str(d) for d in used)
        cmd = ["gdalwarp", "-of", "GTiff", "-r", resampling, "-overwrite"]
        if clip_geom is not None:
            cutline = _write_cutline(clip_geom, clip_geom_crs, dest_tif.parent)
            cmd += ["-cutline", str(cutline), "-crop_to_cutline"]
        if target_srs is not None:
            cmd += ["-t_srs", target_srs]
        _run(cmd + [str(tmp), str(dest_tif)])
        try:
            import rasterio

            with rasterio.open(dest_tif, "r+") as dst:
                dst.update_tags(
                    STORCITO_PRIMARY_SOURCE_DATE=str(capture_date),
                    STORCITO_SOURCE_DATES=",".join(source_dates),
                )
        except Exception:
            pass
    finally:
        tmp.unlink(missing_ok=True)
        if clip_geom is not None:
            cutline.unlink(missing_ok=True)
    return dest_tif, capture_date


def _raster_has_positive_data(path: str | Path) -> bool:
    """Return whether a raster contains at least one finite, non-nodata value > 0."""
    import numpy as np
    import rasterio

    with rasterio.open(path) as src:
        for _index, window in src.block_windows(1):
            values = src.read(1, window=window, masked=True).compressed()
            if values.size and np.any(np.isfinite(values) & (values > 0)):
                return True
    return False


def export_common_ts_rasters(
    layers: list[tuple[str, str, Path]],
    target_date,
    *,
    max_age_days: int,
    clip_geom: BaseGeometry | None = None,
    clip_geom_crs: str = "EPSG:4326",
    target_srs: str | None = None,
    resampling: str = "bilinear",
) -> tuple[dict[str, Path], str]:
    """Export synchronized bands from the newest usable common capture.

    A region-wide Sentinel mosaic can be structurally complete while a cloudy
    acquisition contains only nodata inside a small AOI. Trying common capture
    dates as a group keeps B4/B8/B11 synchronized and avoids producing blank
    NDVI/NDMI layers from such a window.
    """
    if not layers:
        raise ValueError("at least one time-series raster is required")
    candidates = _common_ts_dates(
        tuple(ts_table for _name, ts_table, _dest in layers),
        target_date,
        max_age_days=max_age_days,
    )
    rejected: list[str] = []
    outputs = {name: Path(dest) for name, _ts_table, dest in layers}

    for capture in candidates:
        for path in outputs.values():
            path.unlink(missing_ok=True)
        blank_layer = None
        for name, ts_table, dest in layers:
            export_ts_raster(
                ts_table,
                name,
                target_date,
                dest,
                clip_geom=clip_geom,
                clip_geom_crs=clip_geom_crs,
                target_srs=target_srs,
                resampling=resampling,
                capture_date=capture,
            )
            if not _raster_has_positive_data(dest):
                blank_layer = name
                break
        if blank_layer is None:
            if rejected:
                skipped = ", ".join(rejected)
                print(
                    f"[reconstruct] Sentinel capture {capture} selected; "
                    f"newer capture(s) had no valid AOI pixels: {skipped}",
                    flush=True,
                )
            else:
                print(
                    f"[reconstruct] Sentinel capture {capture} selected for all bands",
                    flush=True,
                )
            return outputs, capture
        rejected.append(f"{capture} ({blank_layer})")

    for path in outputs.values():
        path.unlink(missing_ok=True)
    raise LookupError(
        "no common Sentinel capture contains valid pixels in the requested AOI "
        f"within {max_age_days} day(s) of {target_date}; rejected: "
        + ", ".join(rejected)
    )


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


def reconstruct_fwi(
    target_date,
    dest_fwi_dir: str | Path,
    *,
    score_start=None,
) -> list[Path]:
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
    if isinstance(score_start, str):
        score_start = date.fromisoformat(score_start)
    score_start = score_start or target_date

    with _pg_connect() as conn, conn.cursor() as cur:
        # Exact model window: run-up begins before the first scoring date.
        from FR.FWI import FWI_RUNUP_DAYS

        window_start = score_start - timedelta(days=FWI_RUNUP_DAYS)
        cur.execute(
            "SELECT DISTINCT ON (fdate) fdate, filename, nbytes FROM fwi_files "
            "WHERE fdate BETWEEN %s AND %s ORDER BY fdate, id DESC",
            (window_start, target_date),
        )
        rows = cur.fetchall()
        if not rows:
            cur.execute("SELECT min(fdate), max(fdate) FROM fwi_files")
            lo, hi = cur.fetchone()
            raise RuntimeError(
                f"No FWI files in the database for date <= {target_date} "
                f"(available range: {lo} .. {hi}). Seed with scripts/seed_blobs.py."
            )
        by_date = {row[0]: (row[1], row[2]) for row in rows}
        expected_dates = {
            window_start + timedelta(days=offset)
            for offset in range((target_date - window_start).days + 1)
        }
        missing_dates = sorted(expected_dates - set(by_date))
        if missing_dates:
            preview = ", ".join(day.isoformat() for day in missing_dates[:10])
            suffix = "..." if len(missing_dates) > 10 else ""
            raise RuntimeError(
                f"FWI run-up window is incomplete ({len(missing_dates)} missing day(s): "
                f"{preview}{suffix})"
            )
        names = [by_date[day][0] for day in sorted(expected_dates)]
        # Fetch (heavy) blobs only for files not already in the cache.
        missing = []
        for day in sorted(expected_dates):
            filename, nbytes = by_date[day]
            cached = _FWI_CACHE_DIR / filename
            if not cached.is_file() or cached.stat().st_size != nbytes:
                cached.unlink(missing_ok=True)
                missing.append(filename)
        for i, filename in enumerate(missing, start=1):
            print(f"[reconstruct] FWI cache fill {i}/{len(missing)}: {filename}", flush=True)
            cur.execute(
                "SELECT data, nbytes FROM fwi_files WHERE filename = %s",
                (filename,),
            )
            row = cur.fetchone()
            if row is None or row[0] is None or len(row[0]) != row[1]:
                raise RuntimeError(f"FWI database blob is incomplete: {filename}")
            tmp = _FWI_CACHE_DIR / f"{filename}.part"
            tmp.write_bytes(bytes(row[0]))
            tmp.replace(_FWI_CACHE_DIR / filename)  # atomic publish
            del row

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
    """All FWI dates available in the blob table (sorted ascending).

    DB-backed replacement for FR.FWI.available_fwi_dates, which reads INPUT/FWI.
    """
    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT fdate FROM fwi_files "
            "WHERE fdate IS NOT NULL ORDER BY fdate"
        )
        return [r[0] for r in cur.fetchall()]


def available_dynamic_fwi_dates_db() -> list[date]:
    """Dates satisfying FWI run-up plus fresh LST and common Sentinel inputs."""
    sentinel_age = int(os.environ.get("STORCITO_MAX_SENTINEL_AGE_DAYS", "14"))
    lst_age = int(os.environ.get("STORCITO_MAX_LST_AGE_DAYS", "3"))
    if sentinel_age < 0 or lst_age < 0:
        raise ValueError("dynamic source age limits must be non-negative")
    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """WITH dates AS (
                   SELECT DISTINCT fdate FROM fwi_files WHERE fdate IS NOT NULL
               ), eligible AS (
                   SELECT d.fdate FROM dates d
                   WHERE EXTRACT(MONTH FROM d.fdate) BETWEEN 5 AND 10
                     AND (SELECT count(DISTINCT f.fdate) FROM fwi_files f
                          WHERE f.fdate BETWEEN d.fdate - 60 AND d.fdate) = 61
               )
               SELECT e.fdate FROM eligible e
               WHERE EXISTS (
                   SELECT 1 FROM lst_ts l
                   WHERE l.capture_date BETWEEN e.fdate - %s AND e.fdate
               )
                 AND EXISTS (
                   SELECT 1 FROM sentinel_b4_ts b4
                   WHERE b4.capture_date BETWEEN e.fdate - %s AND e.fdate
                     AND EXISTS (SELECT 1 FROM sentinel_b8_ts b8
                                 WHERE b8.capture_date = b4.capture_date)
                     AND EXISTS (SELECT 1 FROM sentinel_b11_ts b11
                                 WHERE b11.capture_date = b4.capture_date)
               )
               ORDER BY e.fdate""",
            (lst_age, sentinel_age),
        )
        return [row[0] for row in cur.fetchall()]


FIRE_SEASON_START_MONTH = 5
FIRE_SEASON_END_MONTH = 10


def select_hottest_fwi_date(
    observations: Iterable[tuple[date, float]], year: int
) -> date:
    """Select the hottest May-October observation for ``year``.

    The earliest date wins an exact temperature tie so selection is stable
    regardless of database or filesystem ordering.
    """
    candidates = [
        (day, float(peak_temp))
        for day, peak_temp in observations
        if day.year == year
        and FIRE_SEASON_START_MONTH <= day.month <= FIRE_SEASON_END_MONTH
    ]
    if not candidates:
        raise LookupError(
            f"No eligible FWI day is available from May 1 through October 31, {year}."
        )
    return min(candidates, key=lambda item: (-item[1], item[0]))[0]


def highest_temperature_fwi_date_for_year(year: int) -> date:
    """Return the hottest eligible May-October FWI date for one year."""
    try:
        season_start = date(int(year), FIRE_SEASON_START_MONTH, 1)
        season_end = date(int(year), FIRE_SEASON_END_MONTH, 31)
    except (TypeError, ValueError) as exc:
        raise ValueError("static assessment year must be a valid calendar year") from exc

    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """WITH eligible AS (
                   SELECT d.fdate, max(d.peak_temp) AS peak_temp
                   FROM fwi_files d
                   WHERE d.fdate BETWEEN %s AND %s
                     AND d.peak_temp IS NOT NULL
                     AND (SELECT count(DISTINCT f.fdate) FROM fwi_files f
                          WHERE f.fdate BETWEEN d.fdate - 60 AND d.fdate) = 61
                   GROUP BY d.fdate
               )
               SELECT fdate
               FROM eligible
               ORDER BY peak_temp DESC, fdate ASC
               LIMIT 1""",
            (season_start, season_end),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(
            f"No eligible FWI day is available from May 1 through October 31, {year}."
        )
    return row[0]


def highest_temperature_fwi_dates_db() -> list[date]:
    """Hottest eligible May-October FWI day per calendar year (sorted)."""
    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """WITH eligible AS (
                   SELECT d.fdate, max(d.peak_temp) AS peak_temp
                   FROM fwi_files d
                   WHERE d.fdate IS NOT NULL AND d.peak_temp IS NOT NULL
                     AND EXTRACT(MONTH FROM d.fdate) BETWEEN %s AND %s
                     AND (SELECT count(DISTINCT f.fdate) FROM fwi_files f
                          WHERE f.fdate BETWEEN d.fdate - 60 AND d.fdate) = 61
                   GROUP BY d.fdate
               )
               SELECT DISTINCT ON (EXTRACT(YEAR FROM fdate)) fdate
               FROM eligible
               ORDER BY EXTRACT(YEAR FROM fdate), peak_temp DESC, fdate ASC""",
            (FIRE_SEASON_START_MONTH, FIRE_SEASON_END_MONTH),
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
        cur.execute("SELECT phase, filename FROM hist_scenes ORDER BY phase, filename")
        rows = [
            (phase, filename)
            for phase, filename in cur.fetchall()
            if target_date is None or filename[:10] <= str(target_date)
        ]
        year_phase_bands: dict[str, dict[str, set[str]]] = {}
        for phase, filename in rows:
            band = "B8A" if "_B8A_" in filename else "B12" if "_B12_" in filename else ""
            year_phase_bands.setdefault(filename[:4], {}).setdefault(phase, set()).add(band)
        complete = {
            y for y, phases in year_phase_bands.items()
            if {"B8A", "B12"} <= phases.get("PRE_FIRE", set())
            and {"B8A", "B12"} <= phases.get("POST_FIRE", set())
        }
        missing = sorted(int(y) for y in complete if y.isdigit() and int(y) not in years)
        if missing:
            raise RuntimeError(
                f"hist_scenes has complete scenes for {missing} but hist has no "
                f"hotspots for those years (available: {years}). Seed them with "
                + " and ".join(f"`make hist START={y}-05-01 END={y}-10-31`" for y in missing)
            )
        for phase, filename in rows:
            if filename[:4] not in complete:
                continue
            phase_dir = dest_hist_dir / phase
            phase_dir.mkdir(parents=True, exist_ok=True)
            out = phase_dir / filename
            cur.execute(
                "SELECT data FROM hist_scenes WHERE phase = %s AND filename = %s",
                (phase, filename),
            )
            out.write_bytes(bytes(cur.fetchone()[0]))
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

    return {
        "years": years,
        "complete_scene_years": sorted(int(y) for y in complete if y.isdigit()),
        "perimeters": produced,
        "scenes": copied_scenes,
    }


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

TEMPORAL_TS_TABLES = {
    "lst": "lst_ts",
    "sentinel_b4": "sentinel_b4_ts",
    "sentinel_b8": "sentinel_b8_ts",
    "sentinel_b11": "sentinel_b11_ts",
}


def _raster_resolution_m(path: str | Path) -> float | None:
    """Approximate native grid spacing in metres for provenance metadata."""
    try:
        import rasterio
        from rasterio.warp import calculate_default_transform

        with rasterio.open(path) as src:
            if src.crs is None:
                return None
            if src.crs.is_projected:
                return float(max(abs(src.transform.a), abs(src.transform.e)))
            transform, _width, _height = calculate_default_transform(
                src.crs, ENGINE_RASTER_SRS, src.width, src.height, *src.bounds
            )
            return float(max(abs(transform.a), abs(transform.e)))
    except Exception:
        return None


def _raster_has_values_in_range(path: str | Path, lower: float, upper: float) -> bool:
    import numpy as np
    import rasterio

    with rasterio.open(path) as src:
        for _index, window in src.block_windows(1):
            values = src.read(1, window=window, masked=True).compressed()
            if values.size and np.any(np.isfinite(values) & (values > lower) & (values < upper)):
                return True
    return False


def _raster_source_dates(path: str | Path, fallback: str) -> list[str]:
    try:
        import rasterio

        with rasterio.open(path) as src:
            raw = src.tags().get("STORCITO_SOURCE_DATES", "")
        dates = [value.strip() for value in raw.split(",") if value.strip()]
        return dates or [str(fallback)]
    except Exception:
        return [str(fallback)]


def reconstruct_temporal_inputs(
    dest_input_dir: str | Path,
    *,
    target_date,
    include_lst: bool,
    include_satellite: bool,
    clip_geom: BaseGeometry | None = None,
    clip_geom_crs: str = "EPSG:4326",
) -> dict[str, object]:
    """Reconstruct only date-dependent layers as of one assessment day.

    LST is an optional enhancer: if no physically valid capture exists inside
    the freshness window, it is omitted and the combiner reports/renormalizes
    the missing weight. Sentinel vegetation layers remain required for the
    dynamic vegetation topic and therefore fail closed when unavailable.
    """
    dest_input_dir = Path(dest_input_dir)
    produced: dict[str, str] = {}
    layer_dates: dict[str, str] = {}
    layer_date_details: dict[str, dict[str, object]] = {}
    skipped: dict[str, str] = {}
    resolutions: dict[str, float] = {}

    if include_lst:
        lst_age = int(os.environ.get("STORCITO_MAX_LST_AGE_DAYS", "3"))
        if lst_age < 0:
            raise ValueError("STORCITO_MAX_LST_AGE_DAYS must be non-negative")
        lst_path = dest_input_dir / "LST" / "LST.tiff"
        try:
            print(f"[reconstruct] temporal {target_date}: exporting lst", flush=True)
            _, capture = export_ts_raster(
                TEMPORAL_TS_TABLES["lst"],
                "lst",
                target_date,
                lst_path,
                clip_geom=clip_geom,
                clip_geom_crs=clip_geom_crs,
                target_srs=ENGINE_RASTER_SRS,
                resampling="bilinear",
                max_age_days=lst_age,
            )
            if not _raster_has_values_in_range(lst_path, 220.0, 340.0):
                raise LookupError("available LST captures contain no valid AOI pixels")
            produced["LST/LST.tiff"] = str(lst_path)
            layer_dates["lst"] = capture
            layer_date_details["lst"] = {
                "primary": capture,
                "contributors": _raster_source_dates(lst_path, capture),
                "selection": "newest valid pixel on or before assessment date",
            }
            resolution = _raster_resolution_m(lst_path)
            if resolution is not None:
                resolutions["lst"] = resolution
        except LookupError as exc:
            lst_path.unlink(missing_ok=True)
            skipped["lst"] = str(exc)
            print(
                f"[reconstruct] temporal {target_date}: LST unavailable; "
                "FWI-only meteorology will be used",
                flush=True,
            )

    if include_satellite:
        sentinel_age = int(os.environ.get("STORCITO_MAX_SENTINEL_AGE_DAYS", "14"))
        if sentinel_age < 0:
            raise ValueError("STORCITO_MAX_SENTINEL_AGE_DAYS must be non-negative")
        layers = [
            (name, TEMPORAL_TS_TABLES[name], dest_input_dir / relative)
            for name, relative in (
                ("sentinel_b4", "Sentinel/B4.tiff"),
                ("sentinel_b8", "Sentinel/B8.tiff"),
                ("sentinel_b11", "Sentinel/B11.tiff"),
            )
        ]
        paths, capture = export_common_ts_rasters(
            layers,
            target_date,
            max_age_days=sentinel_age,
            clip_geom=clip_geom,
            clip_geom_crs=clip_geom_crs,
            target_srs=ENGINE_RASTER_SRS,
            resampling="bilinear",
        )
        for name, _table, path in layers:
            relative = str(path.relative_to(dest_input_dir))
            produced[relative] = str(paths[name])
            layer_dates[name] = capture
            layer_date_details[name] = {
                "primary": capture,
                "contributors": [capture],
                "selection": "synchronized common Sentinel capture",
            }
            resolution = _raster_resolution_m(paths[name])
            if resolution is not None:
                resolutions[name] = resolution

    return {
        "input_dir": str(dest_input_dir),
        "produced": produced,
        "layer_dates": layer_dates,
        "layer_date_details": layer_date_details,
        "skipped_layers": skipped,
        "layer_resolutions_m": resolutions,
        "sentinel_nominal_resolution_m": 20 if include_satellite else None,
    }

# PostgreSQL lowercases identifiers on import, but the engine modules expect the
# original shapefile column casing. Re-alias on export (the SELECT must include
# the geometry column so OGR carries it through).
_VECTOR_SELECT_SQL: dict[str, str] = {
    "iuf": 'SELECT geom, code_18 AS "Code_18" FROM iuf',
}


_COMMON_PLAN: list[tuple[str, str, str]] = [
    (_RASTER, "dtm", "DTM/DTM.tif"),
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
    "static": [
        item
        for item in _COMMON_PLAN
        if item[1] not in {"lst", "sentinel_b4", "sentinel_b8", "sentinel_b11"}
    ],
    "dynamic": _COMMON_PLAN,
}


def reconstruct_inputs(
    dest_input_dir: str | Path,
    *,
    engine: str,
    target_date,
    start_date=None,
    include_weather: bool = True,
    include_history: bool = True,
    include_terrain: bool = True,
    include_satellite: bool = True,
    include_lst: bool | None = None,
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
    include_lst = (include_weather and engine == "dynamic") if include_lst is None else include_lst

    fwi_files: list[Path] = []
    if include_weather:
        print("[reconstruct] copying FWI weather files (run-up window) from DB blobs", flush=True)
        fwi_files = reconstruct_fwi(
            target_date, dest_input_dir / "FWI", score_start=start_date
        )
        print(f"[reconstruct] FWI: {len(fwi_files)} day file(s) materialised", flush=True)

    temporal = reconstruct_temporal_inputs(
        dest_input_dir,
        target_date=target_date,
        include_lst=bool(include_lst),
        include_satellite=bool(include_satellite and engine == "dynamic"),
        clip_geom=clip_geom,
        clip_geom_crs=clip_geom_crs,
    )
    produced.update(temporal["produced"])

    temporal_tables = set(TEMPORAL_TS_TABLES)
    _plan = [
        item
        for item in _ENGINE_PLANS[engine]
        if item[1] not in temporal_tables and (include_terrain or item[1] != "twi")
    ]
    for _n, (kind, table, rel) in enumerate(_plan, start=1):
        print(f"[reconstruct] {_n:>2}/{len(_plan)} exporting {table} -> {rel}", flush=True)
        dest = dest_input_dir / rel
        if kind == _RASTER:
            resampling = "near" if table in {"fuels"} else "bilinear"
            export_raster_table(table, dest, clip_geom=clip_geom,
                                clip_geom_crs=clip_geom_crs, target_srs=ENGINE_RASTER_SRS,
                                resampling=resampling)
        else:
            export_vector_table(table, dest, clip_geom=clip_geom, clip_geom_crs=clip_geom_crs,
                                t_srs=ENGINE_VECTOR_SRS, select_sql=_VECTOR_SELECT_SQL.get(table))
        produced[rel] = str(dest)

    # Historical fire (both engines): yearly perimeters from the `hist` table
    # plus the on-disk PRE_FIRE / POST_FIRE Sentinel scenes.
    hist_info: dict[str, object] = {"years": [], "complete_scene_years": []}
    if include_history:
        print("[reconstruct] exporting fire history (hotspot shapefiles + dNBR scenes)", flush=True)
        hist_info = reconstruct_hist(dest_input_dir / "HIST",
                                     clip_geom=clip_geom, clip_geom_crs=clip_geom_crs,
                                     target_date=target_date)

    return {
        "input_dir": str(dest_input_dir),
        "produced": produced,
        "fwi_files": [str(p) for p in fwi_files],
        "hist": hist_info,
        "layer_dates": temporal["layer_dates"],
        "layer_date_details": temporal["layer_date_details"],
        "skipped_layers": temporal["skipped_layers"],
        "layer_resolutions_m": temporal["layer_resolutions_m"],
        "sentinel_nominal_resolution_m": temporal["sentinel_nominal_resolution_m"],
    }
