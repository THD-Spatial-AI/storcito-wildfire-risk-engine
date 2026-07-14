"""Job execution: engine runs, wildfire payload runs, results and callbacks."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request
from shapely.geometry import mapping

from app.engines.FFRM_estatic_aoi import run_static_aoi_for_geometry
from FR.aoi import DEFAULT_PROJECTED_CRS, reproject_geometry, resample_raster_resolution
from FR.db_reconstruct import reconstruct_inputs

from app.config import (
    AOI_OUTPUT_ROOT,
    BASE_DIR,
    DEBUG_LOG,
    ENGINE_SCRIPTS,
    JOBS_OUTPUT_ROOT,
    MODEL_VERSION,
    logger,
)
from app.schemas import WildfireCalculationRequest
from app.services.fwi_sampling import write_model_weather_summary
from app.services.payload import (
    wildfire_calculation_mode,
    wildfire_clip_geometry_wgs84,
    wildfire_context_buffer,
    wildfire_date_range,
    wildfire_geometry,
    wildfire_optional_layers,
    wildfire_risk_profile,
    wildfire_target_date,
)
from app.services.user_inputs import (
    user_input_lock,
    validate_user_raster_coverage,
    wildfire_user_inputs,
)


def public_base_url(request: Request | None) -> str:
    env_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if env_url:
        return env_url
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def job_relative_path(file_path: str, root: Path = AOI_OUTPUT_ROOT) -> str | None:
    try:
        resolved = Path(file_path).resolve()
        return resolved.relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def augment_with_urls(
    outputs: dict[str, str],
    request: Request | None,
    *,
    root: Path = AOI_OUTPUT_ROOT,
    url_prefix: str = "results",
) -> dict[str, Any]:
    base_url = public_base_url(request)
    urls: dict[str, str] = {}
    for key, value in outputs.items():
        if not isinstance(value, str):
            continue
        rel = job_relative_path(value, root)
        if rel is None:
            continue
        urls[key] = f"{base_url}/{url_prefix}/{rel}" if base_url else f"/{url_prefix}/{rel}"
    enriched: dict[str, Any] = dict(outputs)
    if urls:
        enriched["urls"] = urls
        if "final_map" in urls:
            enriched["download_url"] = urls["final_map"]
    return enriched


def zip_job_outputs(job_dir: Path) -> Path:
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


def post_result_callback(callback_url: str, zip_path: Path, session_id: str | None) -> dict[str, Any]:
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


def store_results_to_db(
    outputs: dict[str, Any],
    *,
    metadata: dict[str, Any],
    aoi_wgs84=None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Best-effort PostGIS store of result maps (STORCITO_STORE_RESULTS; never raises)."""
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


def validate_risk_outputs(outputs: dict[str, Any]) -> None:
    """Validate required risk rasters and an optional quality-coverage raster."""
    import numpy as np
    import rasterio

    grids = []
    for key in ("final_map", "continuous_map"):
        raw = outputs.get(key)
        if not isinstance(raw, (str, Path)):
            raise RuntimeError(f"required output is missing: {key}")
        path = Path(raw)
        with rasterio.open(path) as src:
            if src.count != 1 or src.crs is None or src.crs.to_epsg() != 32629:
                raise RuntimeError(f"{key} must be a single-band EPSG:32629 raster")
            grids.append((src.width, src.height, src.transform, src.crs))
            valid_count = 0
            minimum = float("inf")
            maximum = float("-inf")
            integer_classes = True
            for _index, window in src.block_windows(1):
                values = src.read(1, window=window, masked=True).compressed().astype("float64")
                values = values[np.isfinite(values)]
                if not values.size:
                    continue
                valid_count += int(values.size)
                minimum = min(minimum, float(values.min()))
                maximum = max(maximum, float(values.max()))
                if key == "final_map" and not np.allclose(
                    values, np.rint(values), atol=1e-6
                ):
                    integer_classes = False
        if not valid_count or minimum < 0 or maximum > 5.0001:
            raise RuntimeError(f"{key} is blank or outside the expected 0..5 risk domain")
        if key == "final_map" and not integer_classes:
            raise RuntimeError("final_map contains non-integer risk classes")
    if grids[0] != grids[1]:
        raise RuntimeError("final_map and continuous_map use different raster grids")

    coverage_raw = outputs.get("data_coverage")
    if coverage_raw is not None:
        with rasterio.open(Path(coverage_raw)) as src:
            coverage_grid = (src.width, src.height, src.transform, src.crs)
            values = src.read(1, masked=True).compressed().astype("float64")
        values = values[np.isfinite(values)]
        if (
            coverage_grid != grids[0]
            or not values.size
            or float(values.min()) < 0
            or float(values.max()) > 1.0001
        ):
            raise RuntimeError(
                "data_coverage must match the result grid and contain values in 0..1"
            )


