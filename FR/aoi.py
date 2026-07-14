from __future__ import annotations

import json
import os
import tempfile
from contextlib import nullcontext
from pathlib import Path

import geopandas as gpd
import rasterio
from rasterio.mask import mask
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling, calculate_default_transform, reproject
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
    """Persist an AOI geometry for inspection/debugging. Uses the standard library instead of geopandas.to_file to avoid pulling in pyogrio's GDAL_DATA initialisation, which can fail in some conda/container layouts even when GDAL itself is healthy."""
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
    target_crs: str | None = None,
    resampling: Resampling = Resampling.nearest,
) -> Path:
    """Crop a raster to a geometry and write the result."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(input_path) as source:
        if source.crs is None:
            raise ValueError(f"Input raster has no CRS: {input_path}")
        warped = (
            WarpedVRT(source, crs=target_crs, resampling=resampling)
            if target_crs is not None and str(source.crs) != target_crs
            else nullcontext(source)
        )
        with warped as src:
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


def resample_raster_resolution(
    path: str | Path,
    resolution_m: float,
    *,
    resampling: Resampling = Resampling.nearest,
) -> Path:
    """Atomically resample a projected result raster to a requested metre grid."""
    path = Path(path)
    if resolution_m <= 0:
        raise ValueError("output resolution must be positive")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tif", dir=path.parent)
    os.close(fd)
    Path(tmp_name).unlink(missing_ok=True)
    try:
        with rasterio.open(path) as src:
            if src.crs is None or not src.crs.is_projected:
                raise ValueError(f"result raster must use a projected CRS: {path}")
            current = max(abs(src.transform.a), abs(src.transform.e))
            if abs(current - resolution_m) < 1e-6:
                return path
            transform, width, height = calculate_default_transform(
                src.crs,
                src.crs,
                src.width,
                src.height,
                *src.bounds,
                resolution=resolution_m,
            )
            width = max(1, width)
            height = max(1, height)
            profile = src.profile.copy()
            profile.update(
                transform=transform,
                width=width,
                height=height,
                compress="deflate",
                nodata=src.nodata if src.nodata is not None else 0,
            )
            with rasterio.open(tmp_name, "w", **profile) as dst:
                for band in range(1, src.count + 1):
                    reproject(
                        source=rasterio.band(src, band),
                        destination=rasterio.band(dst, band),
                        src_transform=src.transform,
                        src_crs=src.crs,
                        src_nodata=src.nodata,
                        dst_transform=transform,
                        dst_crs=src.crs,
                        dst_nodata=profile["nodata"],
                        resampling=resampling,
                    )
        Path(tmp_name).replace(path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    return path
