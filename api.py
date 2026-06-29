import os
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

from fastapi import FastAPI, HTTPException, Request
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
from FR.aoi import build_geojson_aoi
from FR.FWI import available_fwi_dates, highest_temperature_fwi_dates

app = FastAPI(title="STORCITO API")
BASE_DIR = Path(__file__).resolve().parent
AOI_OUTPUT_ROOT = (BASE_DIR / "OUTPUT" / "aoi").resolve()
BERLIN_TZ = ZoneInfo("Europe/Berlin")

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


def _to_berlin_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BERLIN_TZ)
    return value.astimezone(BERLIN_TZ)


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


def _wildfire_user_inputs(payload: WildfireCalculationRequest, dest_dir: Path) -> dict[str, Path]:
    """Download optional user-supplied inputs (DTM, station data) advertised in
    parameters.user_inputs as URLs, returning local paths keyed by kind. Absent
    or unreachable inputs are skipped so the run falls back to bundled data.
    """
    raw = payload.parameters.get("user_inputs")
    if not isinstance(raw, dict) or not raw:
        return {}

    dest_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for kind, url in raw.items():
        if not isinstance(kind, str) or not isinstance(url, str) or not url:
            continue
        try:
            with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as resp:
                resp.raise_for_status()
                target = dest_dir / kind
                with target.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
            paths[kind] = target
        except Exception as exc:  # noqa: BLE001 - optional; fall back to bundled
            logger.warning("Failed to download user input %s from %s: %s", kind, url, exc)
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

    dates = [day.isoformat() for day in available_fwi_dates(BASE_DIR / "INPUT" / "FWI")]
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

def _job_relative_path(file_path: str) -> str | None:
    try:
        resolved = Path(file_path).resolve()
        return resolved.relative_to(AOI_OUTPUT_ROOT).as_posix()
    except (ValueError, OSError):
        return None


def _augment_with_urls(outputs: dict[str, str], request: Request | None) -> dict[str, Any]:
    base_url = _public_base_url(request)
    urls: dict[str, str] = {}
    for key, value in outputs.items():
        if not isinstance(value, str):
            continue
        rel = _job_relative_path(value)
        if rel is None:
            continue
        urls[key] = f"{base_url}/results/{rel}" if base_url else f"/results/{rel}"
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
    enriched_outputs = _augment_with_urls(outputs, request)

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
    return response


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
def run_dynamic():
    """
    Runs the FFRM_dinamic.py script.
    """
    try:
        # Run script as a subprocess
        result = subprocess.run(["python", "FFRM_dinamic.py"], capture_output=True, text=True)
        if result.returncode == 0:
            return {"status": "success", "output": result.stdout}
        else:
            return {"status": "error", "error": result.stderr}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.post("/run-static")
def run_static():
    """
    Runs the FFRM_estatic.py script.
    """
    try:
        # Run script as a subprocess
        result = subprocess.run(["python", "FFRM_estatic.py"], capture_output=True, text=True)
        if result.returncode == 0:
            return {"status": "success", "output": result.stdout}
        else:
            return {"status": "error", "error": result.stderr}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/available-static-dates")
def available_static_dates():
    # One representative day per year: the available FWI day with the highest
    # air temperature in that year.
    dates = highest_temperature_fwi_dates(BASE_DIR / "INPUT" / "FWI")
    return {"dates": [day.isoformat() for day in dates]}


@app.get("/available-dynamic-dates")
def available_dynamic_dates():
    dates = available_fwi_dates(BASE_DIR / "INPUT" / "FWI")
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
        return {"status": "success", "outputs": _augment_with_urls(outputs, request)}
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

if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8090, reload=True)
