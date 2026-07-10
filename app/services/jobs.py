"""Job execution: engine runs, wildfire payload runs, results and callbacks."""
from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException, Request
from shapely.geometry import mapping

from app.engines.FFRM_estatic_aoi import run_static_aoi_for_geometry
from FR.aoi import reproject_geometry, DEFAULT_PROJECTED_CRS
from FR.db_reconstruct import reconstruct_inputs

from app.config import (
    AOI_OUTPUT_ROOT,
    BASE_DIR,
    BERLIN_TZ,
    DEBUG_LOG,
    ENGINE_SCRIPTS,
    JOBS_OUTPUT_ROOT,
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
from app.services.user_inputs import wildfire_user_inputs


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


def raise_aoi_http_error(exc: Exception) -> None:
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
    if isinstance(exc, ValueError):
        if detail.startswith("Dynamic wildfire payloads are not supported"):
            raise HTTPException(status_code=501, detail=detail) from exc
        raise HTTPException(status_code=422, detail=detail) from exc
    raise HTTPException(status_code=500, detail=detail) from exc


def create_job_dir(payload: WildfireCalculationRequest) -> tuple[str, Path]:
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


def run_wildfire_payload(payload: WildfireCalculationRequest, request: Request | None = None):
    calculation_mode = wildfire_calculation_mode(payload)
    risk_profile = wildfire_risk_profile(payload)
    start_date, target_date = wildfire_date_range(payload, calculation_mode)
    output_aoi = wildfire_geometry(payload)
    optional_layers = wildfire_optional_layers(payload)
    user_inputs = wildfire_user_inputs(payload, AOI_OUTPUT_ROOT / "_user_inputs" / payload.model_id)

    # Serve from the nightly precomputed regional map when the request is a
    # plain regional dynamic run (no custom inputs/layers, no force_compute):
    # ST_Clip in seconds instead of a ~30 min engine run.
    if (
        calculation_mode == "dynamic"
        and risk_profile == "regional"
        # Multi-day requests produce per-day maps; a single-date regional
        # clip would silently change what the result means.
        and (start_date is None or start_date == target_date)
        and not user_inputs
        and optional_layers is None
        and not payload.parameters.get("force_compute")
    ):
        from app.services.precomputed import get_precomputed_result

        pre = get_precomputed_result(target_date, output_aoi)
        if pre is not None:
            print(f"[STORCITO] serving precomputed regional map for {target_date}", flush=True)
            return _finish_wildfire_response(pre, payload, request, calculation_mode,
                                             risk_profile, target_date, start_date,
                                             output_aoi, optional_layers, user_inputs)

    outputs = run_static_aoi_for_geometry(
        output_aoi,
        target_date,
        start_date=start_date,
        context_buffer_m=wildfire_context_buffer(payload),
        optional_layers=optional_layers,
        dtm_path=user_inputs.get("dtm"),
        ndvi_path=user_inputs.get("ndvi"),
        station_data_path=user_inputs.get("station_data"),
        calculation_mode=calculation_mode,
        risk_profile=risk_profile,
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
            "risk_profile": risk_profile,
            "optional_layers": optional_layers or {},
        },
    )
    return _finish_wildfire_response(outputs, payload, request, calculation_mode,
                                     risk_profile, target_date, start_date,
                                     output_aoi, optional_layers, user_inputs)


