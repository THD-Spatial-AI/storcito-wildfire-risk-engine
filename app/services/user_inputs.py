"""Resolution of user-supplied inputs (uploads and Postgres-stored files)."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path

import httpx

from FR.db_user_inputs import (
    KIND_DTM,
    KIND_NDVI,
    KIND_STATION_DATA,
    USER_INPUT_TABLE,
    materialize_user_input,
    store_dtm_file,
    store_ndvi_file,
    store_station_csv_file,
)

from app.config import logger
from app.schemas import WildfireCalculationRequest
from app.services.payload import (
    wildfire_calculation_mode,
    wildfire_date_range,
    wildfire_user_input_model_id,
)


MAX_INPUT_BYTES = {
    KIND_DTM: int(os.environ.get("STORCITO_MAX_DTM_UPLOAD_BYTES", str(1024**3))),
    KIND_NDVI: int(os.environ.get("STORCITO_MAX_NDVI_UPLOAD_BYTES", str(512 * 1024**2))),
    KIND_STATION_DATA: int(
        os.environ.get("STORCITO_MAX_STATION_UPLOAD_BYTES", str(25 * 1024**2))
    ),
}


@contextmanager
def user_input_lock(directory: Path):
    """Serialize reuse/update of one user/model input set across API workers."""
    import fcntl

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
    dest_names = {
        KIND_DTM: "dtm.tif",
        KIND_NDVI: "ndvi.tif",
        KIND_STATION_DATA: "station_data.csv",
    }
    dest_name = dest_names[kind]
    path = materialize_user_input(payload.user_id, model_id, kind, dest_dir / dest_name)
    if path is not None:
        _log_user_input(f"reused stored {kind} from {USER_INPUT_TABLE} -> {path}")
    return path


def wildfire_user_inputs(
    payload: WildfireCalculationRequest,
    dest_dir: Path,
    *,
    processing_aoi=None,
) -> dict[str, Path]:
    """Resolve user inputs: download fresh uploads, else fall back to Postgres-stored ones."""
    model_id = wildfire_user_input_model_id(payload)
    raw = payload.parameters.get("user_inputs")
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("parameters.user_inputs must be an object when provided.")
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
            if kind not in {KIND_DTM, KIND_NDVI, KIND_STATION_DATA}:
                raise ValueError(f"Unsupported user input kind: {kind!r}.")
            requested_kinds.add(kind)
            if url is None or url == "":
                _log_user_input(
                    f"stored user input requested kind={kind} "
                    f"user_id={payload.user_id} model_id={model_id}"
                )
                continue
            if not isinstance(url, str):
                raise ValueError(f"parameters.user_inputs.{kind} must be a URL string or null.")
            try:
                _log_user_input(
                    f"upload reference received kind={kind} "
                    f"user_id={payload.user_id} model_id={model_id}"
                )
                upload_path = dest_dir / f"{kind}.upload"
                source_filename = kind
                content_type = None
                with httpx.stream(
                    "GET", url, timeout=120.0, follow_redirects=True
                ) as resp:
                    resp.raise_for_status()
                    source_filename = _source_filename_from_response(resp, source_filename)
                    content_type = resp.headers.get("content-type")
                    content_length = resp.headers.get("content-length")
                    expected = content_length or "unknown"
                    maximum = MAX_INPUT_BYTES[kind]
                    if content_length is not None and int(content_length) > maximum:
                        raise ValueError(
                            f"{kind} upload exceeds the {maximum}-byte size limit."
                        )
                    _log_user_input(
                        f"downloading {kind} from wildfire backend "
                        f"source_filename={source_filename} content_type={content_type or '-'} "
                        f"expected_bytes={expected}"
                    )
                    downloaded = 0
                    progress_step = 25 * 1024 * 1024 if kind in {KIND_DTM, KIND_NDVI} else 5 * 1024 * 1024
                    next_progress = progress_step
                    with upload_path.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            if not chunk:
                                continue
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if downloaded > maximum:
                                raise ValueError(
                                    f"{kind} upload exceeds the {maximum}-byte size limit."
                                )
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
                    if downloaded == 0:
                        raise ValueError(f"{kind} upload is empty.")

                if kind in {KIND_DTM, KIND_NDVI}:
                    target = dest_dir / ("dtm.tif" if kind == KIND_DTM else "ndvi.tif")
                    upload_path.replace(target)
                    if processing_aoi is None:
                        raise ValueError(
                            f"{kind} upload cannot be persisted without a processing AOI."
                        )
                    validate_user_raster_coverage({kind: target}, processing_aoi)
                    store_raster = store_dtm_file if kind == KIND_DTM else store_ndvi_file
                    label = "DTM" if kind == KIND_DTM else "NDVI"
                    _log_user_input(
                        f"writing {label} into {USER_INPUT_TABLE} "
                        f"user_id={payload.user_id} model_id={model_id} path={target}"
                    )
                    stored = store_raster(
                        payload.user_id,
                        model_id,
                        target,
                        source_filename=source_filename,
                        content_type=content_type,
                    )
                    _log_user_input(
                        f"{label} row stored in {USER_INPUT_TABLE} "
                        f"user_id={payload.user_id} model_id={model_id} "
                        f"bytes={stored.get('nbytes')} footprint={'yes' if stored.get('footprint') else 'no'}"
                    )
                else:
                    from FR.FWI_excel import convert_station_file_to_csv, validate_station_fwi_csv

                    target = dest_dir / "station_data.csv"
                    _log_user_input(
                        f"normalizing station upload to CSV before database store "
                        f"source_filename={source_filename}"
                    )
                    convert_station_file_to_csv(upload_path, target)
                    upload_path.unlink(missing_ok=True)
                    calculation_mode = wildfire_calculation_mode(payload)
                    start_date, target_date = wildfire_date_range(payload, calculation_mode)
                    validate_station_fwi_csv(
                        target, start_date=start_date, target_date=target_date
                    )
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
            except Exception as exc:
                (dest_dir / f"{kind}.upload").unlink(missing_ok=True)
                (dest_dir / {
                    KIND_DTM: "dtm.tif",
                    KIND_NDVI: "ndvi.tif",
                    KIND_STATION_DATA: "station_data.csv",
                }[kind]).unlink(missing_ok=True)
                if isinstance(exc, httpx.HTTPStatusError):
                    detail = f"HTTP {exc.response.status_code}"
                elif isinstance(exc, httpx.RequestError):
                    detail = type(exc).__name__
                else:
                    detail = str(exc)
                logger.warning("Failed to download/store user input %s: %s", kind, detail)
                _log_user_input(f"{kind} download/store failed: {detail}")
                raise ValueError(f"Unable to use requested {kind} input: {detail}") from exc

    for kind in requested_kinds:
        if kind not in paths:
            stored = _materialize_stored_user_input(payload, model_id, kind, dest_dir)
            if stored is not None:
                paths[kind] = stored
            else:
                raise ValueError(
                    f"No stored {kind} input exists for user_id={payload.user_id!r}, "
                    f"model_id={model_id!r}."
                )

    if paths:
        _log_user_input(f"resolved user inputs for run: {', '.join(sorted(paths))}")
    else:
        _log_user_input(
            "no user inputs resolved for run; "
            f"user_id={payload.user_id} model_id={model_id}"
        )
    return paths


def validate_user_raster_coverage(paths: dict[str, Path], processing_aoi) -> None:
    """Require supplied rasters to cover the AOI with finite, non-nodata cells."""
    import numpy as np
    from rasterio.mask import raster_geometry_mask
    from shapely.geometry import box, mapping

    from FR.aoi import DEFAULT_PROJECTED_CRS, reproject_geometry

    for kind in (KIND_DTM, KIND_NDVI):
        path = paths.get(kind)
        if path is None:
            continue
        import rasterio

        with rasterio.open(path) as src:
            if src.crs is None:
                raise ValueError(f"Requested {kind} raster has no CRS.")
            if src.count != 1:
                raise ValueError(f"Requested {kind} raster must contain exactly one band.")
            aoi_in_raster = reproject_geometry(
                processing_aoi, DEFAULT_PROJECTED_CRS, str(src.crs)
            )
            covered = aoi_in_raster.intersection(box(*src.bounds)).area
            total = aoi_in_raster.area
            if total <= 0 or covered / total < 0.999:
                raise ValueError(
                    f"Requested {kind} raster does not cover the processing AOI and context buffer."
                )
            try:
                outside, _transform, window = raster_geometry_mask(
                    src, [mapping(aoi_in_raster)], crop=True
                )
            except ValueError as exc:
                raise ValueError(
                    f"Requested {kind} raster does not overlap the processing AOI."
                ) from exc
            data = src.read(1, window=window, masked=True)
            inside = ~outside
            valid = inside & ~np.ma.getmaskarray(data) & np.isfinite(np.ma.getdata(data))
            inside_count = int(np.count_nonzero(inside))
            valid_fraction = (
                float(np.count_nonzero(valid)) / inside_count if inside_count else 0.0
            )
            valid_values = np.ma.getdata(data)[valid]
        if valid_fraction < 0.999:
            raise ValueError(
                f"Requested {kind} raster has nodata gaps in the processing AOI "
                f"({valid_fraction:.2%} valid coverage)."
            )
        if inside_count < 16:
            raise ValueError(
                f"Requested {kind} raster is too coarse for the processing AOI "
                f"({inside_count} intersecting pixels; at least 16 required)."
            )
        if kind == KIND_NDVI and (
            float(valid_values.min()) < -1.0 or float(valid_values.max()) > 1.0
        ):
            raise ValueError("Requested ndvi raster contains values outside the [-1, 1] range.")
        if kind == KIND_DTM and (
            float(valid_values.min()) < -500 or float(valid_values.max()) > 9000
        ):
            raise ValueError(
                "Requested dtm raster contains implausible elevation values outside -500..9000 m."
            )
