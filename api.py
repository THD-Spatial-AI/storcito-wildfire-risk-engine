import os
import re
import json
import logging
import zipfile
from functools import lru_cache

# Pre-import pyogrio so its GDAL_DATA initialisation happens cleanly at module
# load (before anything else, e.g. rasterio, gets a chance to rewrite GDAL's
# internal config state). NOTE: this only works when uvicorn is started
# *without* --reload — under --reload's spawned subprocess pyogrio's GDAL
# probe fails. The container therefore runs without --reload; use
# `make restart` to pick up code changes.
import pyogrio  # noqa: F401

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import rasterio
from rasterio import features
from rasterio.enums import MaskFlags
from rasterio.warp import transform_bounds, transform_geom
from shapely.geometry import box, mapping, shape

from FFRM_estatic_aoi import run_static_aoi, run_static_aoi_for_geometry
from FR.aoi import build_geojson_aoi, reproject_geometry, DEFAULT_PROJECTED_CRS
from FR.db_reconstruct import (
    reconstruct_inputs,
    available_fwi_dates_db,
    highest_temperature_fwi_dates_db,
)
from FR.db_user_inputs import (
    KIND_DTM,
    KIND_STATION_DATA,
    USER_INPUT_TABLE,
    materialize_user_input,
    store_dtm_file,
    store_station_csv_file,
)

app = FastAPI(title="STORCITO API")
BASE_DIR = Path(__file__).resolve().parent
AOI_OUTPUT_ROOT = (BASE_DIR / "OUTPUT" / "aoi").resolve()
JOBS_OUTPUT_ROOT = (BASE_DIR / "OUTPUT" / "jobs").resolve()
BERLIN_TZ = ZoneInfo("Europe/Berlin")

# Whole-region engines triggered by their own endpoints, each mapped to its
# script and the run-flag overrides needed to skip layers with no DB data.
ENGINE_SCRIPTS = {
    "static": {
        "script": "FFRM_static.py",
        "result": "forest_fire_risk_map.tif",
        # HIST is now reconstructed from the `hist` table + on-disk PRE/POST scenes.
        "run_flags": {"FFRM_RUN_FHIST": "1"},
    },
    "dynamic": {
        "script": "FFRM_dinamic.py",
        "result": "forest_fire_risk_map_dinamico.tif",
        # TWI / LST are now reconstructed from the `twi` / `lst` tables.
        "run_flags": {"FFRM_RUN_TWI": "1", "FFRM_RUN_LST": "1"},
    },
}

COVERAGE_INPUT_RASTERS = {
    "DTM": BASE_DIR / "INPUT" / "DTM" / "DTM.tif",
    "Sentinel B4": BASE_DIR / "INPUT" / "Sentinel" / "B4.tiff",
    "Sentinel B8": BASE_DIR / "INPUT" / "Sentinel" / "B8.tiff",
    "Fuel model": BASE_DIR / "INPUT" / "FUELS" / "FUELS.tif",
}
COVERAGE_CACHE_PATH = BASE_DIR / "OUTPUT" / "cache" / "available_data_coverage.geojson"

logger = logging.getLogger("uvicorn.error")
_DEBUG_LOG = BASE_DIR / "OUTPUT" / "storcito_422.log"

print("[STORCITO] api.py loaded with validation logger v2", flush=True)


@app.exception_handler(RequestValidationError)
async def _log_validation_errors(request: Request, exc: RequestValidationError):
    try:
        body_bytes = await request.body()
        body_preview = body_bytes.decode("utf-8", errors="replace")[:4000]
    except Exception as read_exc:
        body_preview = f"<unreadable: {read_exc}>"
    msg = (
        f"422 validation error on {request.method} {request.url.path}\n"
        f"  errors={exc.errors()}\n"
        f"  body={body_preview}"
    )
    try:
        logger.warning(msg)
    except Exception:
        pass
    print(f"[STORCITO 422] {msg}", flush=True)
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a") as fh:
            fh.write(f"--- {datetime.now().isoformat()} ---\n{msg}\n\n")
    except Exception:
        pass
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body_preview": body_preview},
    )


class StaticAOIRequest(BaseModel):
    longitude: float = Field(..., ge=-180, le=180)
    latitude: float = Field(..., ge=-90, le=90)
    date: date
    buffer_m: float = Field(default=3000, gt=0)
    context_buffer_m: float = Field(default=3000, ge=0)


class WildfireCalculationRequest(BaseModel):
    user_id: str
    model_id: str
    session_id: str
    country: str | None = None
    lkr: str | None = None
    callback_url: str | None = None
    start_date: datetime
    end_date: datetime
    resolution: int | None = None
    buffer_distance: float = Field(default=0, ge=0)
    coordinates: dict[str, Any] | None = None
    topology: list[dict[str, Any]] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FWIAreaSummaryRequest(BaseModel):
    date: date
    aoi: dict[str, Any]
    hour_index: int = Field(default=15, ge=0, le=95)


def _to_berlin_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BERLIN_TZ)
    return value.astimezone(BERLIN_TZ)


def _wildfire_target_date(payload: WildfireCalculationRequest) -> date:
    start_local = _to_berlin_time(payload.start_date)
    end_local = _to_berlin_time(payload.end_date)

    if start_local.date() != end_local.date():
        raise ValueError("start_date and end_date must be on the same Europe/Berlin local date.")

    return start_local.date()


def _wildfire_date_range(payload: WildfireCalculationRequest, calculation_mode: str) -> tuple[date | None, date]:
    start_local = _to_berlin_time(payload.start_date)
    end_local = _to_berlin_time(payload.end_date)

    start_day = start_local.date()
    end_day = end_local.date()
    if calculation_mode == "static" and start_day != end_day:
        raise ValueError("start_date and end_date must be on the same Europe/Berlin local date.")
    if start_day > end_day:
        raise ValueError("start_date must be before or equal to end_date.")

    return (start_day if calculation_mode == "dynamic" else None, end_day)


def _unwrap_geojson_geometry(node: Any) -> dict | None:
    """Unwrap a GeoJSON Feature / FeatureCollection down to a geometry object."""
    if not isinstance(node, dict):
        return None
    node_type = node.get("type")
    if node_type == "FeatureCollection":
        for feature in node.get("features", []) or []:
            geom = _unwrap_geojson_geometry(feature)
            if geom is not None:
                return geom
        return None
    if node_type == "Feature":
        return _unwrap_geojson_geometry(node.get("geometry"))
    if "type" in node and "coordinates" in node:
        return node
    return None


def _wildfire_geometry(payload: WildfireCalculationRequest):
    geometry = _unwrap_geojson_geometry(payload.coordinates)
    if geometry is None:
        for item in payload.topology:
            if not isinstance(item, dict):
                continue
            candidate = _unwrap_geojson_geometry(item.get("geometry")) or _unwrap_geojson_geometry(item)
            if candidate is not None:
                geometry = candidate
                break
    if geometry is None:
        raise ValueError("coordinates or topology[0].geometry must contain a GeoJSON geometry.")

    projected = build_geojson_aoi(geometry)
    if payload.buffer_distance > 0:
        projected = projected.buffer(payload.buffer_distance)
    return projected


