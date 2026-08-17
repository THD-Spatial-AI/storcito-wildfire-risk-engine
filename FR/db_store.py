from __future__ import annotations

import os
import re
from math import ceil
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import Json

from FR.db_reconstruct import _pg_params

# Shared results table. Overridable via env; validated as a bare identifier so it can be safely interpolated into the DDL/DML below.
RESULTS_TABLE = os.environ.get("STORCITO_RESULTS_TABLE", "simulation_results").strip()
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", RESULTS_TABLE):
    raise ValueError(f"Invalid STORCITO_RESULTS_TABLE name: {RESULTS_TABLE!r}")

# GDAL drivers to enable for ST_FromGDALRaster (engine outputs are GeoTIFFs).
_GDAL_DRIVERS = os.environ.get("STORCITO_GDAL_DRIVERS", "GTiff").strip()

# Result keys produced by both the engine jobs and the AOI combine step.
DEFAULT_MAP_KEYS = ("final_map", "continuous_map", "data_coverage")

DEFAULT_RESULT_TILE_SIZE = 1024
MIN_RESULT_TILE_SIZE = 128
MAX_RESULT_TILE_SIZE = 4096

_CREATE_SQL = f"""CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} ( id bigserial PRIMARY KEY, job_id text, session_id text, user_id text, model_id text, publication_id text, model_version text, engine text, calculation_mode text, request_type text, map_kind text NOT NULL, target_date date, source_path text, metadata jsonb, aoi geometry(Geometry, 4326), created_at timestamptz NOT NULL DEFAULT now(), rast raster ); ALTER TABLE {RESULTS_TABLE} ADD COLUMN IF NOT EXISTS publication_id text; ALTER TABLE {RESULTS_TABLE} ADD COLUMN IF NOT EXISTS model_version text; CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_job_id_idx ON {RESULTS_TABLE} (job_id); CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_session_id_idx ON {RESULTS_TABLE} (session_id); CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_target_date_idx ON {RESULTS_TABLE} (target_date); CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_publication_idx ON {RESULTS_TABLE} (publication_id); CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_aoi_gix ON {RESULTS_TABLE} USING gist (aoi);"""

_INSERT_SQL = f"""INSERT INTO {RESULTS_TABLE} (job_id, session_id, user_id, model_id, publication_id, model_version, engine, calculation_mode, request_type, map_kind, target_date, source_path, metadata, aoi, rast) VALUES (%(job_id)s, %(session_id)s, %(user_id)s, %(model_id)s, %(publication_id)s, %(model_version)s, %(engine)s, %(calculation_mode)s, %(request_type)s, %(map_kind)s, %(target_date)s, %(source_path)s, %(metadata)s, CASE WHEN %(aoi)s IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(%(aoi)s), 4326) END, ST_FromGDALRaster(%(rast)s)) RETURNING id, ST_SRID(rast), ST_Width(rast), ST_Height(rast);"""


