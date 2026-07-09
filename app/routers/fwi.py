"""DB-backed FWI weather sampling endpoints."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from shapely.geometry import shape

from app.schemas import FWIAreaSummaryRequest
from app.services.fwi_sampling import sample_fwi_area_from_db, sample_fwi_point_from_db
from app.services.payload import unwrap_geojson_geometry

router = APIRouter()


def raise_db_http_error(exc: Exception) -> None:
    """Map db_catalog errors to HTTP responses."""
    if type(exc).__name__ == "UnknownTable":
        raise HTTPException(status_code=404, detail=f"Unknown table: {exc}") from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ModuleNotFoundError):
        raise HTTPException(
            status_code=503,
            detail="Database driver unavailable (psycopg2 not installed; rebuild the image).",
        ) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/fwi/area")
def fwi_area_summary(payload: FWIAreaSummaryRequest):
    """Return DB-backed WRF/FWI weather and moisture-code values over an AOI."""
    try:
        geometry = unwrap_geojson_geometry(payload.aoi)
        if geometry is None:
            raise ValueError("aoi must be a GeoJSON geometry, Feature, or FeatureCollection.")
        aoi_wgs84 = shape(geometry)
        if aoi_wgs84.is_empty:
            raise ValueError("aoi geometry is empty.")
        return sample_fwi_area_from_db(
            target_date=payload.date,
            aoi_wgs84=aoi_wgs84,
            hour_index=payload.hour_index,
        )
    except Exception as exc:  # noqa: BLE001
        raise_db_http_error(exc)


@router.get("/fwi/point")
def fwi_point_sample(
    fdate: date = Query(..., alias="date"),
    lon: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
    hour_index: int = Query(default=15, ge=0, le=95),
    include_runup: bool = Query(default=True),
):
    """Return DB-backed WRF/FWI weather and moisture-code values at one point."""
    try:
        return sample_fwi_point_from_db(
            target_date=fdate,
            lon=lon,
            lat=lat,
            hour_index=hour_index,
            include_runup=include_runup,
        )
    except Exception as exc:  # noqa: BLE001
        raise_db_http_error(exc)
