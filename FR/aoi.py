from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import rasterio
from rasterio.mask import mask
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry

DEFAULT_PROJECTED_CRS = "EPSG:32629"


def build_point_aoi(
    longitude: float,
    latitude: float,
    buffer_m: float,
    *,
    projected_crs: str = DEFAULT_PROJECTED_CRS,
) -> BaseGeometry:
    """Build a projected point buffer from WGS84 coordinates."""
    if buffer_m <= 0:
        raise ValueError("buffer_m must be greater than zero.")

    point = gpd.GeoSeries([Point(longitude, latitude)], crs="EPSG:4326")
    return point.to_crs(projected_crs).buffer(buffer_m).iloc[0]


def reproject_geometry(
    geometry: BaseGeometry,
    source_crs: str,
    target_crs: str,
) -> BaseGeometry:
    """Reproject a shapely geometry between coordinate reference systems."""
    return gpd.GeoSeries([geometry], crs=source_crs).to_crs(target_crs).iloc[0]


def build_geojson_aoi(
    geojson_geometry: dict,
    *,
    source_crs: str = "EPSG:4326",
    projected_crs: str = DEFAULT_PROJECTED_CRS,
) -> BaseGeometry:
    """Build a projected AOI geometry from a GeoJSON geometry object."""
    geometry = shape(geojson_geometry)
    if geometry.is_empty:
        raise ValueError("AOI geometry must not be empty.")
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("AOI geometry is invalid.")
    return reproject_geometry(geometry, source_crs, projected_crs)


def write_aoi_geojson(
    geometry: BaseGeometry,
    path: str | Path,
    *,
    crs: str = DEFAULT_PROJECTED_CRS,
) -> Path:
    """Persist an AOI geometry for inspection/debugging.

    Uses the standard library instead of geopandas.to_file to avoid pulling in
    pyogrio's GDAL_DATA initialisation, which can fail in some conda/container
    layouts even when GDAL itself is healthy.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    feature_collection = {
        "type": "FeatureCollection",
        "name": path.stem,
        "crs": {"type": "name", "properties": {"name": crs}},
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": mapping(geometry),
            }
        ],
    }
    path.write_text(json.dumps(feature_collection))
    return path


def crop_raster_to_geometry(
    input_path: str | Path,
    output_path: str | Path,
    geometry: BaseGeometry,
    *,
    geometry_crs: str = DEFAULT_PROJECTED_CRS,
) -> Path:
    """Crop a raster to a geometry and write the result."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(input_path) as src:
        raster_geometry = geometry
        if str(src.crs) != geometry_crs:
            raster_geometry = reproject_geometry(geometry, geometry_crs, str(src.crs))

        out_image, out_transform = mask(src, [mapping(raster_geometry)], crop=True)
        out_meta = src.meta.copy()
        out_meta.update(
            {
                "driver": "GTiff",
                "height": out_image.shape[1],
                "width": out_image.shape[2],
                "transform": out_transform,
            }
        )

        with rasterio.open(output_path, "w", **out_meta) as dst:
            dst.write(out_image)

    return output_path