def _wildfire_context_buffer(payload: WildfireCalculationRequest) -> float:
    raw_value = payload.parameters.get("context_buffer_m", 3000)
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("parameters.context_buffer_m must be numeric when provided.") from exc
    if value < 0:
        raise ValueError("parameters.context_buffer_m must be greater than or equal to zero.")
    return value


def _wildfire_calculation_mode(payload: WildfireCalculationRequest) -> str:
    mode = str(payload.parameters.get("calculation_mode", "static")).strip().lower()
    if mode not in {"static", "dynamic"}:
        raise ValueError("parameters.calculation_mode must be either 'static' or 'dynamic' when provided.")
    return mode


def _wildfire_optional_layers(payload: WildfireCalculationRequest) -> dict[str, bool] | None:
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


def _wildfire_user_input_model_id(payload: WildfireCalculationRequest) -> str:
    """Stable model id used for reusable user inputs.

    Wildfire dispatch uses a unique run id in payload.model_id
    (<model_id>_<timestamp>). Store user inputs under the persistent model id so
    a later run can reuse the same DTM/station CSV from Postgres.
    """
    raw = payload.parameters.get("source_model_id")
    if isinstance(raw, (str, int, float)) and str(raw).strip():
        return str(raw).strip()
    return str(payload.model_id).split("_", 1)[0]


def _source_filename_from_response(resp: httpx.Response, fallback: str) -> str:
    disposition = resp.headers.get("content-disposition", "")
    match = re.search(r'filename="?([^";]+)"?', disposition)
    if match:
        return match.group(1)
    return fallback


def _log_user_input(message: str) -> None:
    print(f"[STORCITO user-inputs] {message}", flush=True)


def _materialize_stored_user_input(
    payload: WildfireCalculationRequest,
    model_id: str,
    kind: str,
    dest_dir: Path,
) -> Path | None:
    dest_name = "dtm.tif" if kind == KIND_DTM else "station_data.csv"
    path = materialize_user_input(payload.user_id, model_id, kind, dest_dir / dest_name)
    if path is not None:
        _log_user_input(f"reused stored {kind} from {USER_INPUT_TABLE} -> {path}")
    return path


def _wildfire_user_inputs(payload: WildfireCalculationRequest, dest_dir: Path) -> dict[str, Path]:
    """Resolve user-supplied inputs from upload URLs and/or Postgres.

    Fresh uploads advertised in parameters.user_inputs are downloaded, normalised
    when needed, stored in Postgres, and used for this run. Missing or failed
    downloads fall back to previously stored inputs for the same user/model.
    """
    model_id = _wildfire_user_input_model_id(payload)
    raw = payload.parameters.get("user_inputs")
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    requested_kinds: set[str] = set()

    if isinstance(raw, dict) and raw:
        _log_user_input(
            "calculation payload includes user input references "
            f"user_id={payload.user_id} model_id={model_id} keys={sorted(raw.keys())}"
        )
    else:
        _log_user_input(
            "calculation payload has no user input references; "
            f"user_id={payload.user_id} model_id={model_id} "
            "using bundled/default DTM and weather inputs unless stored files exist"
        )

    if isinstance(raw, dict):
        for kind, url in raw.items():
            if kind not in {KIND_DTM, KIND_STATION_DATA}:
                _log_user_input(
                    f"ignoring unsupported user input kind={kind} "
                    f"user_id={payload.user_id} model_id={model_id}"
                )
                continue
            requested_kinds.add(kind)
            if not isinstance(url, str) or not url:
                _log_user_input(
                    f"user input reference missing URL kind={kind} "
                    f"user_id={payload.user_id} model_id={model_id}"
                )
                continue
            try:
                _log_user_input(
                    f"upload reference received kind={kind} "
                    f"user_id={payload.user_id} model_id={model_id}"
                )
                upload_path = dest_dir / f"{kind}.upload"
                source_filename = kind
                content_type = None
                with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    source_filename = _source_filename_from_response(resp, source_filename)
                    content_type = resp.headers.get("content-type")
                    expected = resp.headers.get("content-length") or "unknown"
                    _log_user_input(
                        f"downloading {kind} from wildfire backend "
                        f"source_filename={source_filename} content_type={content_type or '-'} "
                        f"expected_bytes={expected}"
                    )
                    downloaded = 0
                    progress_step = 25 * 1024 * 1024 if kind == KIND_DTM else 5 * 1024 * 1024
                    next_progress = progress_step
                    with upload_path.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            if not chunk:
                                continue
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if downloaded >= next_progress:
                                _log_user_input(
                                    f"download progress kind={kind} "
                                    f"downloaded_bytes={downloaded} expected_bytes={expected}"
                                )
                                next_progress += progress_step
                    _log_user_input(
                        f"download complete kind={kind} "
                        f"downloaded_bytes={downloaded} temp_path={upload_path}"
                    )

                if kind == KIND_DTM:
                    target = dest_dir / "dtm.tif"
                    upload_path.replace(target)
                    _log_user_input(
                        f"writing DTM into {USER_INPUT_TABLE} "
                        f"user_id={payload.user_id} model_id={model_id} path={target}"
                    )
                    stored = store_dtm_file(
                        payload.user_id,
                        model_id,
                        target,
                        source_filename=source_filename,
                        content_type=content_type,
                    )
                    _log_user_input(
                        f"DTM row stored in {USER_INPUT_TABLE} "
                        f"user_id={payload.user_id} model_id={model_id} "
                        f"bytes={stored.get('nbytes')} footprint={'yes' if stored.get('footprint') else 'no'}"
                    )
                else:
                    from FR.FWI_excel import convert_station_file_to_csv

                    target = dest_dir / "station_data.csv"
                    _log_user_input(
                        f"normalizing station upload to CSV before database store "
                        f"source_filename={source_filename}"
                    )
                    convert_station_file_to_csv(upload_path, target)
                    upload_path.unlink(missing_ok=True)
                    stored = store_station_csv_file(
                        payload.user_id,
                        model_id,
                        target,
                        source_filename=source_filename,
                        content_type=content_type,
                    )
                    _log_user_input(
                        f"station_data row stored in {USER_INPUT_TABLE} "
                        f"user_id={payload.user_id} model_id={model_id} "
                        f"bytes={stored.get('nbytes')}"
                    )

                paths[kind] = target
                size = target.stat().st_size if target.exists() else 0
                _log_user_input(
                    f"using current {kind} file for run "
                    f"user_id={payload.user_id} model_id={model_id} bytes={size}"
                )
            except Exception as exc:  # noqa: BLE001 - optional; fall back to stored/bundled
                logger.warning("Failed to download/store user input %s from %s: %s", kind, url, exc)
                _log_user_input(f"{kind} download/store failed: {exc}")

    for kind in requested_kinds:
        if kind not in paths:
            stored = _materialize_stored_user_input(payload, model_id, kind, dest_dir)
            if stored is not None:
                paths[kind] = stored

    if paths:
        _log_user_input(f"resolved user inputs for run: {', '.join(sorted(paths))}")
    else:
        _log_user_input(
            "no user inputs resolved for run; "
            f"user_id={payload.user_id} model_id={model_id}"
        )
    return paths