def raise_aoi_http_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    detail = str(exc)
    try:
        msg = (
            f"{datetime.now().isoformat()} {type(exc).__name__}: {detail}"
        )
        print(f"[STORCITO ERR] {msg}", flush=True)
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as fh:
            fh.write(f"--- {datetime.now().isoformat()} ---\n{msg}\n")
            import traceback as _tb
            _tb.print_exception(type(exc), exc, exc.__traceback__, file=fh)
            fh.write("\n")
    except Exception:
        pass
    if isinstance(exc, (ValueError, LookupError)):
        raise HTTPException(status_code=422, detail=detail) from exc
    raise HTTPException(status_code=500, detail="Wildfire calculation failed.") from exc


def create_job_dir(payload: WildfireCalculationRequest) -> tuple[str, Path]:
    """Atomically create a unique per-request job directory."""
    raw = f"{payload.user_id}_{payload.model_id}_{payload.session_id}"
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("_")[:100] or "job"
    job_id = f"{prefix}_{uuid4().hex[:12]}"
    job_dir = JOBS_OUTPUT_ROOT / job_id
    resolved = job_dir.resolve()
    if resolved != JOBS_OUTPUT_ROOT and not str(resolved).startswith(str(JOBS_OUTPUT_ROOT) + os.sep):
        raise ValueError("Invalid job identifier derived from request IDs.")
    resolved.mkdir(parents=True, exist_ok=False)
    return job_id, resolved


def _optional_layer_enabled(optional_layers: dict[str, bool] | None, key: str) -> bool:
    return optional_layers is None or optional_layers.get(key, False)


def _uses_full_regional_layers(optional_layers: dict[str, bool] | None) -> bool:
    expected = {"weather_overlay", "terrain_analysis", "historical_fires"}
    return optional_layers is None or (
        set(optional_layers) == expected and all(optional_layers.values())
    )


def run_wildfire_payload(payload: WildfireCalculationRequest, request: Request | None = None):
    input_cache_key = hashlib.sha256(
        f"{payload.user_id}\0{payload.model_id}".encode("utf-8")
    ).hexdigest()[:24]
    input_dir = AOI_OUTPUT_ROOT / "_user_inputs" / input_cache_key
    if payload.parameters.get("user_inputs") is None:
        return _run_wildfire_payload(payload, request, input_dir)
    with user_input_lock(input_dir):
        return _run_wildfire_payload(payload, request, input_dir)