def _dsn() -> str:
    """Connection string: prefer DATABASE_URL, else the PG* params used for reads."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    return psycopg2.extensions.make_dsn(**_pg_params())


def _result_tile_size() -> int:
    raw = os.environ.get(
        "STORCITO_RESULT_TILE_SIZE", str(DEFAULT_RESULT_TILE_SIZE)
    ).strip()
    try:
        tile_size = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"STORCITO_RESULT_TILE_SIZE must be an integer, got {raw!r}"
        ) from exc
    if not MIN_RESULT_TILE_SIZE <= tile_size <= MAX_RESULT_TILE_SIZE:
        raise ValueError(
            "STORCITO_RESULT_TILE_SIZE must be between "
            f"{MIN_RESULT_TILE_SIZE} and {MAX_RESULT_TILE_SIZE}, got {tile_size}"
        )
    return tile_size


def _tile_geotiff_bytes(src, window) -> bytes:
    """Encode one source window as a standalone georeferenced GeoTIFF."""
    from rasterio.io import MemoryFile
    from rasterio.windows import transform as window_transform

    profile = src.profile.copy()
    profile.update(
        driver="GTiff",
        width=int(window.width),
        height=int(window.height),
        transform=window_transform(window, src.transform),
        BIGTIFF="NO",
    )
    with MemoryFile() as memory:
        with memory.open(**profile) as tile:
            tile.write(src.read(window=window))
            dataset_tags = src.tags()
            if dataset_tags:
                tile.update_tags(**dataset_tags)
            for band in range(1, src.count + 1):
                band_tags = src.tags(band)
                if band_tags:
                    tile.update_tags(band, **band_tags)
        return memory.read()


def store_result_maps(
    outputs: dict[str, Any],
    *,
    metadata: dict[str, Any],
    aoi_geojson: str | None = None,
    map_keys: tuple[str, ...] = DEFAULT_MAP_KEYS,
) -> dict[str, Any]:
    """Insert result maps into the shared table as bounded-size raster tiles.

    ``outputs`` maps result keys to GeoTIFF paths (e.g. the dict returned by
    the engine jobs / AOI workflow). ``metadata`` carries the per-request
    descriptors used both for the dedicated columns and the ``metadata``
    jsonb blob. ``aoi_geojson`` is an optional WGS84 GeoJSON geometry string
    stored as the row footprint.

    Every source map is split into independently georeferenced GeoTIFF tiles
    before it reaches psycopg2/PostGIS. This avoids both a whole-file Python
    allocation and PostgreSQL's per-value/per-allocation limit on regional
    rasters. All maps and tiles are committed atomically.

    Raises on connection/SQL failure; callers run this best-effort so a
    storage problem never fails an otherwise-successful simulation.
    """
    to_store: list[tuple[str, Path]] = []
    missing: list[str] = []
    for key in map_keys:
        raw = outputs.get(key)
        if not isinstance(raw, str) or not raw:
            missing.append(key)
            continue
        path = Path(raw)
        if path.is_file():
            to_store.append((key, path))
        else:
            missing.append(key)

    if missing:
        raise FileNotFoundError(f"required result maps missing: {', '.join(missing)}")

    common = {
        "job_id": metadata.get("job_id"),
        "session_id": metadata.get("session_id"),
        "user_id": metadata.get("user_id"),
        "model_id": metadata.get("model_id"),
        "publication_id": metadata.get("publication_id"),
        "model_version": metadata.get("model_version"),
        "engine": metadata.get("engine"),
        "calculation_mode": metadata.get("calculation_mode"),
        "request_type": metadata.get("request_type"),
        "target_date": metadata.get("target_date"),
        "metadata": Json(metadata),
        "aoi": aoi_geojson,
    }

    import rasterio
    from rasterio.windows import Window

    tile_size = _result_tile_size()
    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SET postgis.gdal_enabled_drivers = %s;", (_GDAL_DRIVERS,))
            cur.execute(_CREATE_SQL)
            stored: list[dict[str, Any]] = []
            for kind, path in to_store:
                first_id = None
                last_id = None
                stored_srid = None
                tile_count = 0
                with rasterio.open(path) as src:
                    source_width = src.width
                    source_height = src.height
                    expected_tiles = ceil(source_width / tile_size) * ceil(
                        source_height / tile_size
                    )
                    for row_off in range(0, source_height, tile_size):
                        height = min(tile_size, source_height - row_off)
                        for col_off in range(0, source_width, tile_size):
                            width = min(tile_size, source_width - col_off)
                            window = Window(col_off, row_off, width, height)
                            params = {
                                **common,
                                "map_kind": kind,
                                "source_path": str(path),
                                "rast": psycopg2.Binary(
                                    _tile_geotiff_bytes(src, window)
                                ),
                            }
                            cur.execute(_INSERT_SQL, params)
                            row_id, srid, inserted_width, inserted_height = (
                                cur.fetchone()
                            )
                            if (inserted_width, inserted_height) != (width, height):
                                raise RuntimeError(
                                    f"PostGIS changed {kind} tile dimensions from "
                                    f"{width}x{height} to "
                                    f"{inserted_width}x{inserted_height}"
                                )
                            first_id = row_id if first_id is None else first_id
                            last_id = row_id
                            stored_srid = srid
                            tile_count += 1
                if tile_count != expected_tiles:
                    raise RuntimeError(
                        f"stored {tile_count} of {expected_tiles} expected "
                        f"tiles for {kind}"
                    )
                stored.append(
                    {
                        "map_kind": kind,
                        # Keep the historical id field as the first physical
                        # row while exposing the complete tiled span.
                        "id": first_id,
                        "first_id": first_id,
                        "last_id": last_id,
                        "tile_count": tile_count,
                        "tile_size": tile_size,
                        "srid": stored_srid,
                        "width": source_width,
                        "height": source_height,
                        "source_path": str(path),
                    }
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "table": RESULTS_TABLE,
        "storage": "tiled",
        "tile_size": tile_size,
        "stored": stored,
    }
