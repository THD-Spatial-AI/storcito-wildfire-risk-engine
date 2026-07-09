"""PostGIS catalog inspection endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.routers.fwi import raise_db_http_error

router = APIRouter()


@router.get("/db/tables")
def db_list_tables():
    """List the public PostGIS tables (vector / raster) with kind, srid and row estimate."""
    try:
        from FR.db_catalog import list_tables

        return {"tables": list_tables()}
    except Exception as exc:  # noqa: BLE001
        raise_db_http_error(exc)


@router.get("/db/tables/{table}")
def db_describe_table(table: str):
    """Describe one table: columns, srid, exact row count, WGS84 extent, region/date metadata."""
    try:
        from FR.db_catalog import describe_table

        return describe_table(table)
    except Exception as exc:  # noqa: BLE001
        raise_db_http_error(exc)


@router.get("/db/vector/{table}")
def db_vector_table(
    table: str,
    limit: int = Query(default=100, ge=1, le=1000),
    bbox: str | None = Query(default=None, description="minLon,minLat,maxLon,maxLat (WGS84)"),
    region: str | None = Query(default=None),
):
    """Return a vector table as a GeoJSON FeatureCollection (WGS84), capped by `limit`."""
    try:
        from FR.db_catalog import vector_geojson

        parsed_bbox = None
        if bbox is not None:
            parts = [p for p in bbox.split(",") if p.strip() != ""]
            if len(parts) != 4:
                raise ValueError("bbox must be 'minLon,minLat,maxLon,maxLat'.")
            try:
                parsed_bbox = tuple(float(p) for p in parts)
            except ValueError as exc:
                raise ValueError("bbox values must be numeric.") from exc
        return vector_geojson(table, limit=limit, bbox=parsed_bbox, region=region)
    except Exception as exc:  # noqa: BLE001
        raise_db_http_error(exc)


@router.get("/db/raster/{table}")
def db_raster_table(table: str):
    """Summarise a raster table: tile count, srid, bands, WGS84 extent, regions/dates."""
    try:
        from FR.db_catalog import raster_metadata

        return raster_metadata(table)
    except Exception as exc:  # noqa: BLE001
        raise_db_http_error(exc)