def _run_wildfire_payload(
    payload: WildfireCalculationRequest,
    request: Request | None,
    input_dir: Path,
):
    calculation_mode = wildfire_calculation_mode(payload)
    risk_profile = wildfire_risk_profile(payload)
    requested_date = wildfire_target_date(payload)
    start_date, target_date = wildfire_date_range(payload, calculation_mode)
    output_aoi = wildfire_geometry(payload)
    context_buffer_m = wildfire_context_buffer(payload)
    optional_layers = wildfire_optional_layers(payload)
    force_compute = payload.parameters.get("force_compute", False)
    if not isinstance(force_compute, bool):
        raise ValueError("parameters.force_compute must be a boolean when provided.")
    raw_user_inputs = payload.parameters.get("user_inputs")
    if isinstance(raw_user_inputs, dict):
        if "ndvi" in raw_user_inputs and (
            calculation_mode != "dynamic" or risk_profile != "finca"
        ):
            raise ValueError(
                "A precomputed NDVI user input is supported only for dynamic finca calculations."
            )
        if "station_data" in raw_user_inputs and not _optional_layer_enabled(
            optional_layers, "weather_overlay"
        ):
            raise ValueError("station_data cannot be supplied when weather_overlay is disabled.")
    processing_aoi = output_aoi.buffer(context_buffer_m)
    user_inputs = wildfire_user_inputs(
        payload,
        input_dir,
        processing_aoi=processing_aoi,
    )
    validate_user_raster_coverage(user_inputs, processing_aoi)

    # Serve from the nightly precomputed regional map when the request is a plain regional dynamic run (no custom inputs/layers, no force_compute): ST_Clip in seconds instead of a ~30 min engine run.
    if (
        calculation_mode == "dynamic"
        and risk_profile == "regional"
        # Keep this guard explicit if a future per-day multi-output mode is added.
        and (start_date is None or start_date == target_date)
        and not user_inputs
        and _uses_full_regional_layers(optional_layers)
        and not force_compute
    ):
        from app.services.precomputed import get_precomputed_result

        pre = get_precomputed_result(
            target_date, output_aoi, resolution_m=payload.resolution
        )
        if pre is not None:
            print(f"[STORCITO] serving precomputed regional map for {target_date}", flush=True)
            return _finish_wildfire_response(pre, payload, request, calculation_mode,
                                             risk_profile, requested_date, target_date, start_date,
                                             output_aoi, optional_layers, user_inputs)

    classification_breaks = None
    classification_breaks_by_date = None
    if calculation_mode == "dynamic":
        include_lst = _optional_layer_enabled(optional_layers, "weather_overlay")
        include_twi = _optional_layer_enabled(optional_layers, "terrain_analysis")
        shared_breaks = _region_breaks(
            target_date,
            strict=include_twi,
            include_lst=False,
            include_twi=include_twi,
        )
        classification_breaks_by_date = {}
        if start_date is None:
            scoring_days = [target_date]
        else:
            scoring_days = [
                start_date + timedelta(days=offset)
                for offset in range((target_date - start_date).days + 1)
            ]
        for scoring_day in scoring_days:
            day_breaks = dict(shared_breaks)
            if include_lst:
                day_breaks.update(
                    _region_breaks(
                        scoring_day,
                        strict=False,
                        include_lst=True,
                        include_twi=False,
                    )
                )
            classification_breaks_by_date[scoring_day.isoformat()] = day_breaks
        classification_breaks = classification_breaks_by_date.get(
            target_date.isoformat(), shared_breaks
        )

    outputs = run_static_aoi_for_geometry(
        output_aoi,
        target_date,
        start_date=start_date,
        context_buffer_m=context_buffer_m,
        optional_layers=optional_layers,
        dtm_path=user_inputs.get("dtm"),
        ndvi_path=user_inputs.get("ndvi"),
        station_data_path=user_inputs.get("station_data"),
        calculation_mode=calculation_mode,
        risk_profile=risk_profile,
        classification_breaks=classification_breaks,
        classification_breaks_by_date=classification_breaks_by_date,
        output_resolution_m=payload.resolution,
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
            "requested_date": requested_date.isoformat(),
            "selected_assessment_date": target_date.isoformat(),
            "buffer_distance": payload.buffer_distance,
            "resolution": payload.resolution,
            "calculation_mode": calculation_mode,
            "risk_profile": risk_profile,
            "optional_layers": optional_layers or {},
        },
    )
    return _finish_wildfire_response(outputs, payload, request, calculation_mode,
                                     risk_profile, requested_date, target_date, start_date,
                                     output_aoi, optional_layers, user_inputs)


