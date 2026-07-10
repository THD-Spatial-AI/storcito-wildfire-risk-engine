"""Serve request AOIs by clipping the nightly precomputed regional maps.

The nightly job (scripts/nightly_process.sh) runs the whole-region dynamic
engine per available date and stores the result rasters in simulation_results
under user_id='regional'. Requests matching a precomputed date are answered by
ST_Clip on that raster in seconds instead of a ~30 min engine run.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from shapely.geometry import mapping

from FR.db_reconstruct import _pg_connect

from app.config import AOI_OUTPUT_ROOT

_REGIONAL_USER = "regional"
_MAP_KINDS = ("final_map", "continuous_map")


def _clip_raster(cur, map_kind: str, target_date: date, aoi_geojson: str) -> bytes | None:
    cur.execute(
        """
        WITH aoi AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 32629) AS g)
        SELECT ST_AsGDALRaster(ST_Clip(rast, aoi.g, true), 'GTiff')
        FROM simulation_results, aoi
        WHERE user_id = %s AND engine = 'dynamic' AND map_kind = %s
          AND target_date = %s
          AND ST_Intersects(ST_ConvexHull(rast), aoi.g)
        ORDER BY ST_Contains(ST_ConvexHull(rast), aoi.g) DESC,
                 ST_Area(ST_Intersection(ST_ConvexHull(rast), aoi.g)) DESC,
                 created_at DESC
        LIMIT 1
        """,
        (aoi_geojson, _REGIONAL_USER, map_kind, target_date),
    )
    row = cur.fetchone()
    return bytes(row[0]) if row and row[0] else None


def _render_png(tif_path: Path, png_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import rasterio

    with rasterio.open(tif_path) as src:
        data = src.read(1).astype("float32")
    data = np.where(data <= 0, np.nan, data)
    fig, ax = plt.subplots(figsize=(8, 6))
    im = ax.imshow(data, cmap="RdYlGn_r", vmin=1, vmax=5)
    fig.colorbar(im, ax=ax, label="Wildfire risk (1=low, 5=very high)")
    ax.set_axis_off()
    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def get_precomputed_result(target_date: date, aoi_projected) -> dict[str, Any] | None:
    """Clip the regional dynamic map for target_date to the AOI (EPSG:32629).

    Returns an outputs dict shaped like the AOI engine's, or None when no
    regional run exists for the date (caller falls back to on-demand compute).
    """
    aoi_geojson = json.dumps(mapping(aoi_projected))
    clipped: dict[str, bytes] = {}
    try:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SET postgis.gdal_enabled_drivers = 'GTiff'")
            for kind in _MAP_KINDS:
                raw = _clip_raster(cur, kind, target_date, aoi_geojson)
                if raw is None:
                    return None
                clipped[kind] = raw
    except Exception:
        return None  # any retrieval problem -> normal compute path

    request_id = (
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_pre_{target_date.isoformat()}"
    )
    job_dir = AOI_OUTPUT_ROOT / request_id
    job_dir.mkdir(parents=True, exist_ok=True)

    final_map = job_dir / "forest_fire_risk_map.tif"
    final_map.write_bytes(clipped["final_map"])
    continuous_map = job_dir / "mapa_final.tif"
    continuous_map.write_bytes(clipped["continuous_map"])
    final_png = job_dir / "forest_fire_risk_map.png"
    try:
        _render_png(final_map, final_png)
    except Exception:
        final_png = None  # PNG is presentation-only; never block on it

    outputs: dict[str, Any] = {
        "request_id": request_id,
        "job_dir": str(job_dir),
        "final_map": str(final_map),
        "continuous_map": str(continuous_map),
        "source": "precomputed",
        "precomputed_date": target_date.isoformat(),
    }
    if final_png is not None:
        outputs["final_png"] = str(final_png)
    return outputs
