"""Simulation-triggering endpoints (whole-region engines and AOI workflows)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.engines.FFRM_estatic_aoi import run_static_aoi

from app.schemas import StaticAOIRequest, WildfireCalculationRequest
from app.services.jobs import (
    augment_with_urls,
    raise_aoi_http_error,
    run_engine_job,
    run_wildfire_payload,
    store_results_to_db,
    validate_risk_outputs,
)

router = APIRouter()


@router.post("/run-dynamic")
def run_dynamic(payload: WildfireCalculationRequest, request: Request):
    """
    Reconstruct inputs from PostGIS (clipped to the request boundary) and run the
    dynamic risk engine.
    """
    try:
        return run_engine_job(payload, "dynamic", request)
    except Exception as e:
        raise_aoi_http_error(e)


@router.post("/run-static")
def run_static(payload: WildfireCalculationRequest, request: Request):
    """
    Reconstruct inputs from PostGIS (clipped to the request boundary) and run the
    whole-region static risk engine.
    """
    try:
        return run_engine_job(payload, "static", request)
    except Exception as e:
        raise_aoi_http_error(e)


@router.post("/run-static-aoi")
def run_static_aoi_request(payload: StaticAOIRequest, request: Request):
    """
    Runs the static workflow for one coordinate AOI and one selected FWI date.
    """
    try:
        outputs = run_static_aoi(
            payload.longitude,
            payload.latitude,
            payload.date,
            buffer_m=payload.buffer_m,
            context_buffer_m=payload.context_buffer_m,
            risk_profile=payload.risk_profile,
        )
        validate_risk_outputs(outputs)
        result: dict[str, Any] = {
            "status": "success",
            "requested_date": outputs["requested_date"],
            "target_date": outputs["target_date"],
            "outputs": augment_with_urls(outputs, request),
        }
        db_info, db_error = store_results_to_db(
            outputs,
            metadata={
                "job_id": outputs.get("request_id"),
                "user_id": None,
                "model_id": None,
                "session_id": None,
                "engine": "static_aoi",
                "calculation_mode": "static",
                "request_type": "point",
                "target_date": outputs["target_date"],
                "requested_date": outputs["requested_date"],
                "longitude": payload.longitude,
                "latitude": payload.latitude,
                "risk_profile": payload.risk_profile,
            },
        )
        if db_info is not None:
            result["db_store"] = db_info
        if db_error is not None:
            result["db_store_error"] = db_error
        return result
    except Exception as e:
        raise_aoi_http_error(e)


@router.post("/run-static-aoi-wildfire")
def run_static_aoi_wildfire_request(payload: WildfireCalculationRequest, request: Request):
    """
    Runs the static workflow from the generic wildfire calculation payload.
    """
    try:
        return run_wildfire_payload(payload, request)
    except Exception as e:
        raise_aoi_http_error(e)


@router.post("/calliope/start")
def calliope_start(payload: WildfireCalculationRequest, request: Request):
    """
    Start a wildfire risk assessment (wildfire-platform default endpoint).
    """
    try:
        return run_wildfire_payload(payload, request)
    except Exception as e:
        raise_aoi_http_error(e)