def _finish_wildfire_response(outputs, payload, request, calculation_mode,
                              risk_profile, requested_date, target_date, start_date,
                              output_aoi, optional_layers, user_inputs):
    """Shared tail of a wildfire request: weather summary, URLs, DB store, callback zip, response dict. Used by both the computed and the precomputed (regional clip) paths."""
    precomputed = outputs.get("source") == "precomputed"
    if not precomputed:
        validate_risk_outputs(outputs)

    risk_date = target_date
    if outputs.get("peak_date"):
        risk_date = date.fromisoformat(str(outputs["peak_date"]))
    weather_summary_path = write_model_weather_summary(
        Path(outputs["job_dir"]),
        payload=payload,
        target_date=risk_date,
        output_aoi=output_aoi,
        start_date=start_date,
        range_end_date=target_date,
        optional_layers=optional_layers,
        user_inputs=user_inputs,
    )
    if weather_summary_path is not None:
        outputs["weather_summary"] = str(weather_summary_path)

    enriched_outputs = augment_with_urls(outputs, request)

    db_info = db_error = None
    if not precomputed:
        # Precomputed responses are clips of an already-stored regional map; storing them again would duplicate rasters per request.
        db_info, db_error = store_results_to_db(
            outputs,
            metadata={
                "job_id": outputs.get("request_id"),
                "session_id": payload.session_id,
                "user_id": payload.user_id,
                "model_id": payload.model_id,
                "engine": "static_aoi",
                "calculation_mode": calculation_mode,
                "request_type": "wildfire_payload",
                "target_date": risk_date.isoformat(),
                "range_end_date": target_date.isoformat(),
                "country": payload.country,
                "lkr": payload.lkr,
                "risk_profile": risk_profile,
                "output_resolution_m": payload.resolution,
            },
            aoi_wgs84=reproject_geometry(output_aoi, DEFAULT_PROJECTED_CRS, "EPSG:4326"),
        )

    callback_info: dict[str, Any] | None = None
    callback_error: str | None = None
    if payload.callback_url:
        job_dir_str = outputs.get("job_dir")
        if job_dir_str:
            try:
                zip_path = zip_job_outputs(Path(job_dir_str))
                enriched_outputs["result_zip"] = str(zip_path)
                rel = job_relative_path(str(zip_path))
                if rel is not None:
                    base_url = public_base_url(request)
                    zip_url = f"{base_url}/results/{rel}" if base_url else f"/results/{rel}"
                    enriched_outputs.setdefault("urls", {})["result_zip"] = zip_url
                callback_info = post_result_callback(
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
        "requested_date": requested_date.isoformat(),
        "target_date": risk_date.isoformat(),
        "range_end_date": target_date.isoformat(),
        "source": "precomputed" if precomputed else "computed",
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



def _region_breaks(
    target_date,
    *,
    strict: bool = False,
    include_lst: bool = True,
    include_twi: bool = True,
) -> dict[str, str]:
    """Region-wide 20/40/60/80 percentile breakpoints for the percentile- classified layers, so tiled/partial runs classify identically everywhere. LST is per assessment date (from lst_ts); TWI is static and cached. Empty dict on any failure -> engine falls back to extent-local percentiles."""
    breaks: dict[str, str] = {}
    try:
        from FR.db_reconstruct import _pg_connect, _ts_date_for

        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5min'")
            cur.execute("SET postgis.gdal_enabled_drivers = 'GTiff'")
            cur.execute(
                """CREATE TABLE IF NOT EXISTS layer_breaks (layer text PRIMARY KEY, breaks float8[], computed_at timestamptz, source_signature text)"""
            )
            cur.execute(
                "ALTER TABLE layer_breaks ADD COLUMN IF NOT EXISTS source_signature text"
            )

            def source_signature(table: str, where: str = "", params=()) -> str:
                cur.execute(
                    f"SELECT count(*), coalesce(max(xmin::text::bigint), 0) "
                    f"FROM {table} {where}",
                    params,
                )
                count, xmax = cur.fetchone()
                return f"{count}:{xmax}"

            def cached(layer: str, signature: str):
                cur.execute(
                    "SELECT breaks FROM layer_breaks "
                    "WHERE layer=%s AND source_signature=%s",
                    (layer, signature),
                )
                row = cur.fetchone()
                return list(row[0]) if row else None

            def store(layer: str, signature: str, values) -> None:
                cur.execute(
                    "INSERT INTO layer_breaks (layer, breaks, computed_at, source_signature) "
                    "VALUES (%s, %s, now(), %s) "
                    "ON CONFLICT (layer) DO UPDATE SET breaks=EXCLUDED.breaks, "
                    "computed_at=EXCLUDED.computed_at, "
                    "source_signature=EXCLUDED.source_signature",
                    (layer, list(values), signature),
                )

            capture = (
                _ts_date_for(
                    "lst_ts",
                    target_date,
                    max_age_days=int(os.environ.get("STORCITO_MAX_LST_AGE_DAYS", "3")),
                )
                if include_lst
                else None
            )
            if include_lst and capture is None:
                raise RuntimeError(f"no LST capture is available on or before {target_date}")
            if capture:
                lst_layer = f"lst:{capture}"
                lst_sig = source_signature(
                    "lst_ts", "WHERE capture_date=%s", (capture,)
                )
                vals = cached(lst_layer, lst_sig)
                if vals is None:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lst_layer,))
                    vals = cached(lst_layer, lst_sig)
                if vals is None:
                    cur.execute(
                        "SELECT ST_AsGDALRaster(ST_Union(rast), 'GTiff') "
                        "FROM lst_ts WHERE capture_date = %s",
                        (capture,),
                    )
                    row = cur.fetchone()
                    if not row or not row[0]:
                        raise RuntimeError(f"no LST raster bytes for {capture}")
                    import io

                    import numpy as np
                    import rasterio

                    with rasterio.open(io.BytesIO(bytes(row[0]))) as src:
                        arr = src.read(1)
                    valid = arr[np.isfinite(arr) & (arr > 220.0) & (arr < 340.0)]
                    if not valid.size:
                        raise RuntimeError(f"no physically valid LST pixels for {capture}")
                    vals = np.percentile(valid, [20, 40, 60, 80]).tolist()
                    store(lst_layer, lst_sig, vals)
                breaks["FFRM_LST_BREAKS"] = ",".join(f"{v:.3f}" for v in vals)

            if include_twi:
                twi_sig = source_signature("twi")
                vals = cached("twi", twi_sig)
                if vals is None:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext('twi'))")
                    vals = cached("twi", twi_sig)
                if vals is None:
                    cur.execute(
                        """SELECT (q).value FROM ( SELECT ST_Quantile(ST_Union(rast), ARRAY[0.2,0.4,0.6,0.8]) q FROM twi WHERE mod(abs(hashint8(rid)::bigint), 50) = 0) s"""
                    )
                    vals = [r[0] for r in cur.fetchall()]
                    if len(vals) != 4:
                        raise RuntimeError("unable to compute four TWI breakpoints")
                    store("twi", twi_sig, vals)
                breaks["FFRM_TWI_BREAKS"] = ",".join(f"{v:.4f}" for v in vals)
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise RuntimeError(f"region breakpoints unavailable: {exc}") from exc
        print(f"[STORCITO] region breaks unavailable ({exc}); extent-local percentiles", flush=True)
    return breaks