def _public_base_url(request: Request | None) -> str:
    env_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if env_url:
        return env_url
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""



def _raster_coverage_box(path: Path):
    with rasterio.open(path) as src:
        source_crs = src.crs or "EPSG:4326"
        minx, miny, maxx, maxy = transform_bounds(
            source_crs,
            "EPSG:4326",
            src.bounds.left,
            src.bounds.bottom,
            src.bounds.right,
            src.bounds.top,
            densify_pts=21,
        )
    return box(minx, miny, maxx, maxy)


def _coverage_input_signature() -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    for name, raster_path in COVERAGE_INPUT_RASTERS.items():
        stat = raster_path.stat()
        signature.append({
            "name": name,
            "path": str(raster_path.relative_to(BASE_DIR)),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        })

    fwi_dir = BASE_DIR / "INPUT" / "FWI"
    if fwi_dir.exists():
        for fwi_path in sorted(fwi_dir.glob("*.tif")):
            stat = fwi_path.stat()
            signature.append({
                "name": f"FWI:{fwi_path.name}",
                "path": str(fwi_path.relative_to(BASE_DIR)),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            })
    return signature


def _read_cached_coverage(signature: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        cached = json.loads(COVERAGE_CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if cached.get("input_signature") != signature:
        return None
    coverage = cached.get("coverage")
    return coverage if isinstance(coverage, dict) else None


def _write_cached_coverage(signature: list[dict[str, Any]], coverage: dict[str, Any]) -> None:
    try:
        COVERAGE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        COVERAGE_CACHE_PATH.write_text(json.dumps({
            "input_signature": signature,
            "coverage": coverage,
        }, separators=(",", ":")))
    except OSError as exc:
        logger.warning("Unable to write STORCITO coverage cache: %s", exc)


def _raster_has_exact_mask(src: rasterio.io.DatasetReader) -> bool:
    for band_flags in src.mask_flag_enums:
        if MaskFlags.all_valid not in band_flags:
            return True
    return False


def _raster_exact_outer_boundary(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    with rasterio.open(path) as src:
        valid_mask = src.dataset_mask() > 0
        if not valid_mask.any():
            raise RuntimeError(f"Coverage raster has no valid data: {path}")

        best_geometry = None
        best_area = 0.0
        total_area = 0.0
        component_count = 0

        for geometry, value in features.shapes(
            valid_mask.astype("uint8"),
            mask=valid_mask,
            transform=src.transform,
            connectivity=8,
        ):
            if not value:
                continue
            polygon = shape(geometry)
            area = float(polygon.area)
            if area <= 0:
                continue
            component_count += 1
            total_area += area
            if area > best_area:
                best_area = area
                best_geometry = polygon

        if best_geometry is None:
            raise RuntimeError(f"Unable to derive a valid coverage polygon for: {path}")

        # Expose the pixel-edge outer boundary of the main valid-data component.
        # Internal nodata holes are intentionally not sent to the browser because
        # this raster contains tens of thousands of tiny rings and islands.
        exterior_geometry = {
            "type": "Polygon",
            "coordinates": [[list(coord) for coord in best_geometry.exterior.coords]],
        }
        wgs84_geometry = transform_geom(
            src.crs or "EPSG:4326",
            "EPSG:4326",
            exterior_geometry,
            precision=6,
        )

    return wgs84_geometry, {
        "component_count": component_count,
        "selected_component_area_m2": best_area,
        "valid_component_area_m2": total_area,
        "selected_component_area_fraction": best_area / total_area if total_area else None,
        "internal_holes_omitted": True,
    }


@lru_cache(maxsize=1)
def _available_data_coverage_geojson() -> dict[str, Any]:
    rasters: list[dict[str, Any]] = []
    coverage_box = None
    exact_mask_rasters: list[Path] = []

    for name, raster_path in COVERAGE_INPUT_RASTERS.items():
        if not raster_path.exists():
            raise FileNotFoundError(f"Required coverage raster is missing: {raster_path}")
        raster_box = _raster_coverage_box(raster_path)
        coverage_box = raster_box if coverage_box is None else coverage_box.intersection(raster_box)

        with rasterio.open(raster_path) as src:
            has_exact_mask = _raster_has_exact_mask(src)
        if has_exact_mask:
            exact_mask_rasters.append(raster_path)

        rasters.append({
            "name": name,
            "path": str(raster_path.relative_to(BASE_DIR)),
            "bbox": [float(value) for value in raster_box.bounds],
            "has_exact_mask": has_exact_mask,
        })

    if coverage_box is None or coverage_box.is_empty:
        raise RuntimeError("Unable to derive a non-empty wildfire data coverage boundary.")

    signature = _coverage_input_signature()
    cached = _read_cached_coverage(signature)
    if cached is not None:
        return cached

    mask_metadata: dict[str, Any] = {}
    if exact_mask_rasters:
        if len(exact_mask_rasters) > 1:
            raise RuntimeError(
                "Exact coverage boundary currently supports one masked core raster; "
                f"found {len(exact_mask_rasters)}."
            )
        coverage_geometry, mask_metadata = _raster_exact_outer_boundary(exact_mask_rasters[0])
        coverage_method = "exact_outer_boundary_from_valid_raster_mask"
    else:
        coverage_geometry = mapping(coverage_box)
        coverage_method = "intersection_of_core_input_raster_bounds"

    dates = [day.isoformat() for day in available_fwi_dates_db()]
    bounds = [float(value) for value in shape(coverage_geometry).bounds]
    coverage = {
        "type": "FeatureCollection",
        "bbox": bounds,
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "Available wildfire data area",
                    "source": "storcito",
                    "coverage_method": coverage_method,
                    "date_from": dates[0] if dates else None,
                    "date_to": dates[-1] if dates else None,
                    "available_dates": dates,
                    "input_rasters": rasters,
                    **mask_metadata,
                },
                "geometry": coverage_geometry,
            }
        ],
    }
    _write_cached_coverage(signature, coverage)
    return coverage

def _job_relative_path(file_path: str, root: Path = AOI_OUTPUT_ROOT) -> str | None:
    try:
        resolved = Path(file_path).resolve()
        return resolved.relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def _augment_with_urls(
    outputs: dict[str, str],
    request: Request | None,
    *,
    root: Path = AOI_OUTPUT_ROOT,
    url_prefix: str = "results",
) -> dict[str, Any]:
    base_url = _public_base_url(request)
    urls: dict[str, str] = {}
    for key, value in outputs.items():
        if not isinstance(value, str):
            continue
        rel = _job_relative_path(value, root)
        if rel is None:
            continue
        urls[key] = f"{base_url}/{url_prefix}/{rel}" if base_url else f"/{url_prefix}/{rel}"
    enriched: dict[str, Any] = dict(outputs)
    if urls:
        enriched["urls"] = urls
        if "final_map" in urls:
            enriched["download_url"] = urls["final_map"]
    return enriched


def _zip_job_outputs(job_dir: Path) -> Path:
    """Bundle all final result files into a single zip the wildfire callback can ingest."""
    zip_path = job_dir / f"{job_dir.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(job_dir.rglob("*")):
            if not file.is_file() or file == zip_path:
                continue
            zf.write(file, file.relative_to(job_dir).as_posix())
    return zip_path


