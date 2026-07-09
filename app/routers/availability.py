"""Available dates and data-coverage endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from FR.db_reconstruct import available_fwi_dates_db, highest_temperature_fwi_dates_db

from app.services.coverage import available_data_coverage_geojson

router = APIRouter()


@router.get("/available-static-dates")
def available_static_dates():
    # One day per year: the highest-peak-temperature FWI day.
    dates = highest_temperature_fwi_dates_db()
    return {"dates": [day.isoformat() for day in dates]}


@router.get("/available-dynamic-dates")
def available_dynamic_dates():
    dates = available_fwi_dates_db()
    return {"dates": [day.isoformat() for day in dates]}


@router.get("/available-data-coverage")
def available_data_coverage():
    try:
        return available_data_coverage_geojson()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