# Static products required by dynamic combine step. Date-dependent layers absent.
STATIC_CACHE_VERSION = MODEL_VERSION
_STATIC_REQUIRED = (
    Path("TIFs/MDT_RISK_MAP.tif"),
    Path("TIFs/SLOPE_RISK_MAP.tif"),
    Path("TIFs/ASPECT_RISK_MAP.tif"),
    Path("twi.tif"),
    Path("twi_risk_map.tif"),
    Path("TIFs/FMT.tif"),
    Path("TIFs/galicia_solo_vehiculos_(INFRA Risk_Map).tif"),
    Path("TIFs/IUF_Risk_Map.tif"),
)


def _static_signature(twi_breaks: str | None) -> dict | None:
    """Fingerprint of the date-independent source tables; a reseed of any of them (or a cache-version bump) invalidates the cached static layers."""
    try:
        from FR.db_reconstruct import _pg_connect

        sig: dict = {"v": STATIC_CACHE_VERSION, "twi_breaks": twi_breaks}
        with _pg_connect() as conn, conn.cursor() as cur:
            for t in ("dtm", "twi", "fuels"):
                cur.execute(
                    f"SELECT count(*), coalesce(max(rid), 0), "
                    f"coalesce(max(xmin::text::bigint), 0) FROM {t}"
                )
                sig[t] = list(cur.fetchone())
            for t in ("infra", "iuf"):
                cur.execute(
                    f"SELECT count(*), coalesce(max(xmin::text::bigint), 0) FROM {t}"
                )
                sig[t] = list(cur.fetchone())
        return sig
    except Exception:
        return None