def _post_result_callback(callback_url: str, zip_path: Path, session_id: str | None) -> dict[str, Any]:
    """POST the result zip to the wildfire callback (multipart/form-data, field 'file')."""
    timeout = httpx.Timeout(connect=15.0, read=600.0, write=600.0, pool=15.0)
    with zip_path.open("rb") as fh:
        files = {"file": (zip_path.name, fh, "application/zip")}
        data: dict[str, str] = {}
        if session_id:
            data["session_id"] = session_id
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(callback_url, files=files, data=data)
    info = {
        "callback_url": callback_url,
        "status_code": response.status_code,
        "body": response.text[:2000],
    }
    (zip_path.parent / "callback.log").write_text(
        f"status={info['status_code']}\nbody={info['body']}\n"
    )
    response.raise_for_status()
    return info


def _run_wildfire_payload(payload: WildfireCalculationRequest, request: Request | None = None):
    calculation_mode = _wildfire_calculation_mode(payload)
    start_date, target_date = _wildfire_date_range(payload, calculation_mode)
    output_aoi = _wildfire_geometry(payload)
    optional_layers = _wildfire_optional_layers(payload)
    user_inputs = _wildfire_user_inputs(payload, AOI_OUTPUT_ROOT / "_user_inputs" / payload.model_id)
    outputs = run_static_aoi_for_geometry(
        output_aoi,
        target_date,
        start_date=start_date,
        context_buffer_m=_wildfire_context_buffer(payload),
        optional_layers=optional_layers,
        dtm_path=user_inputs.get("dtm"),
        station_data_path=user_inputs.get("station_data"),
        request_metadata={
            "request_type": "wildfire_payload",
            "user_id": payload.user_id,
            "model_id": payload.model_id,
            "session_id": payload.session_id,
            "country": payload.country,
            "lkr": payload.lkr,
            "callback_url": payload.callback_url,
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
            "buffer_distance": payload.buffer_distance,
            "resolution": payload.resolution,
            "calculation_mode": calculation_mode,
            "optional_layers": optional_layers or {},
        },
    )
    weather_summary_path = _write_model_weather_summary(
        Path(outputs["job_dir"]),
        payload=payload,
        target_date=target_date,
        output_aoi=output_aoi,
        start_date=start_date,
        optional_layers=optional_layers,
        user_inputs=user_inputs,
    )
    if weather_summary_path is not None:
        outputs["weather_summary"] = str(weather_summary_path)

    enriched_outputs = _augment_with_urls(outputs, request)

    db_info, db_error = _store_results_to_db(
        outputs,
        metadata={
            "job_id": outputs.get("request_id"),
            "session_id": payload.session_id,
            "user_id": payload.user_id,
            "model_id": payload.model_id,
            "engine": "static_aoi",
            "calculation_mode": calculation_mode,
            "request_type": "wildfire_payload",
            "target_date": target_date.isoformat(),
            "country": payload.country,
            "lkr": payload.lkr,
        },
        aoi_wgs84=reproject_geometry(output_aoi, DEFAULT_PROJECTED_CRS, "EPSG:4326"),
    )

    callback_info: dict[str, Any] | None = None
    callback_error: str | None = None
    if payload.callback_url:
        job_dir_str = outputs.get("job_dir")
        if job_dir_str:
            try:
                zip_path = _zip_job_outputs(Path(job_dir_str))
                enriched_outputs["result_zip"] = str(zip_path)
                rel = _job_relative_path(str(zip_path))
                if rel is not None:
                    base_url = _public_base_url(request)
                    zip_url = f"{base_url}/results/{rel}" if base_url else f"/results/{rel}"
                    enriched_outputs.setdefault("urls", {})["result_zip"] = zip_url
                callback_info = _post_result_callback(
                    payload.callback_url,
                    zip_path,
                    payload.session_id,
                )
            except Exception as exc:
                callback_error = str(exc)

    response: dict[str, Any] = {
        "status": "success",
        "session_id": payload.session_id,
        "callback_url": payload.callback_url,
        "outputs": enriched_outputs,
    }
    if callback_info is not None:
        response["callback"] = callback_info
    if callback_error is not None:
        response["callback_error"] = callback_error
    if db_info is not None:
        response["db_store"] = db_info
    if db_error is not None:
        response["db_store_error"] = db_error
    return response


