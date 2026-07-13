"""Available-data coverage derived from the PostGIS layer tables."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import rasterio
from rasterio import features
from rasterio.enums import MaskFlags
from rasterio.warp import transform_bounds, transform_geom
from shapely.geometry import box, mapping, shape

from FR.db_reconstruct import available_fwi_dates_db, export_raster_table, _pg_connect

from app.config import (
    COVERAGE_CACHE_PATH,
    COVERAGE_RASTER_DIR,
    COVERAGE_SOURCE_TABLES,
    logger,
)


def _raster_coverage_box(path: Path):
    with rasterio.open(path) as src:
        source_crs = src.crs or "EPSG:4326"
        minx, miny, maxx, maxy = transform_bounds(
            source_crs,
            "EPSG:4326",
            src.bounds.left,
            src.bounds.bottom,
            src.bounds.right,
            src.bounds.top,
            densify_pts=21,
        )
    return box(minx, miny, maxx, maxy)


def _coverage_input_signature() -> list[dict[str, Any]]:
    """Cheap DB fingerprint of the coverage source tables (cache invalidation)."""
    signature: list[dict[str, Any]] = []
    with _pg_connect() as conn, conn.cursor() as cur:
        for name, table in COVERAGE_SOURCE_TABLES.items():
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            if cur.fetchone()[0] is None:
                raise RuntimeError(
                    f"Coverage source table '{table}' is missing; seed it with the "
                    f"Makefile data pipeline (see `make help`)."
                )
            cur.execute(f"SELECT count(*), coalesce(max(rid), 0) FROM public.{table}")
            count, max_rid = cur.fetchone()
            signature.append({"name": name, "table": table,
                              "rows": int(count), "max_rid": int(max_rid)})
        cur.execute("SELECT to_regclass('public.fwi_files')")
        if cur.fetchone()[0] is not None:
            cur.execute("SELECT count(*), coalesce(max(fdate)::text, '') FROM fwi_files")
            count, max_date = cur.fetchone()
            signature.append({"name": "FWI", "table": "fwi_files",
                              "rows": int(count), "max_date": max_date})
    return signature


def _coverage_region_geom():
    """Region polygon (WGS84) that bounds the coverage; None when unavailable.

    Clipping the masked raster to the region keeps hole/bay topology stable:
    without it, farmland corridors connect out through the raster's wider bbox
    and render as no-data bays instead of omitted interior holes.
    """
    pattern = os.environ.get("STORCITO_COVERAGE_REGION", "%galicia%")
    try:
        from shapely.geometry import shape as shapely_shape

        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT ST_AsGeoJSON(geom) FROM spain_autonomous_communities "
                "WHERE acom_name ILIKE %s LIMIT 1",
                (pattern,),
            )
            row = cur.fetchone()
        return shapely_shape(json.loads(row[0])) if row else None
    except Exception as exc:  # noqa: BLE001 - coverage still works unclipped
        logger.warning("Coverage region clip unavailable: %s", exc)
        return None


def _ensure_coverage_rasters(signature: list[dict[str, Any]]) -> dict[str, Path]:
    """Export the source tables to local GeoTIFFs when the DB content changed."""
    COVERAGE_RASTER_DIR.mkdir(parents=True, exist_ok=True)
    marker = COVERAGE_RASTER_DIR / "signature.json"
    table_signature = [entry for entry in signature if entry.get("table") != "fwi_files"]
    try:
        fresh = json.loads(marker.read_text()) == table_signature
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        fresh = False
    paths: dict[str, Path] = {}
    for name, table in COVERAGE_SOURCE_TABLES.items():
        dest = COVERAGE_RASTER_DIR / f"{table}.tif"
        if not fresh or not dest.exists():
            # Only the masked boundary raster gets the region cutline: a
            # cutline would add nodata masks to the other rasters and break
            # the single-masked-raster assumption of the boundary tracer.
            clip = _coverage_region_geom() if table == "fuels" else None
            export_raster_table(table, dest, clip_geom=clip)
        paths[name] = dest
    marker.write_text(json.dumps(table_signature, separators=(",", ":")))
    return paths


def _read_cached_coverage(signature: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        cached = json.loads(COVERAGE_CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if cached.get("input_signature") != signature:
        return None
    coverage = cached.get("coverage")
    return coverage if isinstance(coverage, dict) else None


def _write_cached_coverage(signature: list[dict[str, Any]], coverage: dict[str, Any]) -> None:
    try:
        COVERAGE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        COVERAGE_CACHE_PATH.write_text(json.dumps({
            "input_signature": signature,
            "coverage": coverage,
        }, separators=(",", ":")))
    except OSError as exc:
        logger.warning("Unable to write STORCITO coverage cache: %s", exc)


def _raster_has_exact_mask(src: rasterio.io.DatasetReader) -> bool:
    for band_flags in src.mask_flag_enums:
        if MaskFlags.all_valid not in band_flags:
            return True
    return False


def _raster_exact_outer_boundary(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    with rasterio.open(path) as src:
        valid_mask = src.dataset_mask() > 0
        if not valid_mask.any():
            raise RuntimeError(f"Coverage raster has no valid data: {path}")

        best_geometry = None
        best_area = 0.0
        total_area = 0.0
        component_count = 0

        for geometry, value in features.shapes(
            valid_mask.astype("uint8"),
            mask=valid_mask,
            transform=src.transform,
            connectivity=8,
        ):
            if not value:
                continue
            polygon = shape(geometry)
            area = float(polygon.area)
            if area <= 0:
                continue
            component_count += 1
            total_area += area
            if area > best_area:
                best_area = area
                best_geometry = polygon

        if best_geometry is None:
            raise RuntimeError(f"Unable to derive a valid coverage polygon for: {path}")

        from shapely.geometry import Polygon

        # Main component's outer ring only, ~100 m simplified (raw trace is >20 MB).
        simplified = Polygon(best_geometry.exterior).simplify(100, preserve_topology=True)
        exterior_geometry = {
            "type": "Polygon",
            "coordinates": [[list(coord) for coord in simplified.exterior.coords]],
        }
        wgs84_geometry = transform_geom(
            src.crs or "EPSG:4326",
            "EPSG:4326",
            exterior_geometry,
            precision=6,
        )

    return wgs84_geometry, {
        "component_count": component_count,
        "selected_component_area_m2": best_area,
        "valid_component_area_m2": total_area,
        "selected_component_area_fraction": best_area / total_area if total_area else None,
        "internal_holes_omitted": True,
    }


def available_data_coverage_geojson() -> dict[str, Any]:
    signature = _coverage_input_signature()
    cached = _read_cached_coverage(signature)
    if cached is not None:
        return cached

    raster_paths = _ensure_coverage_rasters(signature)
    rasters: list[dict[str, Any]] = []
    coverage_box = None
    exact_mask_rasters: list[Path] = []

    for name, raster_path in raster_paths.items():
        raster_box = _raster_coverage_box(raster_path)
        coverage_box = raster_box if coverage_box is None else coverage_box.intersection(raster_box)

        with rasterio.open(raster_path) as src:
            has_exact_mask = _raster_has_exact_mask(src)
        if has_exact_mask:
            exact_mask_rasters.append(raster_path)

        rasters.append({
            "name": name,
            "table": COVERAGE_SOURCE_TABLES[name],
            "bbox": [float(value) for value in raster_box.bounds],
            "has_exact_mask": has_exact_mask,
        })

    if coverage_box is None or coverage_box.is_empty:
        raise RuntimeError("Unable to derive a non-empty wildfire data coverage boundary.")

    mask_metadata: dict[str, Any] = {}
    if exact_mask_rasters:
        fuels_path = raster_paths.get("Fuel model")
        boundary_source = (
            fuels_path if fuels_path in exact_mask_rasters else exact_mask_rasters[0]
        )
        coverage_geometry, mask_metadata = _raster_exact_outer_boundary(boundary_source)
        coverage_method = "exact_outer_boundary_from_valid_raster_mask"
    else:
        coverage_geometry = mapping(coverage_box)
        coverage_method = "intersection_of_core_input_raster_bounds"

    dates = [day.isoformat() for day in available_fwi_dates_db()]
    bounds = [float(value) for value in shape(coverage_geometry).bounds]
    coverage = {
        "type": "FeatureCollection",
        "bbox": bounds,
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "Available wildfire data area",
                    "source": "storcito",
                    "coverage_method": coverage_method,
                    "date_from": dates[0] if dates else None,
                    "date_to": dates[-1] if dates else None,
                    "available_dates": dates,
                    "input_rasters": rasters,
                    **mask_metadata,
                },
                "geometry": coverage_geometry,
            }
        ],
    }
    _write_cached_coverage(signature, coverage)
    return coverage
