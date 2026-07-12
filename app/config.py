"""Shared configuration: paths, engine registry and coverage sources."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("STORCITO_DATA_DIR", BASE_DIR / "data")).resolve()
INPUT_ROOT = DATA_DIR / "INPUT"
OUTPUT_ROOT = DATA_DIR / "OUTPUT"
ENGINE_DIR = BASE_DIR / "app" / "engines"
AOI_OUTPUT_ROOT = (OUTPUT_ROOT / "aoi").resolve()
JOBS_OUTPUT_ROOT = (OUTPUT_ROOT / "jobs").resolve()
MODEL_TZ = ZoneInfo("Europe/Madrid")
# Backward-compatible name for callers imported before the Galicia timezone was
# made explicit. Its value is intentionally Europe/Madrid.
BERLIN_TZ = MODEL_TZ

MODEL_VERSION = os.environ.get("STORCITO_MODEL_VERSION", "2026-07-12.1").strip()

# Engine registry: script, result file and run-flag overrides per engine.
ENGINE_SCRIPTS = {
    "static": {
        "script": ENGINE_DIR / "FFRM_static.py",
        "result": "forest_fire_risk_map.tif",
        # HIST is now reconstructed from the `hist` table + on-disk PRE/POST scenes.
        "run_flags": {"FFRM_RUN_FHIST": "1"},
    },
    "dynamic": {
        "script": ENGINE_DIR / "FFRM_dinamic.py",
        "result": "forest_fire_risk_map_dinamico.tif",
        # TWI / LST are now reconstructed from the `twi` / `lst` tables.
        "run_flags": {"FFRM_RUN_TWI": "1", "FFRM_RUN_LST": "1"},
    },
}

# Coverage sources: PostGIS layer tables, exported to the cache dir on change.
COVERAGE_SOURCE_TABLES = {
    "DTM": "dtm",
    "Sentinel B4": "sentinel_b4",
    "Sentinel B8": "sentinel_b8",
    "Fuel model": "fuels",
}
COVERAGE_RASTER_DIR = OUTPUT_ROOT / "cache" / "coverage_rasters"
COVERAGE_CACHE_PATH = OUTPUT_ROOT / "cache" / "available_data_coverage.geojson"

logger = logging.getLogger("uvicorn.error")
DEBUG_LOG = OUTPUT_ROOT / "storcito_422.log"
