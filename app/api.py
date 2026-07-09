"""STORCITO API entrypoint (logic in app/services/*, endpoints in app/routers/*)."""
from __future__ import annotations

import os
from datetime import datetime

# Pre-import pyogrio for clean GDAL_DATA init 
import pyogrio  # noqa: F401

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import DEBUG_LOG, logger
from app.routers import availability, db_catalog, fwi, results, simulations, system

app = FastAPI(title="STORCITO API")

app.include_router(system.router)
app.include_router(availability.router)
app.include_router(simulations.router)
app.include_router(results.router)
app.include_router(fwi.router)
app.include_router(db_catalog.router)

print("[STORCITO] app/api.py loaded with validation logger v2", flush=True)


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
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as fh:
            fh.write(f"--- {datetime.now().isoformat()} ---\n{msg}\n\n")
    except Exception:
        pass
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body_preview": body_preview},
    )


if __name__ == "__main__":
    # Host/port come from the environment (.env)
    uvicorn.run("app.api:app", host=host, port=port, reload=True)
