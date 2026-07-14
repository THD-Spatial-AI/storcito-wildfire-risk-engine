"""STORCITO API entrypoint (logic in app/services/*, endpoints in app/routers/*)."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime

# Pre-import pyogrio for clean GDAL_DATA init
import pyogrio  # noqa: F401

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import DEBUG_LOG, logger
from app.routers import availability, db_catalog, fwi, results, simulations, system


class _HealthLogThrottle(logging.Filter):
    """Keep health/status probes running at full frequency but log each path at most once per 5 minutes - the probes are pure heartbeat noise."""

    INTERVAL_S = 300.0

    def __init__(self) -> None:
        super().__init__()
        self._last_logged: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for path in ("/health", "/status"):
            if f"{path} " in message or message.rstrip().endswith(path):
                now = time.monotonic()
                if now - self._last_logged.get(path, float("-inf")) >= self.INTERVAL_S:
                    self._last_logged[path] = now
                    return True
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(_HealthLogThrottle())

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
    errors = [
        {key: value for key, value in item.items() if key not in {"input", "url"}}
        for item in exc.errors()
    ]
    msg = (
        f"422 validation error on {request.method} {request.url.path}\n"
        f"  errors={errors}"
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
        content={"detail": errors},
    )


if __name__ == "__main__":
    # Host/port come from the environment (.env)
    uvicorn.run("app.api:app", host=host, port=port, reload=True)