def _valid_static_cache(cache_dir: Path, signature: dict | None) -> bool:
    if signature is None:
        return False
    try:
        cached_signature = json.loads((cache_dir / "signature.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return cached_signature == signature and all(
        (cache_dir / "re" / rel).is_file() for rel in _STATIC_REQUIRED
    )


def _publish_static_cache(cache_dir: Path, source_dir: Path, signature: dict) -> None:
    """Publish a complete cache directory; an incomplete copy is never valid."""
    tmp = cache_dir.with_name(f".{cache_dir.name}.{uuid4().hex}.tmp")
    try:
        for rel in _STATIC_REQUIRED:
            src = source_dir / rel
            if not src.is_file():
                raise FileNotFoundError(f"static layer missing after successful run: {src}")
            dest = tmp / "re" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        (tmp / "signature.json").write_text(json.dumps(signature, sort_keys=True))
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        tmp.rename(cache_dir)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def run_engine_job(
    payload: WildfireCalculationRequest,
    engine: str,
    request: Request | None,
) -> dict[str, Any]:
    """Reconstruct inputs from PostGIS into a per-request folder and run an engine."""
    if wildfire_optional_layers(payload) is not None:
        raise ValueError(
            "parameters.optional_layers is supported by the AOI workflow, not whole-engine endpoints."
        )
    if payload.parameters.get("user_inputs") is not None:
        raise ValueError(
            "parameters.user_inputs is supported by the AOI workflow, not whole-engine endpoints."
        )
    if wildfire_risk_profile(payload) != "regional":
        raise ValueError("Whole-engine endpoints support only the regional risk profile.")
    if "calculation_mode" in payload.parameters and wildfire_calculation_mode(payload) != engine:
        raise ValueError(
            f"parameters.calculation_mode must be {engine!r} for the /run-{engine} endpoint."
        )
    cfg = ENGINE_SCRIPTS[engine]
    requested_date = wildfire_target_date(payload)
    start_date, target_date = wildfire_date_range(payload, engine)
    if engine == "dynamic" and start_date is not None and start_date < target_date:
        # The whole-region script has only one optical/LST input slot and cannot represent a temporally coherent multi-day window. Route ranges through the canonical AOI workflow, which reconstructs and scores every day.
        parameters = dict(payload.parameters)
        parameters["calculation_mode"] = "dynamic"
        canonical_payload = payload.model_copy(update={"parameters": parameters})
        return run_wildfire_payload(canonical_payload, request)
    output_aoi = wildfire_geometry(payload)
    clip_geom = wildfire_clip_geometry_wgs84(payload)

    job_id, job_dir = create_job_dir(payload)
    input_dir = job_dir / "INPUT"
    output_dir = job_dir / "OUTPUT"
    input_dir.mkdir(parents=True, exist_ok=False)
    output_dir.mkdir(parents=True, exist_ok=True)

    import time as _t

    print(f"[job {job_id}] phase 1/3: reconstructing engine inputs from PostGIS "
          f"(exports every layer clipped to the AOI; typically 2-10 min)", flush=True)
    _t0 = _t.time()
    reconstruction = reconstruct_inputs(
        input_dir,
        engine=engine,
        target_date=target_date,
        start_date=start_date,
        include_history=engine == "static",
        clip_geom=clip_geom,
        clip_geom_crs="EPSG:4326",
    )
    print(f"[job {job_id}] phase 1/3 done in {_t.time() - _t0:.0f}s "
          f"(layer dates: {reconstruction.get('layer_dates')})", flush=True)

    # Static-layer cache (regional tiles only): terrain/TWI/fuel/infra/WUI do not depend on the date, so reuse the previous run's outputs for the same tile geometry while the source tables are unchanged.
    lst_available = (input_dir / "LST" / "LST.tiff").is_file()
    region_breaks = (
        _region_breaks(
            target_date,
            strict=True,
            include_lst=lst_available,
            include_twi=True,
        )
        if engine == "dynamic"
        else {}
    )
    temporal_flags = (
        {"FFRM_RUN_LST": "0"} if engine == "dynamic" and not lst_available else {}
    )
    static_flags: dict[str, str] = {}
    cache_dir = None
    static_sig = None
    if payload.user_id == "regional" and engine == "dynamic" and clip_geom is not None:
        key = hashlib.sha1(clip_geom.wkt.encode()).hexdigest()[:16]
        cache_dir = JOBS_OUTPUT_ROOT.parent / "cache" / "static_layers" / key
        static_sig = _static_signature(region_breaks.get("FFRM_TWI_BREAKS"))
        if _valid_static_cache(cache_dir, static_sig):
            shutil.copytree(cache_dir / "re", output_dir / "re", dirs_exist_ok=True)
            static_flags = {"FFRM_GENERATE_MDT": "0", "FFRM_GENERATE_TWI": "0",
                            "FFRM_GENERATE_FMT": "0", "FFRM_GENERATE_INFRA": "0",
                            "FFRM_GENERATE_WUI": "0"}
            print(f"[job {job_id}] static layers reused from cache {key}", flush=True)

    print(f"[job {job_id}] phase 2/3: region-wide LST/TWI class breakpoints", flush=True)
    env = {
        **os.environ,
        "FFRM_BASE_DIR": str(job_dir),
        "FFRM_OUTPUT_DIR": str(output_dir),
        "MPLBACKEND": "Agg",
        **region_breaks,
        **cfg["run_flags"],
        **temporal_flags,
        "FFRM_FWI_TARGET_DATE": target_date.isoformat(),
        "FFRM_FWI_START_DATE": start_date.isoformat() if start_date else "",
        **(
            {"FFRM_RUN_FHIST": "0"}
            if engine == "static"
            and not reconstruction.get("hist", {}).get("complete_scene_years")
            else {}
        ),
        **static_flags,
    }
    print(f"[job {job_id}] phase 3/3: {engine} engine started - follow live: "
          f"tail -f {output_dir / 'engine.log'}", flush=True)
    _t1 = _t.time()
    log_path = output_dir / "engine.log"
    with log_path.open("w") as log_fh:
        proc = subprocess.run(
            ["python", str(cfg["script"])],
            cwd=str(BASE_DIR),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
        )
    with log_path.open("a") as log_fh:
        log_fh.write(f"\nreturncode={proc.returncode}\n")
    print(f"[job {job_id}] engine finished in {_t.time() - _t1:.0f}s "
          f"rc={proc.returncode}", flush=True)
    if proc.returncode != 0:
        tail = log_path.read_text()[-2000:]
        raise RuntimeError(f"{engine} engine failed (see engine.log):\n{tail}")

    result_map = output_dir / cfg["result"]
    if not result_map.is_file():
        raise RuntimeError(
            f"{engine} engine finished but {cfg['result']} was not produced.\n{log_path.read_text()[-1000:]}"
        )

    continuous = "mapa_final_dinamico.tif" if engine == "dynamic" else "mapa_final.tif"
    continuous_map = output_dir / continuous
    if not continuous_map.is_file():
        raise RuntimeError(f"{engine} engine finished but {continuous} was not produced")
    data_coverage = output_dir / "data_coverage.tif"
    if not data_coverage.is_file():
        raise RuntimeError(f"{engine} engine finished but data_coverage.tif was not produced")
    if payload.resolution is not None:
        from rasterio.warp import Resampling

        resample_raster_resolution(result_map, payload.resolution, resampling=Resampling.nearest)
        resample_raster_resolution(
            continuous_map, payload.resolution, resampling=Resampling.bilinear
        )
        resample_raster_resolution(
            data_coverage, payload.resolution, resampling=Resampling.bilinear
        )
    validate_risk_outputs(
        {
            "final_map": str(result_map),
            "continuous_map": str(continuous_map),
            "data_coverage": str(data_coverage),
        }
    )

    # Populate only after both required result maps prove the run completed.
    if cache_dir is not None and not static_flags and static_sig is not None:
        try:
            _publish_static_cache(cache_dir, output_dir / "re", static_sig)
            print(f"[job {job_id}] static layers cached for reuse", flush=True)
        except Exception as exc:  # noqa: BLE001 - cache is best-effort
            print(f"[job {job_id}] static cache write skipped: {exc}", flush=True)

    outputs = {
        "final_map": str(result_map),
        "continuous_map": str(continuous_map),
        "data_coverage": str(data_coverage),
        "job_dir": str(job_dir),
    }
    weather_summary_path = write_model_weather_summary(
        output_dir,
        payload=payload,
        target_date=target_date,
        output_aoi=output_aoi,
        start_date=start_date,
        range_end_date=target_date,
        optional_layers=wildfire_optional_layers(payload),
    )
    if weather_summary_path is not None:
        outputs["weather_summary"] = str(weather_summary_path)
    enriched = augment_with_urls(outputs, request, root=JOBS_OUTPUT_ROOT, url_prefix="jobs")

    response: dict[str, Any] = {
        "status": "success",
        "engine": engine,
        "served_by": __import__("socket").gethostname(),
        "job_id": job_id,
        "session_id": payload.session_id,
        "requested_date": requested_date.isoformat(),
        "target_date": target_date.isoformat(),
        "reconstruction": reconstruction,
        "outputs": enriched,
    }

    db_info, db_error = store_results_to_db(
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
            "requested_date": requested_date.isoformat(),
            "country": payload.country,
            "lkr": payload.lkr,
            "publication_id": payload.parameters.get("publication_id"),
            "model_version": MODEL_VERSION,
            "output_resolution_m": payload.resolution,
            "source_layer_dates": reconstruction.get("layer_dates", {}),
            "source_layer_date_details": reconstruction.get(
                "layer_date_details", {}
            ),
            "source_resolution_m": reconstruction.get(
                "layer_resolutions_m", {}
            ),
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
            zip_path = zip_job_outputs(output_dir)
            response["result_zip"] = str(zip_path)
            response["callback"] = post_result_callback(
                payload.callback_url, zip_path, payload.session_id
            )
        except Exception as exc:  # noqa: BLE001 - report callback failure, keep result
            response["callback_error"] = str(exc)

    return response
