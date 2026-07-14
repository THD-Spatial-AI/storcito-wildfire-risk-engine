from __future__ import annotations

import os
import re
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

_CREATE_SQL = f"""CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} ( id bigserial PRIMARY KEY, job_id text, session_id text, user_id text, model_id text, publication_id text, model_version text, engine text, calculation_mode text, request_type text, map_kind text NOT NULL, target_date date, source_path text, metadata jsonb, aoi geometry(Geometry, 4326), created_at timestamptz NOT NULL DEFAULT now(), rast raster ); ALTER TABLE {RESULTS_TABLE} ADD COLUMN IF NOT EXISTS publication_id text; ALTER TABLE {RESULTS_TABLE} ADD COLUMN IF NOT EXISTS model_version text; CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_job_id_idx ON {RESULTS_TABLE} (job_id); CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_session_id_idx ON {RESULTS_TABLE} (session_id); CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_target_date_idx ON {RESULTS_TABLE} (target_date); CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_publication_idx ON {RESULTS_TABLE} (publication_id); CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_aoi_gix ON {RESULTS_TABLE} USING gist (aoi);"""

_INSERT_SQL = f"""INSERT INTO {RESULTS_TABLE} (job_id, session_id, user_id, model_id, publication_id, model_version, engine, calculation_mode, request_type, map_kind, target_date, source_path, metadata, aoi, rast) VALUES (%(job_id)s, %(session_id)s, %(user_id)s, %(model_id)s, %(publication_id)s, %(model_version)s, %(engine)s, %(calculation_mode)s, %(request_type)s, %(map_kind)s, %(target_date)s, %(source_path)s, %(metadata)s, CASE WHEN %(aoi)s IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(%(aoi)s), 4326) END, ST_FromGDALRaster(%(rast)s)) RETURNING id, ST_SRID(rast), ST_Width(rast), ST_Height(rast);"""


def _dsn() -> str:
    """Connection string: prefer DATABASE_URL, else the PG* params used for reads."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    return psycopg2.extensions.make_dsn(**_pg_params())


def store_result_maps(
    outputs: dict[str, Any],
    *,
    metadata: dict[str, Any],
    aoi_geojson: str | None = None,
    map_keys: tuple[str, ...] = DEFAULT_MAP_KEYS,
) -> dict[str, Any]:
    """Insert the named result maps from ``outputs`` into the shared results table. ``outputs`` maps result keys to GeoTIFF paths (e.g. the dict returned by the engine jobs / AOI workflow). ``metadata`` carries the per-request descriptors used both for the dedicated columns and the ``metadata`` jsonb blob. ``aoi_geojson`` is an optional WGS84 GeoJSON *geometry* string stored as the row footprint. Raises on connection/SQL failure; callers run this best-effort so a storage problem never fails an otherwise-successful simulation."""
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

    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SET postgis.gdal_enabled_drivers = %s;", (_GDAL_DRIVERS,))
            cur.execute(_CREATE_SQL)
            stored: list[dict[str, Any]] = []
            for kind, path in to_store:
                params = {
                    **common,
                    "map_kind": kind,
                    "source_path": str(path),
                    "rast": psycopg2.Binary(path.read_bytes()),
                }
                cur.execute(_INSERT_SQL, params)
                row_id, srid, width, height = cur.fetchone()
                stored.append({
                    "map_kind": kind,
                    "id": row_id,
                    "srid": srid,
                    "width": width,
                    "height": height,
                    "source_path": str(path),
                })
        conn.commit()
    finally:
        conn.close()

    return {"table": RESULTS_TABLE, "stored": stored}
