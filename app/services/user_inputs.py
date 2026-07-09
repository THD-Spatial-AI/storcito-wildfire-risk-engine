"""Resolution of user-supplied inputs (uploads and Postgres-stored files)."""
from __future__ import annotations

import re
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
from app.services.payload import wildfire_user_input_model_id


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


def wildfire_user_inputs(payload: WildfireCalculationRequest, dest_dir: Path) -> dict[str, Path]:
    """Resolve user inputs: download fresh uploads, else fall back to Postgres-stored ones."""
    model_id = wildfire_user_input_model_id(payload)
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
            if kind not in {KIND_DTM, KIND_NDVI, KIND_STATION_DATA}:
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
                    progress_step = 25 * 1024 * 1024 if kind in {KIND_DTM, KIND_NDVI} else 5 * 1024 * 1024
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

                if kind in {KIND_DTM, KIND_NDVI}:
                    target = dest_dir / ("dtm.tif" if kind == KIND_DTM else "ndvi.tif")
                    upload_path.replace(target)
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