def _finish_wildfire_response(outputs, payload, request, calculation_mode,
                              risk_profile, target_date, start_date,
                              output_aoi, optional_layers, user_inputs):
    """Shared tail of a wildfire request: weather summary, URLs, DB store,
    callback zip, response dict. Used by both the computed and the
    precomputed (regional clip) paths."""
    precomputed = outputs.get("source") == "precomputed"

    weather_summary_path = write_model_weather_summary(
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

    enriched_outputs = augment_with_urls(outputs, request)

    db_info = db_error = None
    if not precomputed:
        # Precomputed responses are clips of an already-stored regional map;
        # storing them again would duplicate rasters per request.
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
                "target_date": target_date.isoformat(),
                "country": payload.country,
                "lkr": payload.lkr,
                "risk_profile": risk_profile,
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



def _region_breaks(target_date) -> dict[str, str]:
    """Region-wide 20/40/60/80 percentile breakpoints for the percentile-
    classified layers, so tiled/partial runs classify identically everywhere.
    LST is per assessment date (from lst_ts); TWI is static and cached.
    Empty dict on any failure -> engine falls back to extent-local percentiles.
    """
    breaks: dict[str, str] = {}
    try:
        from FR.db_reconstruct import _pg_connect, _ts_date_for

        with _pg_connect() as conn, conn.cursor() as cur:
            capture = _ts_date_for("lst_ts", target_date)
            if capture:
                cur.execute(
                    """SELECT (q).value FROM (
                         SELECT ST_Quantile(ST_Union(rast), ARRAY[0.2,0.4,0.6,0.8]) q
                         FROM lst_ts WHERE capture_date = %s) s""",
                    (capture,),
                )
                vals = [r[0] for r in cur.fetchall()]
                if len(vals) == 4:
                    breaks["FFRM_LST_BREAKS"] = ",".join(f"{v:.3f}" for v in vals)

            cur.execute(
                """CREATE TABLE IF NOT EXISTS layer_breaks
                   (layer text PRIMARY KEY, breaks float8[], computed_at timestamptz)"""
            )
            cur.execute("SELECT breaks FROM layer_breaks WHERE layer = 'twi'")
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """SELECT (q).value FROM (
                         SELECT ST_Quantile(ST_Union(rast), ARRAY[0.2,0.4,0.6,0.8]) q
                         FROM twi WHERE rid %% 50 = 0) s"""
                )
                vals = [r[0] for r in cur.fetchall()]
                if len(vals) == 4:
                    cur.execute(
                        "INSERT INTO layer_breaks VALUES ('twi', %s, now()) "
                        "ON CONFLICT (layer) DO NOTHING",
                        (vals,),
                    )
                    conn.commit()
                    row = (vals,)
            if row:
                breaks["FFRM_TWI_BREAKS"] = ",".join(f"{v:.4f}" for v in row[0])
    except Exception as exc:  # noqa: BLE001
        print(f"[STORCITO] region breaks unavailable ({exc}); extent-local percentiles", flush=True)
    return breaks


def run_engine_job(
    payload: WildfireCalculationRequest,
    engine: str,
    request: Request | None,
) -> dict[str, Any]:
    """Reconstruct inputs from PostGIS into a per-request folder and run an engine."""
    cfg = ENGINE_SCRIPTS[engine]
    target_date = wildfire_target_date(payload)
    output_aoi = wildfire_geometry(payload)
    clip_geom = wildfire_clip_geometry_wgs84(payload)

    job_id, job_dir = create_job_dir(payload)
    input_dir = job_dir / "INPUT"
    output_dir = job_dir / "OUTPUT"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    import time as _t

    print(f"[job {job_id}] phase 1/3: reconstructing engine inputs from PostGIS "
          f"(exports every layer clipped to the AOI; typically 2-10 min)", flush=True)
    _t0 = _t.time()
    reconstruction = reconstruct_inputs(
        input_dir,
        engine=engine,
        target_date=target_date,
        clip_geom=clip_geom,
        clip_geom_crs="EPSG:4326",
    )
    print(f"[job {job_id}] phase 1/3 done in {_t.time() - _t0:.0f}s "
          f"(layer dates: {reconstruction.get('layer_dates')})", flush=True)

    print(f"[job {job_id}] phase 2/3: region-wide LST/TWI class breakpoints", flush=True)
    env = {
        **os.environ,
        "FFRM_BASE_DIR": str(job_dir),
        "FFRM_OUTPUT_DIR": str(output_dir),
        "MPLBACKEND": "Agg",
        **_region_breaks(target_date),
        **cfg["run_flags"],
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
    outputs = {
        "final_map": str(result_map),
        "continuous_map": str(output_dir / continuous),
        "job_dir": str(job_dir),
    }
    weather_summary_path = write_model_weather_summary(
        output_dir,
        payload=payload,
        target_date=target_date,
        output_aoi=output_aoi,
        start_date=None,
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
            zip_path = zip_job_outputs(output_dir)
            response["result_zip"] = str(zip_path)
            response["callback"] = post_result_callback(
                payload.callback_url, zip_path, payload.session_id
            )
        except Exception as exc:  # noqa: BLE001 - report callback failure, keep result
            response["callback_error"] = str(exc)

    return response
