from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from shapely.geometry import mapping

from FR.aoi import resample_raster_resolution
from FR.db_reconstruct import _pg_connect

from app.config import AOI_OUTPUT_ROOT, MODEL_VERSION, logger

_REGIONAL_USER = "regional"
_MAP_KINDS = ("final_map", "continuous_map", "data_coverage")


def _valid_clipped_raster(
    raw: bytes, *, classified: bool, upper_bound: float = 5.0001
) -> bool:
    import numpy as np
    from rasterio.io import MemoryFile

    try:
        with MemoryFile(raw) as memory, memory.open() as src:
            if src.count != 1 or src.width <= 0 or src.height <= 0 or src.crs is None:
                return False
            if src.crs.to_epsg() != 32629:
                return False
            values = src.read(1, masked=True).compressed().astype("float64")
    except Exception:
        return False
    values = values[np.isfinite(values)]
    if not values.size or values.min() < 0 or values.max() > upper_bound:
        return False
    return not classified or bool(np.allclose(values, np.rint(values), atol=1e-6))


def _clip_rasters(cur, target_date: date, aoi_geojson: str) -> dict[str, bytes]:
    cur.execute(
        """WITH aoi AS ( SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 32629) AS g ), chosen AS ( SELECT q.publication_id, q.model_version FROM regional_runs q WHERE q.engine = 'dynamic' AND q.target_date = %s AND q.status = 'done' AND q.model_version = %s AND q.publication_id IS NOT NULL ORDER BY q.finished_at DESC NULLS LAST LIMIT 1 ) -- The regional tiles overlap by ~12-16 km but each has its own pixel -- grid, so ST_Union refuses to mosaic them ("do not have the same -- alignment"). Fetch every tile piece intersecting the AOI; a single -- covering tile is served directly and multi-tile AOIs are mosaicked -- with gdalwarp, which absorbs the sub-pixel grid offsets. SELECT r.map_kind, ST_AsGDALRaster(ST_Clip(r.rast, aoi.g, true), 'GTiff'), ST_Covers(ST_ConvexHull(r.rast), aoi.g), ST_Area(ST_ConvexHull(r.rast)) FROM simulation_results r JOIN chosen c ON c.publication_id = r.publication_id AND c.model_version = r.model_version CROSS JOIN aoi WHERE r.user_id = %s AND r.engine = 'dynamic' AND r.target_date = %s AND r.map_kind IN ('final_map', 'continuous_map', 'data_coverage') AND ST_Intersects(ST_ConvexHull(r.rast), aoi.g) ORDER BY r.map_kind, ST_Area(ST_ConvexHull(r.rast)) ASC""",
        (aoi_geojson, target_date, MODEL_VERSION, _REGIONAL_USER, target_date),
    )
    pieces: dict[str, list[tuple[bytes, bool]]] = {}
    for kind, raw, covers, _area in cur.fetchall():
        if raw is not None:
            pieces.setdefault(kind, []).append((bytes(raw), bool(covers)))

    clipped: dict[str, bytes] = {}
    for kind, rows in pieces.items():
        covering = [raw for raw, covers in rows if covers]
        if covering:
            clipped[kind] = covering[0]  # smallest covering tile (query order)
        elif len(rows) > 1:
            merged = _merge_tile_pieces(
                [raw for raw, _ in rows], classified=(kind == "final_map")
            )
            if merged is not None:
                clipped[kind] = merged
    return clipped


