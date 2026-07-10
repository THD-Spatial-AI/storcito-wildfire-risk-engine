"""Parsing and validation of the wildfire calculation payload."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from FR.aoi import build_geojson_aoi, reproject_geometry, DEFAULT_PROJECTED_CRS

from app.config import BERLIN_TZ
from app.config import logger
from app.schemas import WildfireCalculationRequest


def to_berlin_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BERLIN_TZ)
    return value.astimezone(BERLIN_TZ)


def wildfire_target_date(payload: WildfireCalculationRequest) -> date:
    start_local = to_berlin_time(payload.start_date)
    end_local = to_berlin_time(payload.end_date)

    if start_local.date() != end_local.date():
        raise ValueError("start_date and end_date must be on the same Europe/Berlin local date.")

    return start_local.date()


def wildfire_date_range(
    payload: WildfireCalculationRequest, calculation_mode: str
) -> tuple[date | None, date]:
    start_local = to_berlin_time(payload.start_date)
    end_local = to_berlin_time(payload.end_date)

    start_day = start_local.date()
    end_day = end_local.date()
    if calculation_mode == "static" and start_day != end_day:
        raise ValueError("start_date and end_date must be on the same Europe/Berlin local date.")
    if start_day > end_day:
        raise ValueError("start_date must be before or equal to end_date.")

    return (start_day if calculation_mode == "dynamic" else None, end_day)


def unwrap_geojson_geometry(node: Any) -> dict | None:
    """Unwrap a GeoJSON Feature / FeatureCollection down to a geometry object."""
    if not isinstance(node, dict):
        return None
    node_type = node.get("type")
    if node_type == "FeatureCollection":
        geoms = [g for g in (unwrap_geojson_geometry(f) for f in node.get("features", []) or [])
                 if g is not None]
        if not geoms:
            return None
        if len(geoms) == 1:
            return geoms[0]
        from shapely.geometry import mapping as _mapping, shape as _shape
        from shapely.ops import unary_union

        return _mapping(unary_union([_shape(g) for g in geoms]))
    if node_type == "Feature":
        return unwrap_geojson_geometry(node.get("geometry"))
    if "type" in node and "coordinates" in node:
        return node
    return None


def _require_inside_coverage(geometry_wgs84) -> None:
    """Reject AOIs outside the data region: the engine would otherwise
    substitute nearest-cell weather and produce fabricated results.
    Skipped (open) when the region polygon is unavailable.
    """
    import json
    import os

    pattern = os.environ.get("STORCITO_COVERAGE_REGION", "%galicia%")
    try:
        from shapely.geometry import shape as shapely_shape

        from FR.db_reconstruct import _pg_connect

        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT ST_AsGeoJSON(geom) FROM spain_autonomous_communities "
                "WHERE acom_name ILIKE %s LIMIT 1",
                (pattern,),
            )
            row = cur.fetchone()
        if not row:
            return
        region = shapely_shape(json.loads(row[0]))
    except Exception as exc:  # noqa: BLE001
        # Fail open (the engine will fail anyway without the DB), but loudly.
        logger.warning("Coverage check skipped (region lookup failed): %s", exc)
        return
    inside = geometry_wgs84.intersection(region).area
    total = geometry_wgs84.area
    mostly_inside = total > 0 and inside / total >= 0.5
    # Anchored (representative point inside) only helps genuinely coastal /
    # border AOIs; a continent-sized box anchored in Galicia must not pass.
    anchored = (
        region.contains(geometry_wgs84.representative_point())
        and total > 0 and inside / total >= 0.10
    )
    if not (mostly_inside or anchored):
        raise ValueError(
            "The requested area lies mostly outside the wildfire data coverage "
            "region; see GET /available-data-coverage for the supported boundary."
        )


def wildfire_geometry(payload: WildfireCalculationRequest):
    geometry = unwrap_geojson_geometry(payload.coordinates)
    if geometry is None:
        for item in payload.topology:
            if not isinstance(item, dict):
                continue
            candidate = unwrap_geojson_geometry(item.get("geometry")) or unwrap_geojson_geometry(item)
            if candidate is not None:
                geometry = candidate
                break
    if geometry is None:
        raise ValueError("coordinates or topology[0].geometry must contain a GeoJSON geometry.")

    from shapely.geometry import shape as shapely_shape

    _require_inside_coverage(shapely_shape(geometry))

    projected = build_geojson_aoi(geometry)
    if payload.buffer_distance > 0:
        projected = projected.buffer(payload.buffer_distance)
    return projected


def wildfire_context_buffer(payload: WildfireCalculationRequest) -> float:
    raw_value = payload.parameters.get("context_buffer_m", 3000)
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("parameters.context_buffer_m must be numeric when provided.") from exc
    if value < 0:
        raise ValueError("parameters.context_buffer_m must be greater than or equal to zero.")
    return value


def wildfire_calculation_mode(payload: WildfireCalculationRequest) -> str:
    mode = str(payload.parameters.get("calculation_mode", "static")).strip().lower()
    if mode not in {"static", "dynamic"}:
        raise ValueError("parameters.calculation_mode must be either 'static' or 'dynamic' when provided.")
    return mode


def wildfire_optional_layers(payload: WildfireCalculationRequest) -> dict[str, bool] | None:
    raw = payload.parameters.get("optional_layers")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("parameters.optional_layers must be an object mapping layer keys to booleans.")
    result: dict[str, bool] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError("parameters.optional_layers keys must be strings.")
        result[key] = bool(value)
    return result


def wildfire_risk_profile(payload: WildfireCalculationRequest) -> str:
    raw = payload.parameters.get("risk_profile", payload.parameters.get("profile", "regional"))
    profile = str(raw).strip().lower()
    if profile not in {"regional", "finca"}:
        raise ValueError("parameters.risk_profile must be either 'regional' or 'finca' when provided.")
    return profile


def wildfire_user_input_model_id(payload: WildfireCalculationRequest) -> str:
    """Persistent model id for reusable user inputs (run ids append a timestamp)."""
    raw = payload.parameters.get("source_model_id")
    if isinstance(raw, (str, int, float)) and str(raw).strip():
        return str(raw).strip()
    return str(payload.model_id).split("_", 1)[0]


def wildfire_clip_geometry_wgs84(payload: WildfireCalculationRequest):
    """WGS84 clip boundary for DB exports (ValueError -> 422 when absent)."""
    projected = wildfire_geometry(payload)  # EPSG:32629, includes buffer_distance
    context_buffer_m = wildfire_context_buffer(payload)
    # Include the engines' internal 3000 m crop margin.
    processing = projected.buffer(context_buffer_m + 3000)
    return reproject_geometry(processing, DEFAULT_PROJECTED_CRS, "EPSG:4326")
