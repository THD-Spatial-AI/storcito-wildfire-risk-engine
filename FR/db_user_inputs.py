"""DB-backed storage for user-supplied engine inputs.

Uploaded DTMs and weather-station files are small in number but need to survive
container restarts and be reusable across runs. Store the original DTM GeoTIFF
bytes plus spatial metadata, and store station data after normalising it to the
CSV layout expected by the FWI station engine.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import rasterio
from rasterio.warp import transform_geom
from shapely.geometry import shape


KIND_DTM = "dtm"
KIND_STATION_DATA = "station_data"
VALID_KINDS = {KIND_DTM, KIND_STATION_DATA}
USER_INPUT_TABLE = "public.user_input_files"
USER_INPUT_CHUNK_TABLE = "public.user_input_file_chunks"
DTM_CHUNK_SIZE = int(os.environ.get("USER_INPUT_DTM_CHUNK_SIZE", str(8 * 1024 * 1024)))


def _log(message: str) -> None:
    print(f"[STORCITO user-inputs] {message}", flush=True)


def _pg_params() -> dict[str, str]:
    return {
        "host": os.environ.get("PGHOST", "postgis"),
        "port": os.environ.get("PGPORT", "5432"),
        "dbname": os.environ.get("PGDATABASE", "gis"),
        "user": os.environ.get("PGUSER", "gis"),
        "password": os.environ.get("PGPASSWORD", "gis"),
    }


def _connect():
    import psycopg2

    p = _pg_params()
    return psycopg2.connect(
        host=p["host"],
        port=p["port"],
        dbname=p["dbname"],
        user=p["user"],
        password=p["password"],
    )


DDL = """
CREATE TABLE IF NOT EXISTS public.user_input_files (
    id              bigserial PRIMARY KEY,
    user_id         text NOT NULL,
    model_id        text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('dtm', 'station_data')),
    filename        text NOT NULL,
    source_filename text,
    content_type    text,
    data            bytea NOT NULL,
    nbytes          bigint NOT NULL,
    raster_srid     integer,
    footprint       geometry(Polygon, 4326),
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, model_id, kind)
);

CREATE INDEX IF NOT EXISTS user_input_files_user_model_idx
    ON public.user_input_files (user_id, model_id);

ALTER TABLE public.user_input_files
    ADD COLUMN IF NOT EXISTS footprint geometry(Polygon, 4326);

CREATE INDEX IF NOT EXISTS user_input_files_footprint_gix
    ON public.user_input_files USING gist (footprint);

