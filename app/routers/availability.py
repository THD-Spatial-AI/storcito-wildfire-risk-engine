"""Available dates and data-coverage endpoints."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from FR.db_reconstruct import available_dynamic_fwi_dates_db, available_fwi_dates_db, highest_temperature_fwi_dates_db

from app.config import MODEL_VERSION
from app.services.coverage import available_data_coverage_geojson

router = APIRouter()


@router.get("/available-static-dates")
def available_static_dates():
    # One day per year: the highest-peak-temperature FWI day.
    dates = highest_temperature_fwi_dates_db()
    return {"dates": [day.isoformat() for day in dates]}


@router.get("/available-dynamic-dates")
def available_dynamic_dates():
    dates = available_dynamic_fwi_dates_db()
    forecast: list[str] = []
    if dates:
        from datetime import timedelta

        from FR.db_reconstruct import (
            _pg_connect,
            FIRE_SEASON_END_MONTH,
            FIRE_SEASON_START_MONTH,
            _ts_date_for,
        )

        newest = dates[-1]
        # Only days beyond the FWI archive are forecasts; if eligibility is
        # capped by Sentinel/LST instead, +1/+2 are past dates, not forecasts.
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT max(fdate) FROM fwi_files")
            newest_fwi = cur.fetchone()[0]
        if newest_fwi is None or newest < newest_fwi:
            return {"dates": [day.isoformat() for day in dates], "forecast_dates": []}
        lst_age = int(os.environ.get("STORCITO_MAX_LST_AGE_DAYS", "3"))
        for offset in (1, 2):
            day = newest + timedelta(days=offset)
            if not FIRE_SEASON_START_MONTH <= day.month <= FIRE_SEASON_END_MONTH:
                continue
            try:
                if _ts_date_for("lst_ts", day, max_age_days=lst_age):
                    forecast.append(day.isoformat())
            except LookupError:
                break
    return {
        "dates": [day.isoformat() for day in dates] + forecast,
        "forecast_dates": forecast,
    }


@router.get("/available-data-coverage")
def available_data_coverage():
    try:
        return available_data_coverage_geojson()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

@router.get("/available-precomputed-dates")
def available_precomputed_dates():
    """Dates whose whole-region dynamic map has been precomputed (nightly job)."""
    try:
        from FR.db_reconstruct import _pg_connect

        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.regional_runs')")
            if cur.fetchone()[0] is None:
                return {"dates": []}
            cur.execute(
                "SELECT target_date FROM regional_runs "
                "WHERE engine = 'dynamic' AND status = 'done' AND model_version = %s "
                "AND publication_id IS NOT NULL ORDER BY target_date",
                (MODEL_VERSION,),
            )
            return {"dates": [r[0].isoformat() for r in cur.fetchall()]}
    except Exception:
        return {"dates": []}
