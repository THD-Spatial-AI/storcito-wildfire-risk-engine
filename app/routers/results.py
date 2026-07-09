"""Result-file download endpoints."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import AOI_OUTPUT_ROOT, JOBS_OUTPUT_ROOT

router = APIRouter()


@router.get("/results/{request_id}/{file_path:path}")
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


@router.get("/jobs/{job_id}/{file_path:path}")
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