CREATE TABLE IF NOT EXISTS public.user_input_file_chunks (
    user_input_id bigint NOT NULL REFERENCES public.user_input_files(id) ON DELETE CASCADE,
    chunk_index   integer NOT NULL,
    data          bytea NOT NULL,
    nbytes        integer NOT NULL,
    PRIMARY KEY (user_input_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS user_input_file_chunks_input_idx
    ON public.user_input_file_chunks (user_input_id, chunk_index);
"""


def _ensure_schema(cur) -> None:
    _log(f"ensuring table {USER_INPUT_TABLE} exists")
    cur.execute(DDL)


def _validate_kind(kind: str) -> None:
    if kind not in VALID_KINDS:
        raise ValueError(f"Unsupported user input kind: {kind}")


def _raster_footprint(src) -> dict[str, Any]:
    corners = [
        src.transform * (0, 0),
        src.transform * (src.width, 0),
        src.transform * (src.width, src.height),
        src.transform * (0, src.height),
    ]
    coords = [(float(x), float(y)) for x, y in corners]
    coords.append(coords[0])
    return {"type": "Polygon", "coordinates": [coords]}


def _dtm_metadata(path: Path) -> tuple[int | None, dict[str, Any], dict[str, Any]]:
    _log(f"reading DTM metadata filename={path.name}")
    with rasterio.open(path) as src:
        srid = src.crs.to_epsg() if src.crs is not None else None
        source_crs = src.crs or "EPSG:4326"
        footprint = transform_geom(
            source_crs,
            "EPSG:4326",
            _raster_footprint(src),
            precision=7,
        )
        bounds = [float(value) for value in shape(footprint).bounds]
        metadata = {
            "driver": src.driver,
            "width": src.width,
            "height": src.height,
            "count": src.count,
            "dtype": src.dtypes[0] if src.dtypes else None,
            "crs": str(src.crs) if src.crs is not None else None,
            "bounds_wgs84": bounds,
            "footprint_wgs84": footprint,
        }
    _log(
        "DTM footprint ready "
        f"filename={path.name} srid={srid or 'unknown'} "
        f"bounds_wgs84={bounds}"
    )
    return srid, footprint, metadata


def _upsert_user_input(
    *,
    user_id: str,
    model_id: str,
    kind: str,
    filename: str,
    source_filename: str | None,
    content_type: str | None,
    data: bytes,
    raster_srid: int | None = None,
    footprint_geojson: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_kind(kind)
    import psycopg2
    from psycopg2.extras import Json

    footprint_text = json.dumps(footprint_geojson) if footprint_geojson is not None else None
    with _connect() as conn, conn.cursor() as cur:
        _ensure_schema(cur)
        _log(
            f"upserting {kind} into {USER_INPUT_TABLE} "
            f"user_id={user_id} model_id={model_id} "
            f"filename={filename} source_filename={source_filename or '-'} "
            f"bytes={len(data)} raster_srid={raster_srid or 'none'}"
        )
        cur.execute(
            """
            INSERT INTO public.user_input_files
                (user_id, model_id, kind, filename, source_filename, content_type,
                 data, nbytes, raster_srid, footprint, metadata)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                 CASE WHEN %s IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) END,
                 %s)
            ON CONFLICT (user_id, model_id, kind)
            DO UPDATE SET
                filename = EXCLUDED.filename,
                source_filename = EXCLUDED.source_filename,
                content_type = EXCLUDED.content_type,
                data = EXCLUDED.data,
                nbytes = EXCLUDED.nbytes,
                raster_srid = EXCLUDED.raster_srid,
                footprint = EXCLUDED.footprint,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            RETURNING kind, filename, source_filename, nbytes, raster_srid,
                      ST_AsGeoJSON(footprint), metadata, updated_at
            """,
            (
                user_id,
                model_id,
                kind,
                filename,
                source_filename,
                content_type,
                psycopg2.Binary(data),
                len(data),
                raster_srid,
                footprint_text,
                footprint_text,
                Json(metadata or {}),
            ),
        )
        row = cur.fetchone()
    result = _row_to_metadata(row)
    _log(
        f"stored {kind} in {USER_INPUT_TABLE} "
        f"user_id={user_id} model_id={model_id} "
        f"bytes={result['nbytes']} updated_at={result['updated_at']}"
    )
    return result


def _upsert_dtm_chunks(
    *,
    user_id: str,
    model_id: str,
    path: Path,
    filename: str,
    source_filename: str | None,
    content_type: str | None,
    raster_srid: int | None,
    footprint_geojson: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    import psycopg2
    from psycopg2.extras import Json

    footprint_text = json.dumps(footprint_geojson)
    file_size = path.stat().st_size
    chunk_size = max(1024 * 1024, DTM_CHUNK_SIZE)
    chunk_metadata = {
        **metadata,
        "storage_mode": "chunks",
        "chunk_size": chunk_size,
    }

    with _connect() as conn, conn.cursor() as cur:
        _ensure_schema(cur)
        _log(
            f"upserting chunked dtm metadata into {USER_INPUT_TABLE} "
            f"user_id={user_id} model_id={model_id} filename={filename} "
            f"source_filename={source_filename or '-'} bytes={file_size} "
            f"chunk_size={chunk_size} raster_srid={raster_srid or 'none'}"
        )
        cur.execute(
            """
            INSERT INTO public.user_input_files
                (user_id, model_id, kind, filename, source_filename, content_type,
                 data, nbytes, raster_srid, footprint, metadata)
            VALUES
                (%s, %s, 'dtm', %s, %s, %s, %s, %s, %s,
                 ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),
                 %s)
            ON CONFLICT (user_id, model_id, kind)
            DO UPDATE SET
                filename = EXCLUDED.filename,
                source_filename = EXCLUDED.source_filename,
                content_type = EXCLUDED.content_type,
                data = EXCLUDED.data,
                nbytes = EXCLUDED.nbytes,
                raster_srid = EXCLUDED.raster_srid,
                footprint = EXCLUDED.footprint,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            RETURNING id, kind, filename, source_filename, nbytes, raster_srid,
                      ST_AsGeoJSON(footprint), metadata, updated_at
            """,
            (
                user_id,
                model_id,
                filename,
                source_filename,
                content_type,
                psycopg2.Binary(b""),
                file_size,
                raster_srid,
                footprint_text,
                Json(chunk_metadata),
            ),
        )
        row = cur.fetchone()
        user_input_id = row[0]
        cur.execute(
            "DELETE FROM public.user_input_file_chunks WHERE user_input_id = %s",
            (user_input_id,),
        )

        uploaded = 0
        chunk_count = 0
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                cur.execute(
                    """
                    INSERT INTO public.user_input_file_chunks
                        (user_input_id, chunk_index, data, nbytes)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user_input_id, chunk_count, psycopg2.Binary(chunk), len(chunk)),
                )
                uploaded += len(chunk)
                chunk_count += 1
                _log(
                    f"DTM database chunk upload progress user_id={user_id} "
                    f"model_id={model_id} uploaded_bytes={uploaded} "
                    f"total_bytes={file_size} chunks={chunk_count}"
                )

    result = _row_to_metadata(row[1:])
    _log(
        f"stored chunked dtm in {USER_INPUT_TABLE}/{USER_INPUT_CHUNK_TABLE} "
        f"user_id={user_id} model_id={model_id} bytes={result['nbytes']} "
        f"updated_at={result['updated_at']}"
    )
    return result


