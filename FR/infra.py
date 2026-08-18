import os
os.environ['GDAL_DATA'] = r'C:\Users\alvar\anaconda3\envs\storcito\Library\share\gdal'
import sys
import time
import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import rasterio

from FR.rutinas.setup import default_imshow, save_file
import numpy.typing as npt
from pathlib import Path
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from shapely.geometry.base import BaseGeometry
from FR.aoi import reproject_geometry
from FR.processing_log import log_array_stats, log_event, logged_step

PUBLISHED_ROAD_DISTANCE_BOUNDS_M = (300, 600, 900, 1200)


def classify_road_distance_risk(
    distance_m: np.ndarray,
    radii: list[int] | tuple[int, ...] = PUBLISHED_ROAD_DISTANCE_BOUNDS_M,
) -> np.ndarray:
    """Map road distance to descending risk classes."""
    if not radii or len(radii) > 5 or list(radii) != sorted(radii):
        raise ValueError("road-distance radii must contain 1-5 increasing distances")
    risks = [5, 4, 3, 2, 1][:len(radii)]
    result = np.full(np.shape(distance_m), max(0, 5 - len(radii)), dtype="uint8")
    for radius, risk in sorted(zip(radii, risks), reverse=True):
        result[np.asarray(distance_m) <= radius] = risk
    return result

# sys.path.append(r'..\geo_auxy')

def _create_risk_rings(geometry: BaseGeometry, radii: list[int], risks: list[int]) -> gpd.GeoDataFrame:
    """Crea anillos concéntricos de riesgo alrededor de geometría. Args: geometry: Geometría unificada (buffer inicial) radii: Lista de radios para los buffers en metros risks: Lista de valores de riesgo correspondientes Returns: GeoDataFrame con geometría de anillos y valores de riesgo"""
    buffers = [geometry.buffer(r) for r in radii]
    anillos_data = []
    
    for i, (buff, risk) in enumerate(zip(buffers, risks)):
        # Primer anillo es el buffer completo, resto son diferencias
        anillo = buff if i == 0 else buff.difference(buffers[i-1])
        
        if not anillo.is_empty:
            anillos_data.append({'geometry': anillo, 'risk': risk})
    
    return gpd.GeoDataFrame(anillos_data)

@logged_step("ROADS", "classify-road-distance")
def infrastructure(input_infra: str|Path,
                   output_folder: str|Path = Path('data/OUTPUT'),
                   ref_raster: str|Path = Path(r'REFERENCE\MDT\DEM_NationalScenario_2013.tif'),
                   epsg: int = 32629,
                   export_image: bool = False,
                   show_plots: bool = False,
                   simplify: bool = False,
                   tolerance: int = 10,
                   aoi_geometry: BaseGeometry | None = None,
                   aoi_crs: str = "EPSG:32629",
                   risk_profile: str = "regional",
                   radii_m: list[int] | None = None,
                   use_reference_grid: bool | None = None) -> npt.NDArray:
    """Classify distance to roads on the reference grid.

    Regional mode reproduces the published Galicia classes: class 5 through
    300 m, classes 4/3/2 through 600/900/1200 m, and class 1 beyond 1200 m.
    Finca mode retains the legacy parcel-scale buffers.
    """
    
    # Validar y convertir paths

    input_infra = Path(input_infra)
    output_folder = Path(output_folder)
    ref_raster = Path(ref_raster)
    profile = (risk_profile or "regional").strip().lower()
    if profile not in {"regional", "finca"}:
        profile = "regional"
    radii = list(
        radii_m
        or (
            [25, 50, 75, 100, 125]
            if profile == "finca"
            else list(PUBLISHED_ROAD_DISTANCE_BOUNDS_M)
        )
    )
    if not radii or len(radii) > 5 or radii != sorted(radii):
        raise ValueError("road-distance radii must contain 1-5 increasing distances")
    outside_risk = max(0, 5 - len(radii))
    native_grid = (profile == "finca") if use_reference_grid is None else bool(use_reference_grid)
    log_event(
        "ROADS",
        "INPUT",
        vectors=input_infra,
        reference=ref_raster,
        profile=profile,
        radii_m=",".join(str(value) for value in radii),
        outside_risk=outside_risk,
    )
    
    # Validar existencia de archivos
    if not input_infra.exists():
        raise FileNotFoundError(f"Archivo de infraestructura no encontrado: {input_infra}")
    if not ref_raster.exists():
        raise FileNotFoundError(f"Raster de referencia no encontrado: {ref_raster}")
    
    # Leer y reproyectar infraestructuras
    road = gpd.read_file(input_infra).to_crs(epsg=epsg)
    if aoi_geometry is not None:
        projected_aoi = reproject_geometry(aoi_geometry, aoi_crs, f"EPSG:{epsg}")
        road = road[road.intersects(projected_aoi.buffer(max(radii)))].copy()
    log_event("ROADS", "VECTOR", feature_count=len(road), crs=f"EPSG:{epsg}")
    
    # Simplificar geometrías si se solicita
    if simplify:
        road['geometry'] = road.geometry.simplify(tolerance=tolerance)

    
    # Obtener parámetros de rasterización del raster de referencia
    with rasterio.open(ref_raster) as src:
        if native_grid:
            transform = src.transform
            x_res = src.width
            y_res = src.height
            ref_crs = src.crs
        else:
            bounds = src.bounds
            x_min, y_min, x_max, y_max = bounds.left, bounds.bottom, bounds.right, bounds.top
            x_res = int((x_max - x_min) / 25)
            y_res = int((y_max - y_min) / 25)
            transform = from_bounds(x_min, y_min, x_max, y_max, x_res, y_res)
            ref_crs = f"EPSG:{epsg}"

    
    if road.empty:
        dist = np.full((y_res, x_res), np.inf)
        raster_data = classify_road_distance_risk(
            dist, radii
        )
        output_crs = ref_crs
    else:
        from scipy.ndimage import distance_transform_edt

        road_mask = rasterize(
            ((geom, 1) for geom in road.geometry),
            out_shape=(y_res, x_res),
            transform=transform,
            fill=0,
            dtype=rasterio.uint8,
            all_touched=True,
        )
        dist = distance_transform_edt(
            road_mask == 0,
            sampling=(abs(transform.e), abs(transform.a)),
        )
        raster_data = classify_road_distance_risk(dist, radii)
        output_crs = ref_crs
    log_array_stats("ROADS", "distance-m", dist)
    log_array_stats("ROADS", "road-distance-risk", raster_data)
    log_event(
        "ROADS",
        "GRID",
        width=x_res,
        height=y_res,
        pixel_x_m=abs(transform.a),
        pixel_y_m=abs(transform.e),
    )
    
    # Configuración de metadatos para guardar
    meta_info = {
        'driver': 'GTiff', 
        'height': y_res, 
        'width': x_res, 
        'count': 1,
        'dtype': rasterio.uint8, 
        'crs': output_crs,
        'transform': transform,
        'compress': 'lzw'
    }
    

    # Visualizar resultado
    fig1, ax1 = default_imshow(raster_data, 'Roads and Railways Risk Map', {'label': 'Risk'})
    fig1.set_size_inches((12, 8))

    if show_plots:
        plt.show()
    
    # Guardar archivos si se solicita
    if export_image:

        save_file(raster_data, input_infra.stem, output_folder, meta_info, 'INFRA Risk_Map',extensions=['tif','png'] ,fig=fig1, meta_intact=True)
    
    return raster_data



if __name__=='__main__':
    
    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        infrastructure(r'data/INPUT\infraestructuras_gal.shp',
            export_image=False)

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