def _merge_tile_pieces(pieces: list[bytes], *, classified: bool) -> bytes | None:
    """Mosaic AOI-clipped tile fragments whose grids differ sub-pixel. gdalwarp resamples every piece onto the first piece's grid; nearest resampling keeps classified values intact and shifts data by less than a pixel. Overlap zones take the later tile (identical model output)."""
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        srcs = []
        for i, raw in enumerate(pieces):
            p = tmp_dir / f"piece_{i}.tif"
            p.write_bytes(raw)
            srcs.append(str(p))
        out = tmp_dir / "merged.tif"
        cmd = ["gdalwarp", "-of", "GTiff", "-r", "near", "-srcnodata", "0",
               "-dstnodata", "0", "-overwrite", *srcs, str(out)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not out.exists():
            logger.warning("Precomputed tile mosaic failed: %s",
                           result.stderr.strip()[:500])
            return None
        return out.read_bytes()


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
    fig.colorbar(im, ax=ax, label="Wildfire risk (1=very low, 5=very high/extreme)")
    ax.set_axis_off()
    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _matching_output_grids(*paths: Path) -> bool:
    import rasterio

    try:
        grids = []
        for path in paths:
            with rasterio.open(path) as src:
                grids.append((src.width, src.height, src.transform, src.crs))
        return bool(grids) and all(grid == grids[0] for grid in grids[1:])
    except Exception:
        return False


def get_precomputed_result(
    target_date: date, aoi_projected, *, resolution_m: float | None = None
) -> dict[str, Any] | None:
    """Clip the regional dynamic map for target_date to the AOI (EPSG:32629). Returns an outputs dict shaped like the AOI engine's, or None when no regional run exists for the date (caller falls back to on-demand compute)."""
    aoi_geojson = json.dumps(mapping(aoi_projected))
    clipped: dict[str, bytes] = {}
    try:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SET postgis.gdal_enabled_drivers = 'GTiff'")
            clipped = _clip_rasters(cur, target_date, aoi_geojson)
            if set(clipped) != set(_MAP_KINDS):
                return None
            if not _valid_clipped_raster(clipped["final_map"], classified=True) or not _valid_clipped_raster(
                clipped["continuous_map"], classified=False
            ) or not _valid_clipped_raster(
                clipped["data_coverage"], classified=False, upper_bound=1.0001
            ):
                return None
    except Exception as exc:
        logger.warning("Precomputed regional lookup failed for %s: %s", target_date, exc)
        return None  # any retrieval problem -> normal compute path

    request_id = (
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:12]}_pre_{target_date.isoformat()}"
    )
    job_dir = AOI_OUTPUT_ROOT / request_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        final_map = job_dir / "forest_fire_risk_map.tif"
        final_map.write_bytes(clipped["final_map"])
        continuous_map = job_dir / "mapa_final.tif"
        continuous_map.write_bytes(clipped["continuous_map"])
        data_coverage = job_dir / "data_coverage.tif"
        data_coverage.write_bytes(clipped["data_coverage"])
        layers_dir = job_dir / "layers"
        layers_dir.mkdir(parents=True, exist_ok=True)
        coverage_layer = layers_dir / "data_coverage.tif"
        coverage_layer.write_bytes(clipped["data_coverage"])
        if resolution_m is not None:
            from rasterio.warp import Resampling

            resample_raster_resolution(final_map, resolution_m, resampling=Resampling.nearest)
            resample_raster_resolution(
                continuous_map, resolution_m, resampling=Resampling.bilinear
            )
            resample_raster_resolution(
                data_coverage, resolution_m, resampling=Resampling.bilinear
            )
            resample_raster_resolution(
                coverage_layer, resolution_m, resampling=Resampling.bilinear
            )
        if (
            not _valid_clipped_raster(final_map.read_bytes(), classified=True)
            or not _valid_clipped_raster(continuous_map.read_bytes(), classified=False)
            or not _valid_clipped_raster(
                data_coverage.read_bytes(), classified=False, upper_bound=1.0001
            )
            or not _matching_output_grids(final_map, continuous_map, data_coverage)
        ):
            raise RuntimeError("precomputed output resampling produced invalid or mismatched maps")
        final_png = job_dir / "forest_fire_risk_map.png"
        try:
            _render_png(final_map, final_png)
        except Exception:
            final_png = None  # PNG is presentation-only; never block on it
        request_path = job_dir / "request.json"
        request_path.write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "source": "precomputed",
                    "model_version": MODEL_VERSION,
                    "peak_date": target_date.isoformat(),
                    "output_resolution_m": resolution_m,
                    "resolution_interpretation": (
                        "The output grid does not increase the native precision of "
                        "coarse FWI or LST source data."
                    ),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    outputs: dict[str, Any] = {
        "request_id": request_id,
        "job_dir": str(job_dir),
        "final_map": str(final_map),
        "continuous_map": str(continuous_map),
        "data_coverage": str(data_coverage),
        "layer_data_coverage": str(coverage_layer),
        "source": "precomputed",
        "peak_date": target_date.isoformat(),
        "precomputed_date": target_date.isoformat(),
        "output_resolution_m": resolution_m,
        "request": str(request_path),
    }
    if final_png is not None:
        outputs["final_png"] = str(final_png)
    return outputs