def _row_to_metadata(row) -> dict[str, Any]:
    kind, filename, source_filename, nbytes, raster_srid, footprint_json, metadata, updated_at = row
    return {
        "kind": kind,
        "filename": filename,
        "source_filename": source_filename,
        "nbytes": int(nbytes),
        "raster_srid": raster_srid,
        "footprint": footprint_json,
        "metadata": metadata or {},
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }


def store_dtm_file(
    user_id: str,
    model_id: str,
    path: str | Path,
    *,
    source_filename: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    path = Path(path)
    _log(
        f"DTM database upload started user_id={user_id} model_id={model_id} "
        f"source_filename={source_filename or path.name}"
    )
    raster_srid, footprint_geojson, metadata = _dtm_metadata(path)
    _log(
        f"DTM bytes ready for chunked table storage filename={path.name} "
        f"bytes={path.stat().st_size}"
    )
    return _upsert_dtm_chunks(
        user_id=user_id,
        model_id=model_id,
        path=path,
        filename="dtm.tif",
        source_filename=source_filename or path.name,
        content_type=content_type,
        raster_srid=raster_srid,
        footprint_geojson=footprint_geojson,
        metadata=metadata,
    )


def store_station_csv_file(
    user_id: str,
    model_id: str,
    path: str | Path,
    *,
    source_filename: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    path = Path(path)
    data = path.read_bytes()
    _log(
        f"station CSV database upload started user_id={user_id} model_id={model_id} "
        f"source_filename={source_filename or path.name} bytes={len(data)}"
    )
    return _upsert_user_input(
        user_id=user_id,
        model_id=model_id,
        kind=KIND_STATION_DATA,
        filename="station_data.csv",
        source_filename=source_filename or path.name,
        content_type=content_type or "text/csv",
        data=data,
        metadata={"format": "csv"},
    )


def materialize_user_input(
    user_id: str,
    model_id: str,
    kind: str,
    dest_path: str | Path,
) -> Path | None:
    _validate_kind(kind)
    dest_path = Path(dest_path)
    with _connect() as conn, conn.cursor() as cur:
        _ensure_schema(cur)
        _log(
            f"looking for stored {kind} in {USER_INPUT_TABLE} "
            f"user_id={user_id} model_id={model_id}"
        )
        cur.execute(
            """
            SELECT id, data, metadata
            FROM public.user_input_files
            WHERE user_id = %s AND model_id = %s AND kind = %s
            """,
            (user_id, model_id, kind),
        )
        row = cur.fetchone()
        if row is None:
            _log(
                f"no stored {kind} found in {USER_INPUT_TABLE} "
                f"user_id={user_id} model_id={model_id}"
            )
            return None

        user_input_id, data, metadata = row
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(metadata, dict) and metadata.get("storage_mode") == "chunks":
            cur.execute(
                """
                SELECT chunk_index, data
                FROM public.user_input_file_chunks
                WHERE user_input_id = %s
                ORDER BY chunk_index
                """,
                (user_input_id,),
            )
            chunk_count = 0
            written = 0
            with dest_path.open("wb") as fh:
                for _, chunk_data in cur:
                    chunk = bytes(chunk_data)
                    fh.write(chunk)
                    written += len(chunk)
                    chunk_count += 1
            if chunk_count == 0:
                _log(
                    f"stored {kind} has chunk metadata but no chunks "
                    f"user_id={user_id} model_id={model_id}"
                )
                return None
            _log(
                f"materialized chunked stored {kind} from {USER_INPUT_CHUNK_TABLE} "
                f"-> {dest_path} bytes={written} chunks={chunk_count}"
            )
            return dest_path

        dest_path.write_bytes(bytes(data))
        _log(f"materialized stored {kind} from {USER_INPUT_TABLE} -> {dest_path}")
    return dest_path


def list_user_inputs(user_id: str, model_id: str) -> list[dict[str, Any]]:
    with _connect() as conn, conn.cursor() as cur:
        _ensure_schema(cur)
        cur.execute(
            """
            SELECT kind, filename, source_filename, nbytes, raster_srid,
                   ST_AsGeoJSON(footprint), metadata, updated_at
            FROM public.user_input_files
            WHERE user_id = %s AND model_id = %s
            ORDER BY kind
            """,
            (user_id, model_id),
        )
        return [_row_to_metadata(row) for row in cur.fetchall()]