def _store_results_to_db(
    outputs: dict[str, Any],
    *,
    metadata: dict[str, Any],
    aoi_wgs84=None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Store the finished result maps into PostGIS (best-effort).

    Controlled by STORCITO_STORE_RESULTS (default on). Never raises: a storage
    failure is reported back to the caller as an error string so the simulation
    response still succeeds, mirroring the callback-error handling.
    """
    flag = os.getenv("STORCITO_STORE_RESULTS", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None, None
    try:
        from FR.db_store import store_result_maps

        aoi_geojson = json.dumps(mapping(aoi_wgs84)) if aoi_wgs84 is not None else None
        info = store_result_maps(outputs, metadata=metadata, aoi_geojson=aoi_geojson)
        return info, None
    except Exception as exc:  # noqa: BLE001 - report and keep the result
        msg = f"{type(exc).__name__}: {exc}"
        logger.warning("STORCITO result DB store failed: %s", msg)
        print(f"[STORCITO DB] store failed: {msg}", flush=True)
        return None, str(exc)


def _raise_aoi_http_error(exc: Exception) -> None:
    detail = str(exc)
    try:
        msg = (
            f"{datetime.now().isoformat()} {type(exc).__name__}: {detail}"
        )
        print(f"[STORCITO ERR] {msg}", flush=True)
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a") as fh:
            fh.write(f"--- {datetime.now().isoformat()} ---\n{msg}\n")
            import traceback as _tb
            _tb.print_exception(type(exc), exc, exc.__traceback__, file=fh)
            fh.write("\n")
    except Exception:
        pass
    if isinstance(exc, ValueError):
        if detail.startswith("Dynamic wildfire payloads are not supported"):
            raise HTTPException(status_code=501, detail=detail) from exc
        raise HTTPException(status_code=422, detail=detail) from exc
    raise HTTPException(status_code=500, detail=detail) from exc


def _create_job_dir(payload: WildfireCalculationRequest) -> tuple[str, Path]:
    """Build a per-request job directory named from the request IDs."""
    raw = f"{payload.user_id}_{payload.model_id}_{payload.session_id}"
    job_id = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("_")[:120] or "job"
    job_dir = JOBS_OUTPUT_ROOT / job_id
    if job_dir.exists():
        # Avoid clobbering a previous run for the same IDs.
        job_id = f"{job_id}_{datetime.now(BERLIN_TZ).strftime('%Y%m%dT%H%M%S')}"
        job_dir = JOBS_OUTPUT_ROOT / job_id
    resolved = job_dir.resolve()
    if resolved != JOBS_OUTPUT_ROOT and not str(resolved).startswith(str(JOBS_OUTPUT_ROOT) + os.sep):
        raise ValueError("Invalid job identifier derived from request IDs.")
    return job_id, resolved


def _wildfire_clip_geometry_wgs84(payload: WildfireCalculationRequest):
    """Boundary (WGS84) used to clip the datasets while exporting from the DB.

    Raises ValueError (-> 422) when the request carries no boundary, which is
    required for the whole-region static/dynamic engines.
    """
    projected = _wildfire_geometry(payload)  # EPSG:32629, includes buffer_distance
    context_buffer_m = _wildfire_context_buffer(payload)
    # Add the engines' internal 3000 m crop margin so the reconstructed data
    # fully covers the area the engine later crops to.
    processing = projected.buffer(context_buffer_m + 3000)
    return reproject_geometry(processing, DEFAULT_PROJECTED_CRS, "EPSG:4326")


def _run_engine_job(
    payload: WildfireCalculationRequest,
    engine: str,
    request: Request | None,
) -> dict[str, Any]:
    """Reconstruct inputs from PostGIS into a per-request folder and run an engine."""
    cfg = ENGINE_SCRIPTS[engine]
    target_date = _wildfire_target_date(payload)
    output_aoi = _wildfire_geometry(payload)
    clip_geom = _wildfire_clip_geometry_wgs84(payload)

    job_id, job_dir = _create_job_dir(payload)
    input_dir = job_dir / "INPUT"
    output_dir = job_dir / "OUTPUT"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    reconstruction = reconstruct_inputs(
        input_dir,
        engine=engine,
        target_date=target_date,
        clip_geom=clip_geom,
        clip_geom_crs="EPSG:4326",
    )

    env = {
        **os.environ,
        "FFRM_BASE_DIR": str(job_dir),
        "FFRM_OUTPUT_DIR": str(output_dir),
        "MPLBACKEND": "Agg",
        **cfg["run_flags"],
    }
    proc = subprocess.run(
        ["python", cfg["script"]],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    (output_dir / "engine.log").write_text(
        f"returncode={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{engine} engine failed (see engine.log):\n{proc.stderr[-2000:]}")

    result_map = output_dir / cfg["result"]
    if not result_map.is_file():
        raise RuntimeError(
            f"{engine} engine finished but {cfg['result']} was not produced.\n{proc.stdout[-1000:]}"
        )

    continuous = "mapa_final_dinamico.tif" if engine == "dynamic" else "mapa_final.tif"
    outputs = {
        "final_map": str(result_map),
        "continuous_map": str(output_dir / continuous),
        "job_dir": str(job_dir),
    }
    weather_summary_path = _write_model_weather_summary(
        output_dir,
        payload=payload,
        target_date=target_date,
        output_aoi=output_aoi,
        start_date=None,
        optional_layers=_wildfire_optional_layers(payload),
    )
    if weather_summary_path is not None:
        outputs["weather_summary"] = str(weather_summary_path)
    enriched = _augment_with_urls(outputs, request, root=JOBS_OUTPUT_ROOT, url_prefix="jobs")

    response: dict[str, Any] = {
        "status": "success",
        "engine": engine,
        "job_id": job_id,
        "session_id": payload.session_id,
        "target_date": target_date.isoformat(),
        "reconstruction": reconstruction,
        "outputs": enriched,
    }

    db_info, db_error = _store_results_to_db(
        outputs,
        metadata={
            "job_id": job_id,
            "session_id": payload.session_id,
            "user_id": payload.user_id,
            "model_id": payload.model_id,
            "engine": engine,
            "calculation_mode": engine,
            "request_type": "engine_job",
            "target_date": target_date.isoformat(),
            "country": payload.country,
            "lkr": payload.lkr,
        },
        aoi_wgs84=reproject_geometry(
            output_aoi, DEFAULT_PROJECTED_CRS, "EPSG:4326"
        ),
    )
    if db_info is not None:
        response["db_store"] = db_info
    if db_error is not None:
        response["db_store_error"] = db_error

    if payload.callback_url:
        try:
            zip_path = _zip_job_outputs(output_dir)
            response["result_zip"] = str(zip_path)
            response["callback"] = _post_result_callback(
                payload.callback_url, zip_path, payload.session_id
            )
        except Exception as exc:  # noqa: BLE001 - report callback failure, keep result
            response["callback_error"] = str(exc)

    return response


@app.get("/")
def read_root():
    return {"message": "Welcome to STORCITO API. Use POST /run-dynamic or POST /run-static to trigger jobs."}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    return {"status": "ok"}


@app.post("/run-dynamic")
def run_dynamic(payload: WildfireCalculationRequest, request: Request):
    """
    Reconstruct inputs from PostGIS (clipped to the request boundary) and run the
    dynamic risk engine (FFRM_dinamic.py).
    """
    try:
        return _run_engine_job(payload, "dynamic", request)
    except Exception as e:
        _raise_aoi_http_error(e)


@app.post("/run-static")
def run_static(payload: WildfireCalculationRequest, request: Request):
    """
    Reconstruct inputs from PostGIS (clipped to the request boundary) and run the
    whole-region static risk engine (FFRM_static.py).
    """
    try:
        return _run_engine_job(payload, "static", request)
    except Exception as e:
        _raise_aoi_http_error(e)


@app.get("/available-static-dates")
def available_static_dates():
    # One representative day per year: the FWI day with the highest air
    # temperature in that year, from the `fwi_files` table.
    dates = highest_temperature_fwi_dates_db()
    return {"dates": [day.isoformat() for day in dates]}


@app.get("/available-dynamic-dates")
def available_dynamic_dates():
    dates = available_fwi_dates_db()
    return {"dates": [day.isoformat() for day in dates]}


@app.get("/available-data-coverage")
def available_data_coverage():
    try:
        return _available_data_coverage_geojson()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/run-static-aoi")
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
        )
        result: dict[str, Any] = {
            "status": "success",
            "outputs": _augment_with_urls(outputs, request),
        }
        db_info, db_error = _store_results_to_db(
            outputs,
            metadata={
                "job_id": outputs.get("request_id"),
                "user_id": None,
                "model_id": None,
                "session_id": None,
                "engine": "static_aoi",
                "calculation_mode": "static",
                "request_type": "point",
                "target_date": payload.date.isoformat(),
                "longitude": payload.longitude,
                "latitude": payload.latitude,
            },
        )
        if db_info is not None:
            result["db_store"] = db_info
        if db_error is not None:
            result["db_store_error"] = db_error
        return result
    except Exception as e:
        _raise_aoi_http_error(e)


@app.post("/run-static-aoi-wildfire")
def run_static_aoi_wildfire_request(payload: WildfireCalculationRequest, request: Request):
    """
    Runs the static workflow from the generic wildfire calculation payload.
    """
    try:
        return _run_wildfire_payload(payload, request)
    except Exception as e:
        _raise_aoi_http_error(e)


@app.post("/calliope/start")
def calliope_start(payload: WildfireCalculationRequest, request: Request):
    """
    Wildfire-compatible default webservice endpoint.
    """
    try:
        return _run_wildfire_payload(payload, request)
    except Exception as e:
        _raise_aoi_http_error(e)


@app.get("/results/{request_id}/{file_path:path}")
def download_result(request_id: str, file_path: str):
    """
    Serve a file from a static-AOI job output directory.
    """
    try:
        target = (AOI_OUTPUT_ROOT / request_id / file_path).resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Invalid result path.") from exc

    job_root = (AOI_OUTPUT_ROOT / request_id).resolve()
    if not str(target).startswith(str(job_root) + os.sep) and target != job_root:
        raise HTTPException(status_code=400, detail="Invalid result path.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Result not found.")

    media_type = "image/tiff" if target.suffix.lower() in {".tif", ".tiff"} else None
    return FileResponse(target, media_type=media_type, filename=target.name)


@app.get("/jobs/{job_id}/{file_path:path}")
def download_job_result(job_id: str, file_path: str):
    """Serve a file from a per-request engine job directory (OUTPUT/jobs)."""
    try:
        target = (JOBS_OUTPUT_ROOT / job_id / file_path).resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Invalid result path.") from exc

    job_root = (JOBS_OUTPUT_ROOT / job_id).resolve()
    if not str(target).startswith(str(job_root) + os.sep) and target != job_root:
        raise HTTPException(status_code=400, detail="Invalid result path.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Result not found.")

    media_type = "image/tiff" if target.suffix.lower() in {".tif", ".tiff"} else None
    return FileResponse(target, media_type=media_type, filename=target.name)


def _nanmean_float(values) -> float | None:
    import numpy as np

    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None
    return float(np.nanmean(arr))


def _circular_mean_degrees(values) -> float | None:
    import numpy as np

    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None
    radians = np.deg2rad(arr)
    sin_mean = float(np.nanmean(np.sin(radians)))
    cos_mean = float(np.nanmean(np.cos(radians)))
    if sin_mean == 0.0 and cos_mean == 0.0:
        return None
    return float((np.degrees(np.arctan2(sin_mean, cos_mean)) + 360.0) % 360.0)


def _fwi_area_grid_indices(lon_grid, lat_grid, aoi_wgs84):
    import numpy as np
    from shapely.geometry import Point

    minx, miny, maxx, maxy = aoi_wgs84.bounds
    candidate_mask = (
        (lon_grid >= minx)
        & (lon_grid <= maxx)
        & (lat_grid >= miny)
        & (lat_grid <= maxy)
    )
    candidate_y, candidate_x = np.where(candidate_mask)

    selected_y: list[int] = []
    selected_x: list[int] = []
    for grid_y, grid_x in zip(candidate_y, candidate_x):
        point = Point(float(lon_grid[grid_y, grid_x]), float(lat_grid[grid_y, grid_x]))
        if aoi_wgs84.covers(point):
            selected_y.append(int(grid_y))
            selected_x.append(int(grid_x))

    reference_point = aoi_wgs84.representative_point()
    if selected_y:
        return (
            np.asarray(selected_y, dtype=int),
            np.asarray(selected_x, dtype=int),
            {
                "method": "aoi_grid_mean",
                "sample_lon": float(reference_point.x),
                "sample_lat": float(reference_point.y),
                "sample_count": len(selected_y),
            },
        )

    grid_y, grid_x, grid_lon, grid_lat = _nearest_fwi_grid_point(
        lon_grid,
        lat_grid,
        float(reference_point.x),
        float(reference_point.y),
    )
    return (
        np.asarray([grid_y], dtype=int),
        np.asarray([grid_x], dtype=int),
        {
            "method": "nearest_grid_fallback",
            "sample_lon": float(reference_point.x),
            "sample_lat": float(reference_point.y),
            "sample_count": 1,
            "grid_lon": grid_lon,
            "grid_lat": grid_lat,
        },
    )


def _fwi_slice(cur, fdate, hour_index: int) -> dict[str, Any] | None:
    """Per-day extract of the FWI NetCDF, cached in the small fwi_slices table
    so weather sampling never re-reads the ~240 MB blobs after the first pass."""
    import io

    import numpy as np
    import netCDF4 as nc

    cur.execute(
        """CREATE TABLE IF NOT EXISTS fwi_slices (
               fdate date NOT NULL,
               hour_index int NOT NULL,
               data bytea NOT NULL,
               PRIMARY KEY (fdate, hour_index)
           )"""
    )
    cur.execute("SELECT data FROM fwi_slices WHERE fdate = %s AND hour_index = %s", (fdate, hour_index))
    row = cur.fetchone()
    if row is not None:
        loaded = np.load(io.BytesIO(bytes(row[0])), allow_pickle=False)
        return {key: loaded[key] for key in loaded.files}

    cur.execute("SELECT data FROM fwi_files WHERE fdate = %s ORDER BY filename LIMIT 1", (fdate,))
    blob = cur.fetchone()
    if blob is None:
        return None
    with nc.Dataset("fwi_from_db.nc", memory=bytes(blob[0])) as dataset:
        n_hours = int(dataset["time"].shape[0])
        if hour_index < 0 or hour_index >= n_hours:
            raise ValueError(f"hour_index must be between 0 and {n_hours - 1} for {fdate}.")
        day_hours = min(n_hours, 24)
        payload = {
            "lon": np.asarray(dataset["lon"][:], dtype=np.float32),
            "lat": np.asarray(dataset["lat"][:], dtype=np.float32),
            "temp": np.asarray(dataset["temp"][hour_index], dtype=np.float32),
            "rh": np.asarray(dataset["rh"][hour_index], dtype=np.float32),
            "mod": np.asarray(dataset["mod"][hour_index], dtype=np.float32),
            "dir": np.asarray(dataset["dir"][hour_index], dtype=np.float32),
            "prec_day": np.asarray(dataset["prec"][:day_hours], dtype=np.float32),
            "month": np.int32(nc.num2date(dataset["time"][0], dataset["time"].units).month),
            "time_str": np.str_(str(nc.num2date(dataset["time"][hour_index], dataset["time"].units))),
        }
    buf = io.BytesIO()
    np.savez_compressed(buf, **payload)
    cur.execute(
        "INSERT INTO fwi_slices (fdate, hour_index, data) VALUES (%s, %s, %s) "
        "ON CONFLICT (fdate, hour_index) DO NOTHING",
        (fdate, hour_index, buf.getvalue()),
    )
    return payload


def _sample_fwi_area_from_db(
    *,
    target_date: date,
    aoi_wgs84,
    hour_index: int = 15,
) -> dict[str, Any]:
    """Summarise WRF/FWI weather over the model AOI from cached daily slices."""
    import numpy as np
    import FR.rutinas.FWI_Equations as Fwi
    import FR.FWI as FwiModule
    from FR.db_reconstruct import _pg_connect

    f0 = None
    dmc0 = None
    dc0 = None
    sample: dict[str, Any] | None = None
    rows_seen = 0
    grid_y_idx = None
    grid_x_idx = None
    area_info: dict[str, Any] | None = None
    prev_rain_tail = None

    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (fdate) fdate, filename FROM fwi_files "
            "WHERE fdate IS NOT NULL AND fdate <= %s ORDER BY fdate, filename",
            (target_date,),
        )
        days = cur.fetchall()
        for fdate, filename in days:
            slice_data = _fwi_slice(cur, fdate, hour_index)
            if slice_data is None:
                continue
            rows_seen += 1

            if grid_y_idx is None or grid_x_idx is None:
                grid_y_idx, grid_x_idx, area_info = _fwi_area_grid_indices(
                    np.asarray(slice_data["lon"], dtype=float),
                    np.asarray(slice_data["lat"], dtype=float),
                    aoi_wgs84,
                )

            month = int(slice_data["month"])
            temperature_k = np.asarray(slice_data["temp"], dtype=float)[grid_y_idx, grid_x_idx]
            temperature_c = temperature_k - 273.15
            relative_humidity = np.asarray(slice_data["rh"], dtype=float)[grid_y_idx, grid_x_idx]
            wind_speed_mps = np.asarray(slice_data["mod"], dtype=float)[grid_y_idx, grid_x_idx]
            wind_direction_deg = np.asarray(slice_data["dir"], dtype=float)[grid_y_idx, grid_x_idx]
            # 24 h rain accumulation up to the assessment hour.
            prec_day = np.asarray(slice_data["prec_day"], dtype=float)[:, grid_y_idx, grid_x_idx]
            day_hours = prec_day.shape[0]
            precipitation_mm = np.nansum(prec_day[: hour_index + 1], axis=0)
            if prev_rain_tail is not None:
                precipitation_mm = precipitation_mm + prev_rain_tail
            prev_rain_tail = np.nansum(prec_day[hour_index + 1 : day_hours], axis=0)

            if f0 is None:
                init_f, init_p, init_d = FwiModule.fwi_init_codes()
                f0 = np.full(relative_humidity.shape, init_f, dtype=float)
                dmc0 = np.full(relative_humidity.shape, init_p, dtype=float)
                dc0 = np.full(relative_humidity.shape, init_d, dtype=float)

            wind_kmh = wind_speed_mps * 3.6
            # FWI equations expect RH in percent; the NetCDF stores a fraction.
            rh_pct = FwiModule.rh_to_percent(relative_humidity)
            ffmc = Fwi.ffmc(temperature_c, rh_pct, wind_kmh, precipitation_mm, f0)
            dmc = Fwi.dmc(temperature_c, rh_pct, precipitation_mm, dmc0, month)
            dc = Fwi.dc(temperature_c, precipitation_mm, month, dc0)
            isi = Fwi.isi(wind_kmh, ffmc)
            bui = Fwi.bui(dmc, dc)
            fwi = Fwi.fwi(isi, bui)

            f0, dmc0, dc0 = ffmc, dmc, dc

            if fdate == target_date:
                sample = {
                    "date": fdate.isoformat(),
                    "filename": filename,
                    "time": str(slice_data["time_str"]),
                    "hour_index": hour_index,
                    "method": area_info.get("method") if area_info else "aoi_grid_mean",
                    "sample_count": area_info.get("sample_count") if area_info else int(relative_humidity.size),
                    "sample_lon": area_info.get("sample_lon") if area_info else None,
                    "sample_lat": area_info.get("sample_lat") if area_info else None,
                    "grid_lon": area_info.get("grid_lon") if area_info else None,
                    "grid_lat": area_info.get("grid_lat") if area_info else None,
                    "temperature_k": _nanmean_float(temperature_k),
                    "temperature_c": _nanmean_float(temperature_c),
                    "relative_humidity": _nanmean_float(relative_humidity),
                    "relative_humidity_pct": _nanmean_float(relative_humidity * 100.0),
                    "wind_speed_mps": _nanmean_float(wind_speed_mps),
                    "wind_speed_kmh": _nanmean_float(wind_kmh),
                    "wind_direction_deg": _circular_mean_degrees(wind_direction_deg),
                    "precipitation_mm": _nanmean_float(precipitation_mm),
                    "ffmc": _nanmean_float(ffmc),
                    "dmc": _nanmean_float(dmc),
                    "dc": _nanmean_float(dc),
                    "isi": _nanmean_float(isi),
                    "bui": _nanmean_float(bui),
                    "fwi": _nanmean_float(fwi),
                    "runup_days": rows_seen,
                    "source": "database:fwi_slices",
                }
                break

    if sample is None:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT min(fdate), max(fdate) FROM fwi_files")
            lo, hi = cur.fetchone()
        raise ValueError(f"FWI date {target_date.isoformat()} is not available. Available range: {lo} .. {hi}.")

    return sample


def _weather_layer_enabled(optional_layers: dict[str, bool] | None) -> bool:
    if optional_layers is None:
        return True
    return bool(optional_layers.get("weather_overlay", False))


def _write_model_weather_summary(
    job_dir: Path,
    *,
    payload: WildfireCalculationRequest,
    target_date: date,
    output_aoi,
    start_date: date | None,
    optional_layers: dict[str, bool] | None,
    user_inputs: dict[str, Path] | None = None,
) -> Path | None:
    """Write model-scoped fire-weather metadata into the result package."""
    if not _weather_layer_enabled(optional_layers):
        return None
    if user_inputs and user_inputs.get("station_data"):
        # Uploaded station data replaces DB weather for FWI; avoid labeling DB
        # weather as the model's actual weather input.
        return None

    try:
        aoi_wgs84 = reproject_geometry(output_aoi, DEFAULT_PROJECTED_CRS, "EPSG:4326")
        summary = _sample_fwi_area_from_db(target_date=target_date, aoi_wgs84=aoi_wgs84)
        summary.update(
            {
                "summary_type": "model_fire_weather",
                "model_id": payload.model_id,
                "source_model_id": str(payload.parameters.get("source_model_id", payload.model_id)),
                "session_id": payload.session_id,
                "calculation_mode": _wildfire_calculation_mode(payload),
                "fwi_start_date": start_date.isoformat() if start_date else None,
                "fwi_end_date": target_date.isoformat(),
                "included_in_risk": True,
            }
        )
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / "weather_summary.json"
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return path
    except Exception as exc:  # noqa: BLE001 - result should still complete
        logger.warning("STORCITO weather summary failed: %s", exc)
        try:
            (job_dir / "weather_summary_error.json").write_text(
                json.dumps({"error": str(exc), "source": "database:fwi_files.data"}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return None


def _nearest_fwi_grid_point(lon_grid, lat_grid, lon: float, lat: float) -> tuple[int, int, float, float]:
    import numpy as np

    distance = (lon_grid - lon) ** 2 + (lat_grid - lat) ** 2
    grid_y, grid_x = np.unravel_index(np.nanargmin(distance), distance.shape)
    return int(grid_y), int(grid_x), float(lon_grid[grid_y, grid_x]), float(lat_grid[grid_y, grid_x])


def _fwi_scalar(value) -> float:
    import numpy as np

    return float(np.asarray(value).squeeze())


def _sample_fwi_point_from_db(
    *,
    target_date: date,
    lon: float,
    lat: float,
    hour_index: int = 15,
    include_runup: bool = True,
) -> dict[str, Any]:
    """Sample one WRF/FWI point from the cached daily slices (see _fwi_slice)."""
    import numpy as np
    import FR.rutinas.FWI_Equations as Fwi
    import FR.FWI as FwiModule
    from FR.db_reconstruct import _pg_connect

    query = (
        "SELECT DISTINCT ON (fdate) fdate, filename FROM fwi_files "
        "WHERE fdate IS NOT NULL AND fdate <= %s ORDER BY fdate, filename"
        if include_runup
        else "SELECT DISTINCT ON (fdate) fdate, filename FROM fwi_files WHERE fdate = %s ORDER BY fdate, filename"
    )

    init_f, init_p, init_d = FwiModule.fwi_init_codes()
    f0 = np.array([init_f], dtype=float)
    dmc0 = np.array([init_p], dtype=float)
    dc0 = np.array([init_d], dtype=float)
    sample: dict[str, Any] | None = None
    rows_seen = 0
    prev_rain_tail = 0.0
    grid = None

    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(query, (target_date,))
        days = cur.fetchall()
        for fdate, filename in days:
            slice_data = _fwi_slice(cur, fdate, hour_index)
            if slice_data is None:
                continue
            rows_seen += 1

            if grid is None:
                grid = _nearest_fwi_grid_point(
                    np.asarray(slice_data["lon"], dtype=float),
                    np.asarray(slice_data["lat"], dtype=float),
                    lon,
                    lat,
                )
            grid_y, grid_x, grid_lon, grid_lat = grid

            month = int(slice_data["month"])
            temperature_k = float(slice_data["temp"][grid_y, grid_x])
            temperature_c = temperature_k - 273.15
            relative_humidity = float(slice_data["rh"][grid_y, grid_x])
            wind_speed_mps = float(slice_data["mod"][grid_y, grid_x])
            wind_direction_deg = float(slice_data["dir"][grid_y, grid_x])
            # 24 h rain accumulation up to the assessment hour.
            prec_day = np.asarray(slice_data["prec_day"], dtype=float)[:, grid_y, grid_x]
            day_hours = prec_day.shape[0]
            precipitation_mm = float(np.nansum(prec_day[: hour_index + 1])) + prev_rain_tail
            prev_rain_tail = float(np.nansum(prec_day[hour_index + 1 : day_hours]))

            temperature_arr = np.array([temperature_c], dtype=float)
            # FWI equations expect RH in percent; the NetCDF stores a fraction.
            humidity_arr = FwiModule.rh_to_percent(np.array([relative_humidity], dtype=float))
            wind_arr = np.array([wind_speed_mps * 3.6], dtype=float)
            rain_arr = np.array([precipitation_mm], dtype=float)

            ffmc = Fwi.ffmc(temperature_arr, humidity_arr, wind_arr, rain_arr, f0)
            dmc = Fwi.dmc(temperature_arr, humidity_arr, rain_arr, dmc0, month)
            dc = Fwi.dc(temperature_arr, rain_arr, month, dc0)
            isi = Fwi.isi(wind_arr, ffmc)
            bui = Fwi.bui(dmc, dc)
            fwi = Fwi.fwi(isi, bui)

            f0, dmc0, dc0 = ffmc, dmc, dc

            if fdate == target_date:
                sample = {
                    "date": fdate.isoformat(),
                    "filename": filename,
                    "time": str(slice_data["time_str"]),
                    "hour_index": hour_index,
                    "requested_lon": lon,
                    "requested_lat": lat,
                    "grid_lon": grid_lon,
                    "grid_lat": grid_lat,
                    "grid_x": grid_x,
                    "grid_y": grid_y,
                    "temperature_k": temperature_k,
                    "temperature_c": temperature_c,
                    "relative_humidity": relative_humidity,
                    "relative_humidity_pct": relative_humidity * 100.0,
                    "wind_speed_mps": wind_speed_mps,
                    "wind_speed_kmh": wind_speed_mps * 3.6,
                    "wind_direction_deg": wind_direction_deg,
                    "precipitation_mm": precipitation_mm,
                    "ffmc": _fwi_scalar(ffmc),
                    "dmc": _fwi_scalar(dmc),
                    "dc": _fwi_scalar(dc),
                    "isi": _fwi_scalar(isi),
                    "bui": _fwi_scalar(bui),
                    "fwi": _fwi_scalar(fwi),
                    "runup_days": rows_seen if include_runup else 1,
                    "source": "database:fwi_slices",
                }
                break

    if sample is None:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT min(fdate), max(fdate) FROM fwi_files")
            lo, hi = cur.fetchone()
        raise ValueError(f"FWI date {target_date.isoformat()} is not available. Available range: {lo} .. {hi}.")

    return sample


def _raise_db_http_error(exc: Exception) -> None:
    """Map db_catalog errors to HTTP responses (no db_catalog import needed here)."""
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


@app.post("/fwi/area")
def fwi_area_summary(payload: FWIAreaSummaryRequest):
    """Return DB-backed WRF/FWI weather and moisture-code values over an AOI."""
    try:
        geometry = _unwrap_geojson_geometry(payload.aoi)
        if geometry is None:
            raise ValueError("aoi must be a GeoJSON geometry, Feature, or FeatureCollection.")
        aoi_wgs84 = shape(geometry)
        if aoi_wgs84.is_empty:
            raise ValueError("aoi geometry is empty.")
        return _sample_fwi_area_from_db(
            target_date=payload.date,
            aoi_wgs84=aoi_wgs84,
            hour_index=payload.hour_index,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_db_http_error(exc)


@app.get("/fwi/point")
def fwi_point_sample(
    fdate: date = Query(..., alias="date"),
    lon: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
    hour_index: int = Query(default=15, ge=0, le=95),
    include_runup: bool = Query(default=True),
):
    """Return DB-backed WRF/FWI weather and moisture-code values at one point."""
    try:
        return _sample_fwi_point_from_db(
            target_date=fdate,
            lon=lon,
            lat=lat,
            hour_index=hour_index,
            include_runup=include_runup,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_db_http_error(exc)


@app.get("/db/tables")
def db_list_tables():
    """List the public PostGIS tables (vector / raster) with kind, srid and row estimate."""
    try:
        from FR.db_catalog import list_tables

        return {"tables": list_tables()}
    except Exception as exc:  # noqa: BLE001
        _raise_db_http_error(exc)


@app.get("/db/tables/{table}")
def db_describe_table(table: str):
    """Describe one table: columns, srid, exact row count, WGS84 extent, region/date metadata."""
    try:
        from FR.db_catalog import describe_table

        return describe_table(table)
    except Exception as exc:  # noqa: BLE001
        _raise_db_http_error(exc)


@app.get("/db/vector/{table}")
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
        _raise_db_http_error(exc)


@app.get("/db/raster/{table}")
def db_raster_table(table: str):
    """Summarise a raster table: tile count, srid, bands, WGS84 extent, regions/dates."""
    try:
        from FR.db_catalog import raster_metadata

        return raster_metadata(table)
    except Exception as exc:  # noqa: BLE001
        _raise_db_http_error(exc)


if __name__ == "__main__":
    # Host/port are env-configurable so they can be changed from the .env file
    # without touching code (defaults match the bundled docker-compose setup).
    host = os.environ.get("STORCITO_HOST", "0.0.0.0")
    port = int(os.environ.get("STORCITO_PORT", "8085"))
    uvicorn.run("api:app", host=host, port=port, reload=True)
