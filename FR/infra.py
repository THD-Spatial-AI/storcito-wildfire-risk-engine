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
    """Calculate infrastructure proximity risk from roads and railways. Creates concentric buffer rings around infrastructure features and assigns decreasing risk values (5 to 1) based on distance (250m to 1250m). Args: input_infra: Path to infrastructure shapefile (roads/railways) output_folder: Output directory for results. Defaults to 'OUTPUT' ref_raster: Reference raster for extent and resolution. Defaults to DEM epsg: Target CRS EPSG code. Defaults to 32629 (UTM 29N) export_image: Whether to save results as GeoTIFF/PNG. Defaults to False show_plots: Whether to display matplotlib plots. Defaults to False simplify: Whether to simplify geometries for performance. Defaults to False tolerance: Simplification tolerance in meters. Defaults to 10 aoi_geometry: Optional AOI geometry used to spatially limit vector processing. aoi_crs: CRS of ``aoi_geometry``. Defaults to EPSG:32629. risk_profile: ``regional`` keeps 250-1250 m buffers; ``finca`` uses the old parcel-scale 25-125 m buffers. radii_m: Optional explicit buffer radii in meters. use_reference_grid: Rasterize on the reference raster's native grid. Defaults to true for finca mode and false for regional mode. Returns: Rasterized risk array with values 0-5 (0=no infrastructure nearby) Raises: FileNotFoundError: If input shapefile or reference raster not found"""
    
    # Validar y convertir paths

    input_infra = Path(input_infra)
    output_folder = Path(output_folder)
    ref_raster = Path(ref_raster)
    profile = (risk_profile or "regional").strip().lower()
    if profile not in {"regional", "finca"}:
        profile = "regional"
    radii = radii_m or ([25, 50, 75, 100, 125] if profile == "finca" else [250, 500, 750, 1000, 1250])
    risks = [5, 4, 3, 2, 1]
    native_grid = (profile == "finca") if use_reference_grid is None else bool(use_reference_grid)
    
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
        raster_data = np.zeros((y_res, x_res), dtype=rasterio.uint8)
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
        raster_data = np.zeros((y_res, x_res), dtype=rasterio.uint8)
        for r, val in sorted(zip(radii, risks), reverse=True):
            raster_data[dist <= r] = val
        output_crs = ref_crs
    
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

